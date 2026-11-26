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


# ---------------------------------------------------------------------------
# 技术位置与抄底框架（建仓执行计划用；只描述状态，不触发任何报警）
def rsi(close: pd.Series, n: int = 14) -> float:
    """Wilder 平滑的 RSI，返回最新值。"""
    close = close.dropna()
    if len(close) <= n:
        return NaN
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    g, lo = float(gain.iloc[-1]), float(loss.iloc[-1])
    if lo == 0:
        return 100.0 if g > 0 else 50.0
    return 100 - 100 / (1 + g / lo)


def technical_indicators(close: pd.Series) -> dict:
    """位置读数：RSI(14)、距 200 日线、50/200 日线相对位置、距 52 周高点。只作参考，不计分。"""
    close = close.dropna().sort_index()
    last = float(close.iloc[-1])
    ma50, ma200 = close.rolling(50).mean(), close.rolling(200).mean()
    high52 = float(close.iloc[-252:].max())
    m50, m200 = _last(ma50), _last(ma200)
    above = (close > ma200).to_numpy()[::-1]            # 从最新一天往回数，连续站上 200 日线的天数
    days_above = int(above.argmin()) if not above.all() else int(above.size)
    return {
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
        "rsi14": rsi(close),
        "vs_ma200": last / m200 - 1 if not math.isnan(m200) else NaN,
        "ma50_above_ma200": bool(m50 > m200) if not (math.isnan(m50) or math.isnan(m200)) else None,
        "days_above_ma200": days_above,
        "dd_52w": last / high52 - 1,
    }


def _window(s: pd.Series | None, start) -> pd.Series | None:
    if s is None or not len(s):
        return None
    w = s.dropna().sort_index()
    w = w[w.index >= start]
    return w if len(w) else None


def framework_indicators(close: pd.Series, fred: dict[str, pd.Series], fw: dict) -> dict:
    """抄底框架中可以用数据计算的 4 个维度（价格、恐慌、信用、宏观），各 1/0；数据缺失记 None。

    "本轮" = 最近一次创 52 周（252 个交易日）收盘新高之后的这段时间。
    政策、主线两个维度需要检索新闻，由 Claude 定时任务判断，这里固定为 None。
    """
    close = close.dropna().sort_index()
    notes: list[str] = []
    roll_max = close.rolling(252, min_periods=1).max()
    start = close.index[(close >= roll_max).to_numpy()][-1]
    ep = close[close.index >= start]
    low_date = ep.idxmin()
    low = float(ep.min())
    days_since_low = len(close[close.index > low_date])
    rebound = float(close.iloc[-1]) / low - 1
    pc = fw["price"]
    price = int(days_since_low >= pc["min_days_since_low"] and rebound >= pc["min_rebound_from_low"])

    vix, vix3m = _window(fred.get("VIXCLS"), start), fred.get("VXVCLS")
    if vix is None or vix3m is None or not len(vix3m):
        panic, vix_max = None, NaN
        notes.append("VIX 或 VIX3M 缺失，恐慌维度无法计算")
    else:
        vix_max = float(vix.max())
        panic = int(vix_max >= fw["panic"]["vix_spike"] and _last(fred["VIXCLS"]) < _last(vix3m))

    hy = _window(fred.get("BAMLH0A0HYM2"), start)
    if hy is None:
        credit, hy_peak, hy_off = None, NaN, NaN
        notes.append("高收益债利差缺失，信用维度无法计算")
    else:
        hy_peak = float(hy.max())
        hy_off = hy_peak - float(hy.iloc[-1])
        credit = int(hy_off >= fw["credit"]["hy_off_peak_pp"])

    sahm, icsa = _last(fred.get("SAHMREALTIME")), fred.get("ICSA")
    icsa_4w = icsa_yoy = NaN
    if icsa is not None and len(icsa.dropna()) >= 56:
        ic = icsa.dropna().sort_index()
        icsa_4w = float(ic.iloc[-4:].mean())
        icsa_yoy = icsa_4w / float(ic.iloc[-56:-52].mean()) - 1
    if math.isnan(sahm):
        macro = None
        notes.append("Sahm 缺失，宏观维度无法计算")
    elif math.isnan(icsa_yoy):
        macro = int(sahm < fw["macro"]["sahm_below"])
        notes.append("首次申领失业金缺失，宏观维度只看 Sahm")
    else:
        macro = int(sahm < fw["macro"]["sahm_below"] and icsa_yoy <= 0)

    dims = {"price": price, "panic": panic, "credit": credit, "macro": macro, "policy": None, "theme": None}
    return {
        "ticker": fw.get("ticker", "QQQ"),
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
        "episode_start": start.strftime("%Y-%m-%d"),
        "episode_high": float(close[start]),
        "episode_low": low,
        "episode_low_date": low_date.strftime("%Y-%m-%d"),
        "days_since_low": days_since_low,
        "rebound_from_low": rebound,
        "vix_max_in_episode": vix_max,
        "hy_peak_in_episode": hy_peak,
        "hy_off_peak_pp": hy_off,
        "sahm": sahm,
        "icsa_4w": icsa_4w,
        "icsa_4w_yoy": icsa_yoy,
        "dims": dims,
        "data_score": sum(v for k, v in dims.items() if k in ("price", "panic", "credit", "macro") and v),
        "notes": notes,
    }
