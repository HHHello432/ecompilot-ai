from __future__ import annotations

import math

import numpy as np
import pandas as pd


def compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()

    result["ctr"] = _safe_divide(result["clicks"], result["impressions"])
    result["cvr"] = _safe_divide(result["orders"], result["visitors"])
    result["gmv"] = result["payment_amount"]
    result["roas"] = _safe_divide(result["payment_amount"], result["ad_cost"])
    result["average_order_value"] = _safe_divide(result["payment_amount"], result["orders"])
    result["gross_profit"] = result["payment_amount"] - result["cost"]
    result["net_profit"] = result["payment_amount"] - result["cost"] - result["ad_cost"] - result["refund_amount"]
    result["refund_rate"] = _safe_divide(result["refund_amount"], result["payment_amount"])
    result["gross_margin"] = _safe_divide(result["gross_profit"], result["payment_amount"])
    result["net_margin"] = _safe_divide(result["net_profit"], result["payment_amount"])
    result["stock_turnover_risk"] = _stock_risk(result)

    rounded = result.copy()
    for column in [
        "ctr",
        "cvr",
        "roas",
        "average_order_value",
        "gross_profit",
        "net_profit",
        "refund_rate",
        "gross_margin",
        "net_margin",
    ]:
        rounded[column] = rounded[column].replace([np.inf, -np.inf], 0).fillna(0).round(4)
    return rounded


def summarize(metrics_df: pd.DataFrame) -> dict[str, float | int | list[str]]:
    total_gmv = float(metrics_df["gmv"].sum())
    total_ad_cost = float(metrics_df["ad_cost"].sum())
    total_cost = float(metrics_df["cost"].sum())
    total_refund = float(metrics_df["refund_amount"].sum())
    total_orders = int(metrics_df["orders"].sum())
    total_visitors = int(metrics_df["visitors"].sum())
    total_clicks = int(metrics_df["clicks"].sum())
    total_impressions = int(metrics_df["impressions"].sum())
    net_profit = float(metrics_df["net_profit"].sum())

    return {
        "total_gmv": round(total_gmv, 2),
        "total_ad_cost": round(total_ad_cost, 2),
        "total_cost": round(total_cost, 2),
        "total_refund": round(total_refund, 2),
        "total_orders": total_orders,
        "total_products": int(len(metrics_df)),
        "overall_ctr": round(_scalar_divide(total_clicks, total_impressions), 4),
        "overall_cvr": round(_scalar_divide(total_orders, total_visitors), 4),
        "overall_roas": round(_scalar_divide(total_gmv, total_ad_cost), 4),
        "average_order_value": round(_scalar_divide(total_gmv, total_orders), 2),
        "gross_profit": round(total_gmv - total_cost, 2),
        "net_profit": round(net_profit, 2),
        "refund_rate": round(_scalar_divide(total_refund, total_gmv), 4),
        "profitable_product_count": int((metrics_df["net_profit"] > 0).sum()),
        "loss_product_count": int((metrics_df["net_profit"] < 0).sum()),
    }


def _safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.replace(0, np.nan)
    return (numerator / denominator).replace([np.inf, -np.inf], 0).fillna(0)


def _scalar_divide(numerator: float, denominator: float) -> float:
    if denominator == 0 or math.isclose(denominator, 0):
        return 0
    return float(numerator / denominator)


def _stock_risk(df: pd.DataFrame) -> pd.Series:
    sale_speed = df["orders"].replace(0, np.nan)
    stock_days = df["stock"] / sale_speed
    risk = np.select(
        [
            (df["stock"] >= 300) & (df["orders"] <= 5),
            stock_days >= 60,
            stock_days.between(30, 60, inclusive="left"),
        ],
        ["高", "高", "中"],
        default="低",
    )
    return pd.Series(risk, index=df.index)
