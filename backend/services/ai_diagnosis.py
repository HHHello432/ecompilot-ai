from __future__ import annotations

import os
from typing import Any

import pandas as pd


def generate_diagnosis(summary: dict[str, Any], products: pd.DataFrame) -> dict[str, Any]:
    deterministic = _rule_based_diagnosis(summary, products)
    llm_summary = _try_llm_summary(summary, products)
    if llm_summary:
        deterministic["summary"] = llm_summary
        deterministic["source"] = "llm"
    return deterministic


def answer_question(question: str, summary: dict[str, Any], products: pd.DataFrame) -> str:
    question = question.strip()
    if not question:
        return "请先输入一个具体问题。"

    llm_answer = _try_llm_answer(question, summary, products)
    if llm_answer:
        return llm_answer

    q = question.lower()
    if any(keyword in question for keyword in ["加广告", "预算", "投放", "放量"]):
        candidates = products[(products["roas"] >= 4) & (products["net_profit"] > 0)].sort_values("priority_score", ascending=False)
        if candidates.empty:
            return "暂时没有特别适合加广告预算的商品。建议先优化净利润为负或转化偏低的商品，再放量。"
        return _format_product_list("最适合加广告预算的是", candidates.head(5), "ROAS 高、净利润为正，放量风险相对可控")

    if any(keyword in question for keyword in ["亏", "不赚钱", "亏钱", "利润"]):
        candidates = products[(products["gmv"] > 0) & (products["net_profit"] < 0)].sort_values("gmv", ascending=False)
        if candidates.empty:
            return "当前没有 GMV 为正但净利润为负的商品。下一步重点看退货率和库存周转。"
        return _format_product_list("GMV 高但实际亏钱的商品是", candidates.head(5), "广告、成本或退货正在吃掉利润")

    if any(keyword in question for keyword in ["最大问题", "核心问题", "问题"]):
        return _biggest_problem(summary, products)

    if any(keyword in question for keyword in ["日报", "汇报", "老板"]):
        from backend.services.report_generator import generate_daily_report

        return generate_daily_report(summary, products)

    if "sku" in q or "主推" in question or "爆款" in question:
        candidates = products[products["segment"].isin(["爆款商品", "潜力商品", "利润商品"])].sort_values("priority_score", ascending=False)
        if candidates.empty:
            return "当前还没有明显的主推 SKU。建议先从高 CTR 但低 CVR 的商品做详情页和价格优化。"
        return _format_product_list("优先主推这些 SKU", candidates.head(6), "综合了 ROAS、净利润、GMV 和库存风险")

    return (
        _biggest_problem(summary, products)
        + "\n\n你也可以继续追问“哪些商品该加广告预算”“哪些商品 GMV 高但亏钱”“帮我生成日报”。"
    )


def _rule_based_diagnosis(summary: dict[str, Any], products: pd.DataFrame) -> dict[str, Any]:
    suggestions: list[str] = []
    alerts: list[str] = []

    if summary["overall_roas"] < 2.5:
        alerts.append("整体 ROAS 偏低，广告投产需要优先排查。")
        suggestions.append("暂停 ROAS 低于 2 的商品投放，把预算转向净利润为正的 SKU。")
    elif summary["overall_roas"] >= 5:
        suggestions.append("整体 ROAS 健康，可以筛选高利润商品做小步放量。")

    if summary["net_profit"] < 0:
        alerts.append("店铺整体净利润为负，GMV 增长没有转化成真实利润。")
        suggestions.append("按商品拆解成本、广告花费和退货金额，先止损亏损商品。")

    high_refund = products[products["refund_rate"] >= 0.18].sort_values("refund_rate", ascending=False)
    if not high_refund.empty:
        names = "、".join(high_refund.head(3)["product_name"].tolist())
        alerts.append(f"{names} 退货率偏高，可能存在质量、尺码、详情页描述或预期管理问题。")
        suggestions.append("优先检查高退货商品的差评、客服记录和详情页承诺。")

    low_ctr = products[(products["impressions"] > products["impressions"].median()) & (products["ctr"] < 0.02)]
    if not low_ctr.empty:
        names = "、".join(low_ctr.head(3)["product_name"].tolist())
        suggestions.append(f"{names} 曝光不低但点击弱，先优化主图、标题和价格锚点。")

    low_cvr = products[(products["visitors"] > 200) & (products["cvr"] < 0.02)]
    if not low_cvr.empty:
        names = "、".join(low_cvr.head(3)["product_name"].tolist())
        suggestions.append(f"{names} 有访客但转化弱，重点排查评价、详情页前三屏和优惠门槛。")

    high_stock = products[products["stock_turnover_risk"] == "高"].sort_values("stock", ascending=False)
    if not high_stock.empty:
        names = "、".join(high_stock.head(3)["product_name"].tolist())
        alerts.append(f"{names} 库存周转风险高，不宜继续盲目补货。")

    top = products.sort_values("priority_score", ascending=False).head(3)["product_name"].tolist()
    if top:
        suggestions.append("明日主推优先级：" + "、".join(top) + "。")

    summary_text = _biggest_problem(summary, products)
    return {
        "summary": summary_text,
        "alerts": alerts[:5],
        "suggestions": _dedupe(suggestions)[:8],
        "source": "rules",
    }


