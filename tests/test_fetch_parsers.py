"""数据源解析函数的离线测试（样本格式取自各官方源的真实返回）。"""
import json

import pandas as pd

from monitor import fetch

TREASURY = (
    'Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"\n'
    "11/17/2026,4.09,4.24,4.26,4.33,4.32,4.41,4.60,4.80,4.84,4.90,4.99,5.10,5.50,5.46\n"
    "11/16/2026,4.10,4.19,4.27,4.34,4.33,4.42,4.62,4.83,4.87,4.93,5.01,5.12,5.51,5.46\n"
)
REAL = 'Date,"5 YR","7 YR","10 YR","20 YR","30 YR"\n11/17/2026,2.64,2.76,2.89,3.14,3.28\n'
CBOE = "DATE,OPEN,HIGH,LOW,CLOSE\n11/14/2026,15.1,16.0,14.9,15.5\n11/17/2026,15.5,17.2,15.2,16.8\n"


def test_treasury_parse_and_spread():
    df = fetch.parse_treasury([TREASURY])
    assert df.index.is_monotonic_increasing
    assert df.loc["2026-11-17", "10 YR"] == 5.10
    assert round(df.loc["2026-11-17", "10 YR"] - df.loc["2026-11-17", "3 MO"], 2) == 0.77
    assert fetch.parse_treasury([REAL]).loc["2026-11-17", "10 YR"] == 2.89


def test_cboe_parse():
    s = fetch.parse_cboe(CBOE)
    assert s.iloc[-1] == 16.8 and s.index[-1] == pd.Timestamp("2026-11-17")


def test_fred_parsers():
    s = fetch.parse_fred_csv("observation_date,VIXCLS\n2026-11-16,15.0\n2026-11-17,.\n")
    assert len(s) == 1
    api = json.dumps({"observations": [{"date": "2026-11-16", "value": "3.10"}, {"date": "2026-11-17", "value": "."}]})
    assert fetch.parse_fred_api(api).iloc[-1] == 3.10


def test_bls_and_sahm():
    data = [{"year": "2026", "period": f"M{m:02d}", "value": str(v)}
            for m, v in zip(range(1, 11), [4.0, 4.0, 4.0, 4.1, 4.1, 4.2, 4.4, 4.6, 4.7, 4.8])]
    data += [{"year": "2025", "period": f"M{m:02d}", "value": "4.0"} for m in range(1, 13)]
    u = fetch.parse_bls(json.dumps({"Results": {"series": [{"data": data}]}}))
    assert u.index[-1] == pd.Timestamp("2026-10-01")
    sahm = fetch.sahm_from_unrate(u)
    # 2026-10 的 3 个月均值 = 4.7，此前 12 个月 3 个月均值最低 = 4.0 → 0.7
    assert round(sahm.iloc[-1], 2) == 0.70
