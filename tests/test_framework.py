"""抄底框架（建仓执行计划）与技术位置的离线测试：合成数据，不访问网络。"""
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from monitor import indicators, rules

ROOT = Path(__file__).resolve().parents[1]
END = "2026-11-25"


def _fw():
    return yaml.safe_load((ROOT / "config/ips.yaml").read_text(encoding="utf-8"))["entry_plan"]["framework"]


def _close(recent_low=False):
    """前 300 天涨到 200（本轮高点），随后跌 20% 到 160，再反弹。recent_low=True 时低点只在 10 天前。"""
    idx = pd.bdate_range(end=END, periods=400)
    p = np.empty(400)
    p[:300] = np.linspace(100, 200, 300)
    if recent_low:
        p[300:390] = np.linspace(198, 160, 90)
        p[390:] = np.linspace(161, 176, 10)
    else:
        p[300:320] = np.linspace(198, 160, 20)
        p[320:] = np.linspace(161, 172, 80)
    return pd.Series(p, idx)


def _fred(vix_last=20.0, vix_peak=35.0, hy_last=3.8, icsa_last=200_000, with_icsa=True):
    idx = pd.bdate_range(end=END, periods=400)
    vix = pd.Series(18.0, idx)
    vix.iloc[315] = vix_peak
    vix.iloc[-1] = vix_last
    hy = pd.Series(3.0, idx)
    hy.iloc[310:330] = 4.2
    hy.iloc[330:] = hy_last
    out = {"VIXCLS": vix, "VXVCLS": pd.Series(22.0, idx), "BAMLH0A0HYM2": hy,
           "SAHMREALTIME": pd.Series(0.1, idx)}
    if with_icsa:
        w = pd.date_range(end="2026-11-21", periods=110, freq="W-SAT")
        ic = pd.Series(220_000.0, w)
        ic.iloc[-4:] = icsa_last
        out["ICSA"] = ic
    return out


def test_rsi_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    assert indicators.rsi(up) == 100.0
    assert indicators.rsi(up[::-1].reset_index(drop=True)) == 0.0
    assert indicators.rsi(pd.Series(5.0, range(30))) == 50.0
    rw = pd.Series(100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 300)))
    assert 0 < indicators.rsi(rw) < 100


def test_technical_rising_series():
    t = indicators.technical_indicators(pd.Series(np.linspace(100, 200, 300), pd.bdate_range(end=END, periods=300)))
    assert t["vs_ma200"] > 0 and t["ma50_above_ma200"] is True and t["dd_52w"] == 0


def test_days_above_ma200_counts_consecutive_closes():
    idx = pd.bdate_range(end=END, periods=300)
    p = np.r_[np.linspace(100, 200, 250), np.full(43, 150.0), np.linspace(190, 196, 7)]  # 最后 7 天重新站上
    t = indicators.technical_indicators(pd.Series(p, idx))
    assert t["days_above_ma200"] == 7
    down = indicators.technical_indicators(pd.Series(np.linspace(200, 100, 300), idx))
    assert down["days_above_ma200"] == 0


def test_framework_all_data_dims_pass():
    f = indicators.framework_indicators(_close(), _fred(), _fw())
    assert f["episode_high"] == 200 and f["episode_low"] == 160
    assert f["days_since_low"] == 80 and abs(f["rebound_from_low"] - 0.075) < 1e-9
    assert f["dims"] == {"price": 1, "panic": 1, "credit": 1, "macro": 1, "policy": None, "theme": None}
    assert f["data_score"] == 4 and not f["notes"]


def test_framework_each_dim_can_fail():
    fw = _fw()
    assert indicators.framework_indicators(_close(recent_low=True), _fred(), fw)["dims"]["price"] == 0  # 低点太近
    assert indicators.framework_indicators(_close(), _fred(vix_last=25.0), fw)["dims"]["panic"] == 0   # 仍倒挂
    assert indicators.framework_indicators(_close(), _fred(vix_peak=28.0), fw)["dims"]["panic"] == 0   # 没出现过恐慌
    assert indicators.framework_indicators(_close(), _fred(hy_last=4.0), fw)["dims"]["credit"] == 0    # 只回落 0.2pp
    assert indicators.framework_indicators(_close(), _fred(icsa_last=250_000), fw)["dims"]["macro"] == 0  # 申领同比上升


def test_framework_missing_data_is_flagged_not_guessed():
    fred = _fred(with_icsa=False)
    del fred["BAMLH0A0HYM2"]
    f = indicators.framework_indicators(_close(), fred, _fw())
    assert f["dims"]["credit"] is None and f["dims"]["macro"] == 1
    assert any("利差缺失" in n for n in f["notes"]) and any("申领" in n for n in f["notes"])
    assert f["data_score"] == 3  # 缺失的维度不计分


def test_ladder_state_drops_non_ladder_tickers():
    cfg = yaml.safe_load((ROOT / "config/ips.yaml").read_text(encoding="utf-8"))
    px = {"QQQM": indicators.price_indicators(_close(), 10)}
    _, st = rules.evaluate(px, indicators.macro_indicators(_fred()), cfg, {"ladder": {"QQQM": 0, "SMH": 2}})
    assert "SMH" not in st["ladder"]
