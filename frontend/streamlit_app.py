from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.ai_diagnosis import answer_question
from backend.services.data_cleaner import DataValidationError
from backend.services.pipeline import analyze_dataframe
from backend.services.report_generator import generate_daily_report


st.set_page_config(page_title="EcomPilot AI", page_icon="E", layout="wide")

st.markdown(
    """
    <style>
    .block-container { padding-top: 1.25rem; padding-bottom: 2rem; }
    [data-testid="stMetricValue"] { font-size: 1.35rem; }
    .stTabs [data-baseweb="tab-list"] { gap: 0.35rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("EcomPilot AI 电商运营诊断助手")

sample_path = ROOT / "sample_data" / "ecommerce_demo.csv"
uploaded = st.sidebar.file_uploader("上传数据", type=["csv", "xlsx", "xls"])
use_demo = st.sidebar.button("载入演示数据", use_container_width=True)

try:
    if uploaded is not None and not use_demo:
        if uploaded.name.endswith((".xlsx", ".xls")):
            raw_df = pd.read_excel(uploaded)
        else:
            raw_df = pd.read_csv(uploaded, encoding="utf-8-sig")
        bundle = analyze_dataframe("streamlit_upload", raw_df)
    else:
        raw_df = pd.read_csv(sample_path, encoding="utf-8-sig")
        bundle = analyze_dataframe("demo", raw_df)
except (DataValidationError, UnicodeDecodeError) as exc:
    st.error(str(exc))
    st.stop()

summary = bundle.summary
products = bundle.products
diagnosis = bundle.diagnosis
report = generate_daily_report(summary, products)

if bundle.warnings:
    st.warning("；".join(bundle.warnings))

kpi_cols = st.columns(6)
kpi_cols[0].metric("GMV", f"¥{summary['total_gmv']:,.0f}")
kpi_cols[1].metric("净利润", f"¥{summary['net_profit']:,.0f}")
kpi_cols[2].metric("ROAS", f"{summary['overall_roas']:.2f}")
kpi_cols[3].metric("CTR", f"{summary['overall_ctr']:.1%}")
kpi_cols[4].metric("CVR", f"{summary['overall_cvr']:.1%}")
kpi_cols[5].metric("退货率", f"{summary['refund_rate']:.1%}")

tab_overview, tab_products, tab_diagnosis, tab_chat, tab_report = st.tabs(["经营看板", "商品分层", "运营诊断", "AI 问答", "日报导出"])

with tab_overview:
    left, right = st.columns([1.25, 1])
    with left:
        chart_df = products.sort_values("gmv", ascending=False).head(12)
        fig = px.bar(chart_df, x="product_name", y="gmv", color="segment", title="商品 GMV 排名")
        fig.update_layout(xaxis_title="", yaxis_title="GMV", legend_title="")
        st.plotly_chart(fig, use_container_width=True)
    with right:
        segment_df = pd.DataFrame(
            [{"segment": key, "count": value} for key, value in summary["segment_counts"].items() if value > 0]
        )
        fig = px.pie(segment_df, values="count", names="segment", hole=0.45, title="商品分层占比")
        st.plotly_chart(fig, use_container_width=True)

    scatter = px.scatter(
        products,
        x="roas",
        y="net_profit",
        size="gmv",
        color="segment",
        hover_name="product_name",
        title="ROAS / 净利润矩阵",
    )
    scatter.update_layout(xaxis_title="ROAS", yaxis_title="净利润")
    st.plotly_chart(scatter, use_container_width=True)

with tab_products:
    table_source = products.copy()
    table_source["ctr_pct"] = table_source["ctr"] * 100
    table_source["cvr_pct"] = table_source["cvr"] * 100
    table_source["refund_rate_pct"] = table_source["refund_rate"] * 100
    display_columns = {
        "product_name": "商品名",
        "segment": "商品分层",
        "priority_score": "优先级",
        "gmv": "GMV",
        "ctr_pct": "CTR",
        "cvr_pct": "CVR",
        "roas": "ROAS",
        "refund_rate_pct": "退货率",
        "net_profit": "净利润",
        "stock_turnover_risk": "库存风险",
        "risk_flags": "风险",
        "opportunity_flags": "机会",
    }
    table = table_source[list(display_columns.keys())].rename(columns=display_columns).sort_values("优先级", ascending=False)
    st.dataframe(
        table,
        use_container_width=True,
        hide_index=True,
        column_config={
            "CTR": st.column_config.ProgressColumn("CTR", format="%.1f%%", min_value=0, max_value=20),
            "CVR": st.column_config.ProgressColumn("CVR", format="%.1f%%", min_value=0, max_value=20),
            "退货率": st.column_config.ProgressColumn("退货率", format="%.1f%%", min_value=0, max_value=50),
            "GMV": st.column_config.NumberColumn("GMV", format="¥%.0f"),
            "净利润": st.column_config.NumberColumn("净利润", format="¥%.0f"),
        },
    )
    csv = products.to_csv(index=False).encode("utf-8-sig")
    st.download_button("下载商品分析表", csv, "ecompilot_product_analysis.csv", "text/csv", use_container_width=True)

with tab_diagnosis:
    st.subheader("核心判断")
    st.write(diagnosis["summary"])
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("风险提醒")
        for item in diagnosis["alerts"] or ["暂未发现高优先级风险。"]:
            st.markdown(f"- {item}")
    with col_b:
        st.subheader("优化动作")
        for item in diagnosis["suggestions"]:
            st.markdown(f"- {item}")

with tab_chat:
    default_question = "哪些商品最应该加广告预算？"
    question = st.text_input("输入问题", value=default_question)
    if st.button("生成回答", type="primary", use_container_width=True):
        st.markdown(answer_question(question, summary, products))

with tab_report:
    st.text_area("日报", value=report, height=360)
    st.download_button("下载日报", report.encode("utf-8"), "ecompilot_daily_report.md", "text/markdown", use_container_width=True)
