"""每日入口：python -m monitor.run

输出：
- data/snapshots.csv      只追加的点时快照（每次运行一行），以后做回测/复盘不会有前视偏差
- data/state.json        规则状态（已触发的阶梯档位、两把钥匙状态）
- reports/latest.json    给 Claude 定时任务读取的机器可读摘要
- reports/daily/D.md     人看的日报
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import math
from pathlib import Path

import yaml

from . import fetch, indicators, report, rules

ROOT = Path(__file__).resolve().parents[1]


def _clean(o):
    if isinstance(o, float) and math.isnan(o):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def run(root: Path = ROOT, fetcher=fetch.fetch_all, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    cfg = yaml.safe_load((root / "config/ips.yaml").read_text(encoding="utf-8"))
    fred, prices, issues = fetcher(cfg)

    ma_m = cfg["two_key"]["ma_months"]
    px = {}
    for t, s in prices.items():
        try:
            px[t] = indicators.price_indicators(s, ma_m)
        except Exception as e:  # noqa: BLE001
            issues.append(f"{t} 指标计算失败：{e}")
    macro = indicators.macro_indicators(fred)

    # 建仓执行计划：抄底框架的 4 个数据维度 + 技术位置（只描述状态，不产生报警）
    framework, technical = None, None
    plan = cfg.get("entry_plan") or {}
    if plan.get("status") == "active" and plan.get("framework"):
        fw_t = plan["framework"].get("ticker", "QQQ")
        if fw_t in prices:
            try:
                framework = indicators.framework_indicators(prices[fw_t], fred, plan["framework"])
                technical = indicators.technical_indicators(prices[fw_t])
                et = (plan.get("first_half") or {}).get("early_trigger")
                if et:  # 大涨提前买：QQQ 连续 N 个交易日站上 200 日线
                    framework["early_trigger"] = {
                        "days_above_ma200": technical["days_above_ma200"],
                        "required": et["above_ma200_days"],
                        "met": technical["days_above_ma200"] >= et["above_ma200_days"]}
            except Exception as e:  # noqa: BLE001
                issues.append(f"抄底框架计算失败：{e}")
        else:
            issues.append(f"抄底框架缺少 {fw_t} 价格，本次未计算")

    (root / "data").mkdir(parents=True, exist_ok=True)
    state_p = root / "data/state.json"
    state = json.loads(state_p.read_text()) if state_p.exists() else {}
    last_data_date = max((p["as_of"] for p in px.values()), default=None)
    if state.get("last_data_date") == last_data_date:
        issues.append(f"价格数据日期未更新（{last_data_date}），可能是美股休市或数据源延迟；以下报警沿用上次结果")
        # 无新数据时不重新评估，但保留上次报警（标记 repeat），避免同日重跑把报警覆盖掉
        alerts = [dict(a, repeat=True) for a in state.get("last_alerts", [])]
        new_state = state
    else:
        alerts, new_state = rules.evaluate(px, macro, cfg, state)
        new_state["last_data_date"] = last_data_date
        new_state["last_alerts"] = alerts
    level = rules.overall_level(alerts)
    run_date = (now + dt.timedelta(hours=8)).strftime("%Y-%m-%d")  # 以新加坡日期命名

    out = _clean({
        "run_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "run_date_sgt": run_date,
        "level": level, "alerts": alerts, "etf": px, "macro": macro,
        "framework": framework, "technical": technical,
        "state": new_state, "data_issues": issues,
        "sources": dict(getattr(fetch, "LAST_SOURCES", {})),
    })
    (root / "reports/daily").mkdir(parents=True, exist_ok=True)
    (root / "reports/latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / f"reports/daily/{run_date}.md").write_text(
        report.to_markdown(run_date, level, alerts, px, macro, new_state, issues,
                           framework=out["framework"], technical=out["technical"]), encoding="utf-8")
    state_p.write_text(json.dumps(_clean(new_state), ensure_ascii=False, indent=2), encoding="utf-8")

    # 只追加的点时快照
    row = {"run_at_utc": out["run_at_utc"], "level": level, "n_alerts": len(alerts)}
    for t, p in px.items():
        row |= {f"{t}_asof": p["as_of"], f"{t}_close": p["close"], f"{t}_dd": round(p["drawdown"], 5)}
    for k in ("vix", "vix_term_ratio", "hy_oas", "hy_oas_chg_20d", "hy_oas_vs_12m_mean",
              "t10y3m", "real_10y", "ust_10y", "nfci", "sahm", "usd_broad"):
        row[k] = out["macro"].get(k)
    fw, te = out["framework"], out["technical"]
    if fw:
        row |= {"fw_episode_start": fw["episode_start"], "fw_data_score": fw["data_score"],
                **{f"fw_{k}": fw["dims"][k] for k in ("price", "panic", "credit", "macro")}}
    if te:
        row |= {"fw_rsi14": round(te["rsi14"], 2) if te["rsi14"] is not None else None,
                "fw_vs_ma200": round(te["vs_ma200"], 5) if te["vs_ma200"] is not None else None,
                "fw_ma50_above_ma200": te["ma50_above_ma200"],
                "fw_days_above_ma200": te.get("days_above_ma200")}
    snap = root / "data/snapshots.csv"
    fields = list(row)
    if snap.exists():
        with snap.open(encoding="utf-8") as f:
            existing = next(csv.reader(f), [])
        fields = existing + [k for k in row if k not in existing]
        if fields != existing:  # 新增列时重写表头，旧行保留
            rows = list(csv.DictReader(snap.open(encoding="utf-8")))
            with snap.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
    new_file = not snap.exists()
    with snap.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        w.writerow(row)
    return out


def main():
    logging.basicConfig(level=logging.INFO)
    argparse.ArgumentParser(description="Daily market monitor").parse_args()
    out = run()
    print(f"{out['run_date_sgt']} level={out['level']} alerts={len(out['alerts'])} issues={len(out['data_issues'])}")


if __name__ == "__main__":
    main()
