from __future__ import annotations

from html import escape

from fastapi import APIRouter, HTTPException, Response

from backend.database.db import load_analysis
from backend.models.schema import ReportResponse
from backend.services.pipeline import bundle_from_dict
from backend.services.report_generator import generate_daily_report

router = APIRouter(prefix="/api", tags=["report"])


@router.get("/report/{file_id}", response_model=ReportResponse)
def get_report(file_id: str) -> ReportResponse:
    bundle = _get_bundle(file_id)
    report = generate_daily_report(bundle.summary, bundle.products)
    return ReportResponse(file_id=file_id, report=report)


@router.get("/report/{file_id}/download")
def download_report(file_id: str) -> Response:
    bundle = _get_bundle(file_id)
    report = generate_daily_report(bundle.summary, bundle.products)
    return Response(
        content=report,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file_id}_daily_report.md"'},
    )


@router.get("/report/{file_id}/html")
def download_html_report(file_id: str) -> Response:
    bundle = _get_bundle(file_id)
    report = generate_daily_report(bundle.summary, bundle.products)
    html = _build_html_report(bundle.summary, report)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{file_id}_operation_report.html"'},
    )


def _get_bundle(file_id: str):
    payload = load_analysis(file_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="没有找到这份分析，请重新上传数据。")
    return bundle_from_dict(payload)


def _build_html_report(summary: dict, report: str) -> str:
    lines = "".join(f"<p>{escape(line)}</p>" for line in report.splitlines() if line.strip())
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>EcomPilot AI 运营日报</title>
  <style>
    body {{ margin: 0; background: #f5f7fb; color: #1f2533; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; }}
    main {{ max-width: 920px; margin: 32px auto; background: #fff; border: 1px solid #e7eaf0; border-radius: 10px; padding: 32px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    .meta {{ color: #667085; margin-bottom: 24px; }}
    .kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }}
    .kpi {{ border: 1px solid #e7eaf0; border-radius: 8px; padding: 12px; background: #fafcff; }}
    .kpi span {{ color: #667085; font-size: 12px; }}
    .kpi strong {{ display: block; margin-top: 6px; font-size: 20px; }}
    .report {{ line-height: 1.85; font-size: 15px; }}
    .report p {{ margin: 0 0 8px; }}
  </style>
</head>
<body>
  <main>
    <h1>EcomPilot AI 运营日报</h1>
    <div class="meta">自动生成，用于老板汇报和运营复盘</div>
    <section class="kpis">
      <div class="kpi"><span>GMV</span><strong>¥{summary.get("total_gmv", 0):,.0f}</strong></div>
      <div class="kpi"><span>净利润</span><strong>¥{summary.get("net_profit", 0):,.0f}</strong></div>
      <div class="kpi"><span>ROAS</span><strong>{summary.get("overall_roas", 0):.2f}</strong></div>
      <div class="kpi"><span>退货率</span><strong>{summary.get("refund_rate", 0):.1%}</strong></div>
    </section>
    <section class="report">{lines}</section>
  </main>
</body>
</html>"""