def _biggest_problem(summary: dict[str, Any], products: pd.DataFrame) -> str:
    if summary["net_profit"] < 0:
        return f"当前最大问题是 GMV 没有转成利润：总 GMV {summary['total_gmv']:,.0f} 元，但净利润 {summary['net_profit']:,.0f} 元。先处理亏损 SKU，比继续拉流量更重要。"

    high_refund_count = int((products["refund_rate"] >= 0.18).sum())
    if high_refund_count:
        return f"当前最大问题是退货风险：有 {high_refund_count} 个商品退货率偏高，会直接吞掉利润并影响后续投放效率。"

    low_roas_count = int(((products["ad_cost"] > 0) & (products["roas"] < 2.5)).sum())
    if low_roas_count:
        return f"当前最大问题是投放结构：{low_roas_count} 个商品 ROAS 偏低，广告花费没有带来足够支付金额。"

    low_cvr_count = int(((products["visitors"] > 200) & (products["cvr"] < 0.02)).sum())
    if low_cvr_count:
        return f"当前最大问题是转化效率：{low_cvr_count} 个商品有访客但成交弱，需要先补详情页、评价和优惠策略。"

    return f"当前店铺整体比较健康：总 GMV {summary['total_gmv']:,.0f} 元，整体 ROAS {summary['overall_roas']:.2f}，净利润 {summary['net_profit']:,.0f} 元。下一步应围绕高优先级商品做稳步放量。"


def _try_llm_summary(summary: dict[str, Any], products: pd.DataFrame) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        compact = products.sort_values("priority_score", ascending=False).head(8)[
            ["product_name", "segment", "gmv", "roas", "ctr", "cvr", "net_profit", "refund_rate", "stock"]
        ].to_dict(orient="records")
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[
                {"role": "system", "content": "你是资深电商运营专家，回答要像给老板汇报，短、准、能执行。"},
                {"role": "user", "content": f"店铺汇总：{summary}\n商品数据：{compact}\n请给出当前店铺最重要的诊断总结。"},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content
    except Exception:
        return None


def _try_llm_answer(question: str, summary: dict[str, Any], products: pd.DataFrame) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        compact = products.sort_values("priority_score", ascending=False).head(12)[
            ["product_name", "segment", "gmv", "roas", "ctr", "cvr", "net_profit", "refund_rate", "risk_flags", "opportunity_flags"]
        ].to_dict(orient="records")
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[
                {"role": "system", "content": "你是电商运营诊断助手。只根据给定数据回答，给可执行动作，不要泛泛而谈。"},
                {"role": "user", "content": f"问题：{question}\n店铺汇总：{summary}\n商品数据：{compact}"},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content
    except Exception:
        return None


def _format_product_list(prefix: str, rows: pd.DataFrame, reason: str) -> str:
    lines = [f"{prefix}："]
    for _, row in rows.iterrows():
        lines.append(
            f"{row['product_name']}：{row['segment']}，GMV {row['gmv']:,.0f} 元，ROAS {row['roas']:.2f}，净利润 {row['net_profit']:,.0f} 元。"
        )
    lines.append(f"判断原因：{reason}。")
    return "\n".join(lines)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result
