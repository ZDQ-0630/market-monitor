"""指标计算：纯函数，输入 pandas Series，输出 dict。不访问网络，便于测试和回测复用。"""
from __future__ import annotations

import math

import pandas as pd

NaN = float("nan")


def _last(s: pd.Series | None) -> float:
    return float(s.iloc[-1]) if s is not None and len(s) else NaN


def price_indicators(close: pd.Series, ma_months: int = 10) -> dict:
    close = close.dropna().sort_index()
    last = float(close.iloc[-1])
    high = float(close.max())
    ma200 = close.rolling(200).mean()
    below200 = close < ma200
    # 只用"已完成"的月份做趋势判断，避免用未走完的当月
    monthly = close.resample("ME").last()
    if len(monthly) and monthly.index[-1].to_period("M") == close.index[-1].to_period("M"):
        monthly_done = monthly.iloc[:-1]
    else:
        monthly_done = monthly
    ma_m = monthly_done.rolling(ma_months).mean()
    trend_below = (
        bool(monthly_done.iloc[-1] < ma_m.iloc[-1])
        if len(monthly_done) >= ma_months and not math.isnan(ma_m.iloc[-1])
        else None
    )
    return {
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
        "close": last,
        "high": high,
        "drawdown": last / high - 1,
        "ret_1d": float(close.iloc[-1] / close.iloc[-2] - 1) if len(close) > 1 else NaN,
        "ma200": _last(ma200),
        "below_ma200": bool(below200.iloc[-1]) if not math.isnan(_last(ma200)) else None,
        "crossed_below_ma200": bool(below200.iloc[-1] and not below200.iloc[-2])
        if len(close) > 201 else False,
        "last_month_end": monthly_done.index[-1].strftime("%Y-%m") if len(monthly_done) else None,
        "trend_below_ma_month_end": trend_below,
        "close_vs_ma_month": last / float(ma_m.iloc[-1]) - 1
        if len(ma_m) and not math.isnan(ma_m.iloc[-1]) else NaN,
    }


def macro_indicators(fred: dict[str, pd.Series]) -> dict:
    g = fred.get
    out: dict = {"as_of": {k: v.index[-1].strftime("%Y-%m-%d") for k, v in fred.items() if len(v)}}
    vix, vix3m = g("VIXCLS"), g("VXVCLS")
    out["vix"] = _last(vix)
    out["vix3m"] = _last(vix3m)
    out["vix_term_ratio"] = out["vix"] / out["vix3m"] if out["vix3m"] else NaN
    hy = g("BAMLH0A0HYM2")
    if hy is not None and len(hy) > 20:
        out["hy_oas"] = _last(hy)
        out["hy_oas_chg_20d"] = float(hy.iloc[-1] - hy.iloc[-21])
        mean12 = float(hy.iloc[-252:].mean())
        out["hy_oas_12m_mean"] = mean12
        out["hy_oas_vs_12m_mean"] = out["hy_oas"] - mean12
    for sid, key in (("T10Y3M", "t10y3m"), ("DFII10", "real_10y"), ("DGS10", "ust_10y"),
                     ("NFCI", "nfci"), ("SAHMREALTIME", "sahm"), ("DTWEXBGS", "usd_broad")):
        out[key] = _last(g(sid))
    r = g("DFII10")
    out["real_10y_chg_20d"] = float(r.iloc[-1] - r.iloc[-21]) if r is not None and len(r) > 20 else NaN
    return out
