from __future__ import annotations

import pandas as pd


SEGMENT_LABELS = {
    "hit": "爆款商品",
    "potential": "潜力商品",
    "traffic": "引流商品",
    "profit": "利润商品",
    "slow": "滞销商品",
    "return_risk": "高退货风险商品",
    "loss": "亏损商品",
    "normal": "观察商品",
}


def classify_products(metrics_df: pd.DataFrame) -> pd.DataFrame:
    df = metrics_df.copy()

    ctr_high = df["ctr"] >= max(0.03, df["ctr"].quantile(0.65))
    cvr_medium = df["cvr"].between(0.02, 0.05, inclusive="left")
    cvr_good = df["cvr"] >= 0.05
    enough_stock = df["stock"] >= 30
    high_margin = df["gross_margin"] >= max(0.25, df["gross_margin"].quantile(0.65))
    high_refund = df["refund_rate"] >= max(0.18, df["refund_rate"].quantile(0.8))
    low_exposure = df["impressions"] <= max(1000, df["impressions"].quantile(0.35))

    df["risk_flags"] = [[] for _ in range(len(df))]
    df["opportunity_flags"] = [[] for _ in range(len(df))]

    for idx, row in df.iterrows():
        risks: list[str] = []
        opportunities: list[str] = []

        if row["net_profit"] < 0:
            risks.append("净利润为负")
        if row["refund_rate"] >= 0.18:
            risks.append("退货率偏高")
        if row["ctr"] < 0.015 and row["impressions"] > 3000:
            risks.append("曝光高但点击弱")
        if row["cvr"] < 0.02 and row["visitors"] > 200:
            risks.append("访客有了但转化弱")
        if row["stock_turnover_risk"] == "高":
            risks.append("库存周转风险高")

        if row["roas"] >= 5 and row["net_profit"] > 0:
            opportunities.append("可加预算放量")
        if row["gross_margin"] >= 0.3 and row["orders"] >= 10:
            opportunities.append("利润表现好")
        if row["ctr"] >= 0.03 and row["cvr"] < 0.04:
            opportunities.append("流量兴趣强，优先优化转化")

        df.at[idx, "risk_flags"] = risks
        df.at[idx, "opportunity_flags"] = opportunities

    conditions = [
        (df["roas"] > 5) & cvr_good & (df["net_profit"] > 0) & enough_stock,
        high_refund & (df["gmv"] > df["gmv"].median()),
        (df["net_profit"] < 0) & (df["gmv"] > 0),
        low_exposure & (df["ctr"] < 0.02) & (df["cvr"] < 0.02) & (df["stock"] >= 100),
        ctr_high & cvr_medium & (df["net_profit"] >= -df["ad_cost"] * 0.2),
        ctr_high & (df["net_profit"] <= df["gmv"] * 0.08),
        high_margin & (df["net_profit"] > 0),
    ]
    choices = [
        SEGMENT_LABELS["hit"],
        SEGMENT_LABELS["return_risk"],
        SEGMENT_LABELS["loss"],
        SEGMENT_LABELS["slow"],
        SEGMENT_LABELS["potential"],
        SEGMENT_LABELS["traffic"],
        SEGMENT_LABELS["profit"],
    ]

    df["segment"] = pd.Series(pd.NA, index=df.index)
    for condition, choice in zip(conditions, choices):
        df.loc[df["segment"].isna() & condition, "segment"] = choice
    df["segment"] = df["segment"].fillna(SEGMENT_LABELS["normal"])
    df["priority_score"] = _priority_score(df).round(2)
    return df


def segment_counts(classified_df: pd.DataFrame) -> dict[str, int]:
    counts = classified_df["segment"].value_counts().to_dict()
    return {label: int(counts.get(label, 0)) for label in SEGMENT_LABELS.values()}


def _priority_score(df: pd.DataFrame) -> pd.Series:
    profit_score = df["net_profit"].rank(pct=True) * 35
    roas_score = df["roas"].rank(pct=True) * 25
    gmv_score = df["gmv"].rank(pct=True) * 20
    risk_penalty = (df["refund_rate"].rank(pct=True) * 10) + (df["stock_turnover_risk"].map({"低": 0, "中": 5, "高": 10}))
    return (profit_score + roas_score + gmv_score + 20 - risk_penalty).clip(lower=0, upper=100)
