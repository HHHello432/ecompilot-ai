from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.analysis import router as analysis_router
from backend.api.console import router as console_router
from backend.api.console import ensure_started, stop_console
from backend.api.platforms import router as platforms_router
from backend.api.report import router as report_router
from backend.api.upload import router as upload_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时拉起托管的 RPA 控制台子服务（失败不影响主服务）
    try:
        ensure_started()
    except Exception:
        pass
    yield
    # 退出时回收子进程
    try:
        stop_console()
    except Exception:
        pass

app = FastAPI(
    title="EcomPilot AI 电商运营诊断助手",
    description="上传电商经营数据，自动生成指标看板、商品分层、运营诊断和日报。",
    version="1.0.0",
    lifespan=lifespan,
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
app.include_router(console_router)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "EcomPilot AI",
        "status": "running",
        "docs": "/docs",
    }
