"""数据采集。每个指标按"来源链"依次尝试，第一个成功的为准：

| 指标 | 来源链 |
|---|---|
| VIX / VIX3M | FRED API（有 key 时）→ Cboe 官方 CSV → FRED 网页 |
| 10年国债、10Y-3M、10年实际利率 | FRED API → 美国财政部官方收益率曲线 CSV → FRED 网页 |
| Sahm 规则 | FRED API → 由 BLS 官方失业率计算 → FRED 网页 |
| 高收益债利差、NFCI、美元指数 | FRED API → FRED 网页（无其他免费官方源） |
| ETF 价格 | yfinance（串行，避免缓存锁）→ Stooq |

FRED 网页接口（fredgraph.csv）在 GitHub Actions 上实测会超时，因此放在最后且超时很短。
若在仓库 Secrets 里设置 FRED_API_KEY（免费申请），会优先走 FRED 官方 API。
任何指标全部来源失败时只记录到 issues，报告中写"缺失"，下游不得凭空补数字。
"""
from __future__ import annotations

import datetime as dt
import functools
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36 market-monitor/0.3",
    "Accept": "text/csv,application/json,text/plain,*/*",
}
FRED_WEB = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
FRED_API = ("https://api.stlouisfed.org/fred/series/observations?series_id={sid}"
            "&api_key={key}&file_type=json&observation_start=2010-01-01")
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
UST_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
           "daily-treasury-rates.csv/{y}/all?type={kind}&field_tdr_date_value={y}&page&_format=csv")
BLS_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/LNS14000000"
_YF_LOCK = threading.Lock()
LAST_SOURCES: dict[str, str] = {}  # 本次运行每个指标实际使用的来源，写进 latest.json


def log(msg: str) -> None:
    print(msg, flush=True)


def _get(url: str, retries: int = 2, timeout=(5, 25)) -> str:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < retries:
                time.sleep(1 + i)
    msg = str(last)
    if "api_key=" in msg:  # 不把 key 打进日志
        msg = msg.split("api_key=")[0] + "api_key=***"
    raise RuntimeError(f"{type(last).__name__}: {msg[:200]}")


# ---------- 解析函数（纯函数，可离线测试） ----------
def parse_fred_csv(text: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(text))
    df.columns = ["date", "value"]
    s = pd.Series(pd.to_numeric(df["value"], errors="coerce").values, index=pd.to_datetime(df["date"]))
    return s.dropna()


def parse_fred_api(text: str) -> pd.Series:
    obs = json.loads(text)["observations"]
    s = pd.Series({pd.Timestamp(o["date"]): pd.to_numeric(o["value"], errors="coerce") for o in obs})
    return s.dropna().sort_index()


def parse_cboe(text: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().upper() for c in df.columns]
    return pd.Series(pd.to_numeric(df["CLOSE"], errors="coerce").values,
                     index=pd.to_datetime(df["DATE"])).dropna().sort_index()


def parse_treasury(texts: list[str]) -> pd.DataFrame:
    frames = []
    for t in texts:
        df = pd.read_csv(io.StringIO(t))
        df.columns = [c.strip().upper() for c in df.columns]
        df["DATE"] = pd.to_datetime(df["DATE"], format="%m/%d/%Y")
        frames.append(df.set_index("DATE"))
    out = pd.concat(frames).sort_index()
    return out[~out.index.duplicated(keep="last")].apply(pd.to_numeric, errors="coerce")


def sahm_from_unrate(u: pd.Series) -> pd.Series:
    """Sahm 规则：失业率 3 个月均值 − 此前 12 个月内 3 个月均值的最低值。"""
    m3 = u.sort_index().rolling(3).mean()
    prior_min = m3.shift(1).rolling(12).min()
    return (m3 - prior_min).dropna()


def parse_bls(text: str) -> pd.Series:
    data = json.loads(text)["Results"]["series"][0]["data"]
    rows = {pd.Timestamp(int(d["year"]), int(d["period"][1:]), 1): pd.to_numeric(d["value"], errors="coerce")
            for d in data if d["period"].startswith("M") and d["period"] != "M13"}
    return pd.Series(rows, dtype=float).dropna().sort_index()  # BLS 用 "-" 表示缺失月份


# ---------- 各来源 ----------
def src_fred_api(sid: str) -> pd.Series:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未设置 FRED_API_KEY")
    return parse_fred_api(_get(FRED_API.format(sid=sid, key=key)))


def src_fred_web(sid: str) -> pd.Series:
    return parse_fred_csv(_get(FRED_WEB.format(sid=sid), retries=1, timeout=(5, 12)))


@functools.lru_cache(maxsize=4)
def _treasury(kind: str) -> pd.DataFrame:
    y = dt.date.today().year
    return parse_treasury([_get(UST_URL.format(y=yr, kind=kind)) for yr in (y - 1, y)])


