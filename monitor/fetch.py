"""数据采集：FRED（免费 CSV 接口，无需 key）+ ETF 价格（yfinance，失败则回退 Stooq）。

设计要点：
- 并行采集、短超时：单个源卡住不会拖垮整个任务。
- 每个源打印耗时和结果，GitHub Actions 日志里能直接看到哪一步出问题。
- 任何单个数据源失败都只记录到 issues，报告中明确写"缺失"，下游不得凭空补数字。
"""
from __future__ import annotations

import io
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36 market-monitor/0.2",
    "Accept": "text/csv,text/plain,*/*",
}
TIMEOUT = (5, 25)  # (连接, 读取) 秒
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
CBOE_FALLBACK = {"VIXCLS": "VIX", "VXVCLS": "VIX3M"}


def log(msg: str) -> None:
    print(msg, flush=True)


def _get(url: str, retries: int = 2) -> str:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + i)
    raise RuntimeError(f"{type(last).__name__}: {last}")


def fetch_fred(series_id: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(_get(FRED_URL.format(sid=series_id))))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")  # FRED 用 "." 表示缺失
    s = df.dropna().set_index("date")["value"]
    if s.empty:
        raise RuntimeError("no data")
    s.name = series_id
    return s


def fetch_cboe(name: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(_get(CBOE_URL.format(name=name))))
    df.columns = [c.strip().upper() for c in df.columns]
    s = pd.Series(pd.to_numeric(df["CLOSE"], errors="coerce").values,
                  index=pd.to_datetime(df["DATE"])).dropna()
    if s.empty:
        raise RuntimeError("no data")
    return s


def fetch_price_yf(ticker: str) -> pd.Series:
    import yfinance as yf  # 延迟导入：测试环境不需要

    h = yf.Ticker(ticker).history(period="max", auto_adjust=True, timeout=20)
    if h.empty:
        raise RuntimeError("empty")
    s = h["Close"].copy()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s.name = ticker
    return s


def fetch_price_stooq(ticker: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(_get(STOOQ_URL.format(sym=ticker.lower()))))
    if "Close" not in df.columns or df.empty:
        raise RuntimeError("bad payload")
    s = df.set_index(pd.to_datetime(df["Date"]))["Close"].astype(float)
    s.name = ticker
    return s


def _timed(label: str, fn, *args):
    t0 = time.time()
    try:
        out = fn(*args)
        log(f"[ok]   {label:<22} {time.time() - t0:5.1f}s  n={len(out)} last={out.index[-1].date()}")
        return out, None
    except Exception as e:  # noqa: BLE001
        log(f"[fail] {label:<22} {time.time() - t0:5.1f}s  {e}")
        return None, str(e)


def _one_fred(sid: str):
    s, err = _timed(f"FRED {sid}", fetch_fred, sid)
    if s is None and sid in CBOE_FALLBACK:
        s2, err2 = _timed(f"Cboe {CBOE_FALLBACK[sid]}", fetch_cboe, CBOE_FALLBACK[sid])
        if s2 is not None:
            s2.name = sid
            return sid, s2, f"FRED {sid} 失败，已改用 Cboe 官方数据"
        err = f"{err}; Cboe: {err2}"
    return sid, s, (None if s is not None else f"FRED {sid} 获取失败：{err}")


def _one_price(t: str):
    s, err = _timed(f"yfinance {t}", fetch_price_yf, t)
    if s is not None:
        return t, s, None
    s2, err2 = _timed(f"stooq {t}", fetch_price_stooq, t)
    if s2 is not None:
        return t, s2, f"{t} 价格来自备用源 Stooq（未做股息复权）"
    return t, None, f"{t} 价格获取失败：yfinance: {err}; stooq: {err2}"


def fetch_all(cfg: dict) -> tuple[dict, dict, list[str]]:
    """返回 (fred: {id: Series}, prices: {ticker: Series}, issues: [str])"""
    fred, prices, issues = {}, {}, []
    u = cfg["universe"]
    tickers = list(dict.fromkeys(u["core"] + u["satellite"] + u["reference"]))
    with ThreadPoolExecutor(max_workers=8) as ex:
        f_jobs = [ex.submit(_one_fred, sid) for sid in cfg["fred_series"]]
        p_jobs = [ex.submit(_one_price, t) for t in tickers]
        for j in f_jobs:
            sid, s, note = j.result()
            if s is not None:
                fred[sid] = s
            if note:
                issues.append(note)
        for j in p_jobs:
            t, s, note = j.result()
            if s is not None:
                prices[t] = s
            if note:
                issues.append(note)
    log(f"FRED {len(fred)}/{len(cfg['fred_series'])}，价格 {len(prices)}/{len(tickers)}，问题 {len(issues)} 条")
    return fred, prices, issues
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
