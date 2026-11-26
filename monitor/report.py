"""生成 reports/latest.json（给 Claude 读）和 reports/daily/日期.md（给人看）。"""
from __future__ import annotations

import math

EMOJI = {"red": "🔴", "yellow": "🟡", "white": "⚪"}


def _f(x, fmt="{:.2f}"):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else fmt.format(x)


def _mark(v) -> str:
    return "✓" if v == 1 else ("✗" if v == 0 else "?")


def to_markdown(run_date: str, level: str, alerts: list, px: dict, macro: dict, st: dict, issues: list,
                framework: dict | None = None, technical: dict | None = None) -> str:
    L = [f"# 市场监控 {run_date}  {EMOJI[level]} {level.upper()}", ""]
    if alerts:
        L += ["## 报警", *[f"- {EMOJI[a['level']]} {a['msg']}" + ("（沿用上次，数据未更新）" if a.get("repeat") else "")
                          for a in alerts], ""]
    else:
        L += ["今日无规则触发。", ""]
    L += ["## ETF", "| 标的 | 数据日 | 收盘 | 距高点 | 1日 | 200日线下方 | 月末<10月均线 |",
          "|---|---|---|---|---|---|---|"]
    for t, p in px.items():
        L.append(f"| {t} | {p['as_of']} | {_f(p['close'])} | {_f(p['drawdown'], '{:.1%}')} | "
                 f"{_f(p['ret_1d'], '{:+.1%}')} | {p['below_ma200']} | {p['trend_below_ma_month_end']} |")
    L += ["", "## 宏观 / 信用 / 情绪",
          f"- VIX {_f(macro.get('vix'), '{:.1f}')}，VIX/VIX3M {_f(macro.get('vix_term_ratio'))}",
          f"- 高收益债利差 {_f(macro.get('hy_oas'))}%（20日 {_f(macro.get('hy_oas_chg_20d'), '{:+.2f}')}pp，"
          f"较12月均值 {_f(macro.get('hy_oas_vs_12m_mean'), '{:+.2f}')}pp）",
          f"- 10Y-3M {_f(macro.get('t10y3m'))}%，10Y {_f(macro.get('ust_10y'))}%，"
          f"实际利率 {_f(macro.get('real_10y'))}%（20日 {_f(macro.get('real_10y_chg_20d'), '{:+.2f}')}）",
          f"- NFCI {_f(macro.get('nfci'))}，Sahm {_f(macro.get('sahm'))}",
          "", f"## 规则状态\n- 钥匙：{st.get('keys')}；降风险状态：{st.get('two_key_active')}；"
          f"抄底阶梯已触发档位：{st.get('ladder')}"]
    if framework:
        d = framework["dims"]
        L += ["", f"## 抄底框架（数据维度，{framework['ticker']}，本轮起点 {framework['episode_start']}）",
              f"- 数据维度得分 {framework['data_score']}/4：价格{_mark(d['price'])} 恐慌{_mark(d['panic'])} "
              f"信用{_mark(d['credit'])} 宏观{_mark(d['macro'])}；政策、主线由 Claude 定时任务判断",
              f"- 本轮最低收盘 {_f(framework['episode_low'])}（{framework['episode_low_date']}），"
              f"之后 {framework['days_since_low']} 个交易日，已反弹 {_f(framework['rebound_from_low'], '{:+.1%}')}",
              f"- 本轮 VIX 最高 {_f(framework['vix_max_in_episode'], '{:.1f}')}；"
              f"高收益债利差比本轮最高值低 {_f(framework['hy_off_peak_pp'])}pp；"
              f"首次申领 4 周均值同比 {_f(framework['icsa_4w_yoy'], '{:+.1%}')}"]
        et = framework.get("early_trigger")
        if et:
            L.append(f"- 大涨提前买条件：QQQ 连续 {et['days_above_ma200']}/{et['required']} 个交易日站上 200 日线"
                     f" → {'已满足' if et['met'] else '未满足'}")
        L += [f"- 备注：{n}" for n in framework.get("notes", [])]
    if technical:
        ma = {True: "上方", False: "下方"}.get(technical["ma50_above_ma200"], "—")
        L += [f"- 技术位置（只作参考，不计分）：RSI14 {_f(technical['rsi14'], '{:.0f}')}｜"
              f"距 200 日线 {_f(technical['vs_ma200'], '{:+.1%}')}｜50 日线在 200 日线{ma}｜"
              f"距 52 周高点 {_f(technical['dd_52w'], '{:+.1%}')}"]
    if issues:
        L += ["", "## 数据问题（缺失项不得用推测数字替代）", *[f"- {i}" for i in issues]]
    return "\n".join(L) + "\n"
