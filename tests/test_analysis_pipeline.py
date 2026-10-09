from __future__ import annotations

import pandas as pd

from backend.services.pipeline import analyze_dataframe


def test_pipeline_computes_summary_and_segments() -> None:
    df = pd.DataFrame(
        [
            {
                "商品名": "爆款A",
                "曝光量": 10000,
                "点击量": 500,
                "访客数": 450,
                "下单数": 45,
                "支付金额": 9000,
                "广告花费": 1000,
                "成本": 3500,
                "退货金额": 200,
                "库存": 120,
            },
            {
                "商品名": "亏损B",
                "曝光量": 20000,
                "点击量": 220,
                "访客数": 210,
                "下单数": 8,
                "支付金额": 1200,
                "广告花费": 1800,
                "成本": 900,
                "退货金额": 120,
                "库存": 500,
            },
        ]
    )

    bundle = analyze_dataframe("test", df)

    assert bundle.summary["total_gmv"] == 10200
    assert bundle.summary["net_profit"] == 2680
    assert len(bundle.products) == 2
    assert "segment" in bundle.products.columns
    assert bundle.diagnosis["suggestions"]


def test_duplicate_products_are_merged() -> None:
    df = pd.DataFrame(
        [
            {"商品名": "A", "曝光量": 100, "点击量": 10, "访客数": 9, "下单数": 1, "支付金额": 100, "广告花费": 10, "成本": 40, "退货金额": 0, "库存": 10},
            {"商品名": "A", "曝光量": 200, "点击量": 20, "访客数": 18, "下单数": 2, "支付金额": 200, "广告花费": 20, "成本": 80, "退货金额": 0, "库存": 20},
        ]
    )

    bundle = analyze_dataframe("test", df)

    assert len(bundle.products) == 1
    assert bundle.products.iloc[0]["gmv"] == 300
    assert bundle.warnings
