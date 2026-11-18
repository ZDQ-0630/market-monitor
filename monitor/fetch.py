"""数据采集：FRED（免费 CSV 接口，无需 key）+ ETF 价格（yfinance，失败则回退 Stooq）。

任何单个数据源失败都不会让整个任务崩溃，而是记录到 issues 里，报告中会明确写出"缺失"，
这样下游的 Claude 简报只能说"数据缺失"，不会凭空补数字。
"""
from __future__ import annotations

import io
import logging
import time

import pandas as pd
import requests

log = logging.getLogger(__name__)
UA = {"User-Agent": "market-monitor/0.1 (personal research)"}
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"


def _get(url: str, retries: int = 3, timeout: int = 30) -> str:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


def fetch_fred(series_id: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(_get(FRED_URL.format(sid=series_id))))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")  # FRED 用 "." 表示缺失
    s = df.dropna().set_index("date")["value"]
    s.name = series_id
    if s.empty:
        raise RuntimeError(f"FRED {series_id} returned no data")
    return s


def fetch_price_yf(ticker: str) -> pd.Series:
    import yfinance as yf  # 延迟导入：测试环境不需要

    h = yf.Ticker(ticker).history(period="max", auto_adjust=True)
    if h.empty:
        raise RuntimeError(f"yfinance empty for {ticker}")
    s = h["Close"].copy()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s.name = ticker
    return s


def fetch_price_stooq(ticker: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(_get(STOOQ_URL.format(sym=ticker.lower()))))
    if "Close" not in df.columns or df.empty:
        raise RuntimeError(f"Stooq bad payload for {ticker}")
    s = df.set_index(pd.to_datetime(df["Date"]))["Close"].astype(float)
    s.name = ticker
    return s


def fetch_price(ticker: str) -> tuple[pd.Series, str]:
    errors = []
    for name, fn in (("yfinance", fetch_price_yf), ("stooq", fetch_price_stooq)):
        try:
            return fn(ticker), name
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    raise RuntimeError("; ".join(errors))


def fetch_all(cfg: dict) -> tuple[dict, dict, list[str]]:
    """返回 (fred: {id: Series}, prices: {ticker: Series}, issues: [str])"""
    fred, prices, issues = {}, {}, []
    for sid in cfg["fred_series"]:
        try:
            fred[sid] = fetch_fred(sid)
        except Exception as e:  # noqa: BLE001
            issues.append(f"FRED {sid} 获取失败：{e}")
    u = cfg["universe"]
    tickers = list(dict.fromkeys(u["core"] + u["satellite"] + u["reference"]))
    for t in tickers:
        try:
            prices[t], src = fetch_price(t)
            if src != "yfinance":
                issues.append(f"{t} 价格来自备用源 {src}（未做股息复权）")
        except Exception as e:  # noqa: BLE001
            issues.append(f"{t} 价格获取失败：{e}")
    return fred, prices, issues
