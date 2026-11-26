"""规则引擎：把指标对照 IPS 阈值，输出报警（红/黄/白）和更新后的状态。

状态（state.json）用于"只在越过新阈值时报警一次"，避免每天重复同一条红色报警。
"""
from __future__ import annotations

import copy
import math

LEVELS = {"white": 0, "yellow": 1, "red": 2}


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def evaluate(px: dict, macro: dict, cfg: dict, state: dict) -> tuple[list[dict], dict]:
    st = copy.deepcopy(state) if state else {}
    st.setdefault("ladder", {})
    st.setdefault("two_key_active", False)
    alerts: list[dict] = []

    def add(level, code, msg):
        alerts.append({"level": level, "code": code, "msg": msg})

    # ---- 红色 1：抄底阶梯 ----
    lad = cfg["drawdown_ladder"]
    dca = cfg.get("monthly_dca_sgd")
    # 只保留当前阶梯标的的状态（清掉旧配置残留，例如已移出阶梯的 SMH）
    st["ladder"] = {t: v for t, v in st["ladder"].items() if t in lad["tickers"]}
    for t in lad["tickers"]:
        p = px.get(t)
        if not p:
            continue
        dd = p["drawdown"]
        hit = st["ladder"].get(t, -1)
        if dd >= -lad["reset_within_pct_of_high"]:
            st["ladder"][t] = -1  # 接近新高，阶梯重置
            continue
        deepest = -1
        for i, lv in enumerate(lad["levels"]):
            if dd <= lv["drawdown"]:
                deepest = i
        if deepest > hit:
            # 一次跳过多档（如暴跌或系统刚上线）时，补齐所有未执行的档位
            mult = sum(lad["levels"][i]["extra_multiple"] for i in range(hit + 1, deepest + 1))
            amt = f"≈ SGD {mult * dca:.0f}" if dca else f"{mult}× 月定投额"
            span = f"第 {deepest + 1} 档" if deepest == hit + 1 else f"第 {hit + 2}–{deepest + 1} 档"
            add("red", f"LADDER_{t}_{deepest}",
                f"{t} 距高点回撤 {dd:.1%}，越过抄底阶梯{span}（{lad['levels'][deepest]['drawdown']:.0%}）→ "
                f"按规则额外投入 {amt}（限价单分批执行）")
            st["ladder"][t] = deepest

    # ---- 红色 2：两把钥匙 ----
    tk = cfg["two_key"]
    tp = px.get(tk["trend_ticker"])
    trend_key = tp.get("trend_below_ma_month_end") if tp else None
    hyv, sahm = macro.get("hy_oas_vs_12m_mean"), macro.get("sahm")
    macro_key = (_ok(hyv) and hyv >= tk["hy_oas_above_12m_mean_pp"]) or \
                (_ok(sahm) and sahm >= tk["sahm_threshold"])
    st["keys"] = {"trend": trend_key, "macro": bool(macro_key)}
    if trend_key is True and macro_key and not st["two_key_active"]:
        st["two_key_active"] = True
        add("red", "TWO_KEY_ON",
            f"两把钥匙同时转动：{tk['trend_ticker']} 月末收盘低于 {tk['ma_months']} 个月均线，"
            f"且信用/就业恶化（HY利差较12月均值 {hyv:+.2f}pp，Sahm {sahm:.2f}）→ {tk['action']}")
    elif st["two_key_active"] and trend_key is False:
        st["two_key_active"] = False
        add("red", "TWO_KEY_OFF", f"降风险状态解除：趋势钥匙已解除 → {tk['resume']}")
    elif st["two_key_active"]:
        add("yellow", "TWO_KEY_HOLD", "降风险状态持续中（等待趋势钥匙解除），继续执行降风险规则")
    elif trend_key is True or macro_key:
        which = "趋势钥匙" if trend_key is True else "宏观/信用钥匙"
        add("yellow", "ONE_KEY", f"只有一把钥匙转动（{which}），不行动，继续观察")

    # ---- 黄色：观察区 ----
    w = cfg["watch"]
    if _ok(macro.get("vix")) and macro["vix"] >= w["vix_level"]:
        add("yellow", "VIX_HIGH", f"VIX {macro['vix']:.1f} ≥ {w['vix_level']}（恐慌区，留意抄底阶梯）")
    if _ok(macro.get("vix_term_ratio")) and macro["vix_term_ratio"] >= w["vix_term_ratio"]:
        add("yellow", "VIX_INVERT", f"VIX 期限结构倒挂（VIX/VIX3M = {macro['vix_term_ratio']:.2f}）")
    if _ok(macro.get("hy_oas_chg_20d")) and macro["hy_oas_chg_20d"] >= w["hy_oas_20d_change_pp"]:
        add("yellow", "HY_WIDEN", f"高收益债利差 20 日走阔 {macro['hy_oas_chg_20d']:+.2f}pp（当前 {macro['hy_oas']:.2f}%）")
    if _ok(macro.get("nfci")) and macro["nfci"] > w["nfci_above"]:
        add("yellow", "NFCI_TIGHT", f"金融条件指数 NFCI = {macro['nfci']:.2f} > 0（偏紧）")
    for t, p in px.items():
        if p.get("crossed_below_ma200"):
            add("yellow", f"MA200_{t}", f"{t} 收盘跌破 200 日均线")
        if _ok(p.get("ret_1d")) and abs(p["ret_1d"]) >= w["daily_move_abs"]:
            add("yellow", f"BIGMOVE_{t}", f"{t} 单日 {p['ret_1d']:+.1%}")

    return alerts, st


def overall_level(alerts: list[dict]) -> str:
    if not alerts:
        return "white"
    return max((a["level"] for a in alerts), key=LEVELS.get)
