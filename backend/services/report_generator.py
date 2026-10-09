from __future__ import annotations

from typing import Any

import pandas as pd


def generate_daily_report(summary: dict[str, Any], products: pd.DataFrame) -> str:
    top_priority = products.sort_values("priority_score", ascending=False).head(3)
    budget_candidates = products[
        (products["roas"] >= 4) & (products["net_profit"] > 0) & (products["stock_turnover_risk"] != "高")
    ].sort_values("priority_score", ascending=False).head(3)
    high_refund = products[products["refund_rate"] >= 0.18].sort_values("refund_rate", ascending=False).head(3)
    loss_products = products[products["net_profit"] < 0].sort_values("gmv", ascending=False).head(3)
    low_ctr = products[(products["impressions"] > products["impressions"].median()) & (products["ctr"] < 0.02)].head(3)
    low_cvr = products[(products["visitors"] > 200) & (products["cvr"] < 0.02)].head(3)

    lines = [
        "今日运营总结：",
        "",
        "老板版结论：",
        f"1. 今日 GMV 为 {summary['total_gmv']:,.0f} 元，广告花费 {summary['total_ad_cost']:,.0f} 元，整体 ROAS 为 {summary['overall_roas']:.2f}。",
        f"2. 净利润为 {summary['net_profit']:,.0f} 元，客单价 {summary['average_order_value']:,.0f} 元，整体退货率 {summary['refund_rate']:.1%}。",
        f"3. 当前共分析 {summary['total_products']} 个商品，其中 {summary['profitable_product_count']} 个盈利，{summary['loss_product_count']} 个亏损。",
    ]

    if summary["net_profit"] < 0:
        lines.append("4. 当前不是继续拉流量的阶段，优先止损亏损商品，先把净利润拉回正区间。")
    elif not budget_candidates.empty:
        names = "、".join(budget_candidates["product_name"].tolist())
        lines.append(f"4. 可放量商品为 {names}，建议小步加预算，并以 24 小时净利润作为复盘口径。")
    else:
        lines.append("4. 暂无强放量商品，先稳定转化、利润和退货，再扩大投放。")

    lines.extend(["", "运营版拆解："])
    if not top_priority.empty:
        names = "、".join(top_priority["product_name"].tolist())
        lines.append(f"1. 明日主推优先级：{names}。")
    if not loss_products.empty:
        names = "、".join(loss_products["product_name"].tolist())
        lines.append(f"2. 止损队列：{names}。这些商品 GMV 不低但净利润为负，需要先降预算或暂停计划。")
    else:
        lines.append("2. 暂无明显亏损集中商品，继续监控广告花费和退货波动。")
    if not high_refund.empty:
        names = "、".join(high_refund["product_name"].tolist())
        lines.append(f"3. 售后风险：{names} 退货率偏高，优先复盘差评、客服记录、详情页承诺和尺码/质量问题。")
    elif not low_cvr.empty:
        names = "、".join(low_cvr["product_name"].tolist())
        lines.append(f"3. 转化风险：{names} 有访客但成交弱，优先优化详情页前三屏、评价和优惠门槛。")
    else:
        lines.append("3. 售后和转化暂无高优先级风险，保持日常监控。")

    actions = ["", "明日必做动作："]
    if not loss_products.empty:
        actions.append("1. 对亏损商品降预算 30%-50%，暂停 ROAS 低且净利润为负的计划。")
    elif not budget_candidates.empty:
        actions.append("1. 对 ROAS 高、净利润为正、库存安全的商品增加 10%-20% 预算。")
    else:
        actions.append("1. 暂不扩大预算，先确认转化和利润结构。")

    if not low_ctr.empty:
        actions.append("2. 优先优化曝光高但点击低商品的主图、标题和首屏价格表达。")
    elif not low_cvr.empty:
        actions.append("2. 优先优化访客高但转化低商品的详情页、评价露出和优惠门槛。")
    else:
        actions.append("2. 维持现有素材节奏，观察高优先级商品放量稳定性。")

    if not high_refund.empty:
        actions.append("3. 对高退货商品逐个复盘售后原因，必要时先降低投放，避免放大亏损。")
    else:
        actions.append("3. 每天固定复盘 CTR、CVR、ROAS、退货率和净利润，不只看 GMV。")

    return "\n".join(lines + actions)