def src_treasury(sid: str) -> pd.Series:
    if sid == "DGS10":
        return _treasury("daily_treasury_yield_curve")["10 YR"].dropna()
    if sid == "T10Y3M":
        df = _treasury("daily_treasury_yield_curve")
        return (df["10 YR"] - df["3 MO"]).dropna()
    if sid == "DFII10":
        return _treasury("daily_treasury_real_yield_curve")["10 YR"].dropna()
    raise RuntimeError("no treasury mapping")


def src_cboe(sid: str) -> pd.Series:
    return parse_cboe(_get(CBOE_URL.format(name={"VIXCLS": "VIX", "VXVCLS": "VIX3M"}[sid])))


def src_bls_sahm(sid: str) -> pd.Series:
    return sahm_from_unrate(parse_bls(_get(BLS_URL)))


CHAINS = {
    "VIXCLS": [("FRED API", src_fred_api), ("Cboe", src_cboe), ("FRED 网页", src_fred_web)],
    "VXVCLS": [("FRED API", src_fred_api), ("Cboe", src_cboe), ("FRED 网页", src_fred_web)],
    "DGS10": [("FRED API", src_fred_api), ("美国财政部", src_treasury), ("FRED 网页", src_fred_web)],
    "T10Y3M": [("FRED API", src_fred_api), ("美国财政部", src_treasury), ("FRED 网页", src_fred_web)],
    "DFII10": [("FRED API", src_fred_api), ("美国财政部", src_treasury), ("FRED 网页", src_fred_web)],
    "SAHMREALTIME": [("FRED API", src_fred_api), ("BLS 计算", src_bls_sahm), ("FRED 网页", src_fred_web)],
}
DEFAULT_CHAIN = [("FRED API", src_fred_api), ("FRED 网页", src_fred_web)]


def fetch_price_yf(ticker: str) -> pd.Series:
    import yfinance as yf  # 延迟导入：测试环境不需要

    with _YF_LOCK:  # yfinance 的本地缓存是 sqlite，并发会报 "database is locked"
        h = yf.Ticker(ticker).history(period="max", auto_adjust=True, timeout=20)
    if h.empty:
        raise RuntimeError("empty")
    s = h["Close"].copy()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s


def fetch_price_stooq(ticker: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(_get(STOOQ_URL.format(sym=ticker.lower()), retries=1)))
    if "Close" not in df.columns or df.empty:
        raise RuntimeError("bad payload")
    return df.set_index(pd.to_datetime(df["Date"]))["Close"].astype(float)


def _run_chain(name: str, chain) -> tuple[pd.Series | None, str | None, list[str]]:
    errors = []
    for label, fn in chain:
        t0 = time.time()
        try:
            s = fn(name)
            if s is None or s.empty:
                raise RuntimeError("no data")
            s.name = name
            log(f"[ok]   {name:<13} via {label:<8} {time.time() - t0:5.1f}s  n={len(s)} last={s.index[-1].date()}")
            return s, label, errors
        except Exception as e:  # noqa: BLE001
            errors.append(f"{label}: {e}")
            log(f"[skip] {name:<13} via {label:<8} {time.time() - t0:5.1f}s  {str(e)[:120]}")
    return None, None, errors


def fetch_all(cfg: dict) -> tuple[dict, dict, list[str]]:
    """返回 (fred: {id: Series}, prices: {ticker: Series}, issues: [str])"""
    fred, prices, issues = {}, {}, []
    u = cfg["universe"]
    tickers = list(dict.fromkeys(u["core"] + u["satellite"] + u["reference"]))
    price_chain = [("yfinance", fetch_price_yf), ("Stooq", fetch_price_stooq)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        f_jobs = {sid: ex.submit(_run_chain, sid, CHAINS.get(sid, DEFAULT_CHAIN)) for sid in cfg["fred_series"]}
        p_jobs = {t: ex.submit(_run_chain, t, price_chain) for t in tickers}
        for sid, j in f_jobs.items():
            s, src, errs = j.result()
            if s is None:
                hint = "（可在仓库 Secrets 设置免费的 FRED_API_KEY 解决）" if sid in (
                    "BAMLH0A0HYM2", "NFCI", "DTWEXBGS") else ""
                issues.append(f"{sid} 缺失{hint}：{'; '.join(errs)[:300]}")
            else:
                fred[sid] = s
                LAST_SOURCES[sid] = src
        for t, j in p_jobs.items():
            s, src, errs = j.result()
            if s is None:
                issues.append(f"{t} 价格缺失：{'; '.join(errs)[:300]}")
            else:
                prices[t] = s
                LAST_SOURCES[t] = src
                if src != "yfinance":
                    issues.append(f"{t} 价格来自备用源 {src}（未做股息复权）")
    log(f"宏观 {len(fred)}/{len(cfg['fred_series'])}，价格 {len(prices)}/{len(tickers)}，备注 {len(issues)} 条")
    return fred, prices, issues
