"""离线测试：用合成数据跑完整流程，不访问网络。运行：python -m pytest -q"""
import datetime as dt
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from monitor import indicators, rules, run

ROOT = Path(__file__).resolve().parents[1]


def _prices(end="2026-11-17", crash=0.0, n=900, seed=0):
    idx = pd.bdate_range(end=end, periods=n)
    rng = np.random.default_rng(seed)
    p = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n)))
    if crash:  # 最后 120 天线性下跌 crash 比例
        p[-120:] = p[-121] * np.linspace(1, 1 - crash, 120)
    return pd.Series(p, idx)


def _fred(end="2026-11-17", hy_jump=0.0, sahm=0.2):
    idx = pd.bdate_range(end=end, periods=400)
    hy = pd.Series(3.0, idx)
    if hy_jump:
        hy.iloc[-30:] = 3.0 + hy_jump
    const = lambda v: pd.Series(v, idx)  # noqa: E731
    return {"VIXCLS": const(18.0), "VXVCLS": const(20.0), "BAMLH0A0HYM2": hy, "T10Y3M": const(0.5),
            "DFII10": const(1.8), "DGS10": const(4.2), "NFCI": const(-0.4),
            "SAHMREALTIME": const(sahm), "DTWEXBGS": const(120.0)}


def _cfg():
    import yaml
    return yaml.safe_load((ROOT / "config/ips.yaml").read_text(encoding="utf-8"))


def test_month_end_trend_uses_completed_month_only():
    s = _prices()
    p = indicators.price_indicators(s, 10)
    assert p["last_month_end"] == "2026-10"  # 11 月未走完，不参与趋势判断
    assert -1 < p["drawdown"] <= 0


def test_ladder_fires_once_then_silent():
    cfg = _cfg()
    px = {"QQQM": indicators.price_indicators(_prices(crash=0.25), 10)}
    macro = indicators.macro_indicators(_fred())
    a1, st = rules.evaluate(px, macro, cfg, {})
    assert any(a["code"].startswith("LADDER_QQQM") for a in a1)
    a2, _ = rules.evaluate(px, macro, cfg, st)
    assert not any(a["code"].startswith("LADDER_QQQM") for a in a2)  # 同一档不重复报警


def test_two_key_requires_both():
    cfg = _cfg()
    crash = {"QQQ": indicators.price_indicators(_prices(crash=0.3), 10)}
    calm = indicators.macro_indicators(_fred())
    stress = indicators.macro_indicators(_fred(hy_jump=1.5))
    a, _ = rules.evaluate(crash, calm, cfg, {})
    assert not any(x["code"] == "TWO_KEY_ON" for x in a)
    assert any(x["code"] == "ONE_KEY" for x in a)
    a, st = rules.evaluate(crash, stress, cfg, {})
    assert any(x["code"] == "TWO_KEY_ON" for x in a) and st["two_key_active"]


def test_full_run_offline(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config/ips.yaml", tmp_path / "config/ips.yaml")
    (tmp_path / "data").mkdir()

    def fake_fetch(cfg):
        u = cfg["universe"]
        tick = u["core"] + u["satellite"] + u["reference"]
        return _fred(), {t: _prices(seed=i) for i, t in enumerate(tick)}, []

    now = dt.datetime(2026, 11, 17, 23, 37, tzinfo=dt.timezone.utc)
    out = run.run(tmp_path, fetcher=fake_fetch, now=now)
    assert out["run_date_sgt"] == "2026-11-18"
    assert (tmp_path / "reports/latest.json").exists()
    assert (tmp_path / "reports/daily/2026-11-18.md").exists()
    json.loads((tmp_path / "reports/latest.json").read_text())
    # 第二次运行：数据日期未变 → 不重复报警，并记录数据问题
    out2 = run.run(tmp_path, fetcher=fake_fetch, now=now + dt.timedelta(days=1))
    assert all(a.get("repeat") for a in out2["alerts"]) and any("未更新" in i for i in out2["data_issues"])
    assert len(out2["alerts"]) == len(out["alerts"])  # 同日重跑不会吞掉报警
    assert len((tmp_path / "data/snapshots.csv").read_text().strip().splitlines()) == 3
