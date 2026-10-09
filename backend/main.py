from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.analysis import router as analysis_router
from backend.api.platforms import router as platforms_router
from backend.api.report import router as report_router
from backend.api.upload import router as upload_router

app = FastAPI(
    title="EcomPilot AI 电商运营诊断助手",
    description="上传电商经营数据，自动生成指标看板、商品分层、运营诊断和日报。",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload_router)
app.include_router(analysis_router)
app.include_router(report_router)
app.include_router(platforms_router)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "EcomPilot AI",
        "status": "running",
        "docs": "/docs",
    }
