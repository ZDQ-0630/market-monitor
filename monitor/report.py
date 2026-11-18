"""生成 reports/latest.json（给 Claude 读）和 reports/daily/日期.md（给人看）。"""
from __future__ import annotations

import math

EMOJI = {"red": "🔴", "yellow": "🟡", "white": "⚪"}


def _f(x, fmt="{:.2f}"):
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else fmt.format(x)


def to_markdown(run_date: str, level: str, alerts: list, px: dict, macro: dict, st: dict, issues: list) -> str:
    L = [f"# 市场监控 {run_date}  {EMOJI[level]} {level.upper()}", ""]
    if alerts:
        L += ["## 报警", *[f"- {EMOJI[a['level']]} {a['msg']}" for a in alerts], ""]
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
    if issues:
        L += ["", "## 数据问题（缺失项不得用推测数字替代）", *[f"- {i}" for i in issues]]
    return "\n".join(L) + "\n"
