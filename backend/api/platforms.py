from __future__ import annotations

import os
import re
import time
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query

from backend.database.db import save_analysis
from backend.models.schema import PlatformAuthStartResponse, PlatformCallbackResponse, PlatformListResponse, PlatformSyncResponse
from backend.platforms.auth import begin_authorization, complete_authorization, create_sync_job
from backend.platforms.lingxing import LingxingClient, build_canonical_dataframe
from backend.platforms.registry import get_platform_status, list_platform_status
from backend.services.data_cleaner import DataValidationError
from backend.services.pipeline import analyze_dataframe

router = APIRouter(prefix="/api/platforms", tags=["platforms"])


@router.get("", response_model=PlatformListResponse)
def list_platforms() -> PlatformListResponse:
    return PlatformListResponse(platforms=[asdict(platform) for platform in list_platform_status()])


@router.post("/{platform_id}/auth/start", response_model=PlatformAuthStartResponse)
def start_platform_auth(platform_id: str) -> PlatformAuthStartResponse:
    result = begin_authorization(platform_id)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=result["message"])
    return PlatformAuthStartResponse(platform_id=platform_id, **result)


@router.get("/{platform_id}/callback", response_model=PlatformCallbackResponse)
def platform_auth_callback(
    platform_id: str,
    code: str = Query(""),
    state: str = Query(""),
    shop_id: str = Query(""),
) -> PlatformCallbackResponse:
    if not code or not state:
        return PlatformCallbackResponse(platform_id=platform_id, status="missing_code", message="授权回调缺少 code 或 state。")
    result = complete_authorization(platform_id, code, state, shop_id)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail=result["message"])
    return PlatformCallbackResponse(platform_id=platform_id, **result)


@router.post("/{platform_id}/sync", response_model=PlatformSyncResponse)
def sync_platform(platform_id: str) -> PlatformSyncResponse:
    platform = get_platform_status(platform_id)
    if platform is None:
        raise HTTPException(status_code=404, detail="不支持的平台")
    job = create_sync_job(platform_id)
    return PlatformSyncResponse(
        platform_id=platform.id,
        status=job["status"],
        message=job["message"],
        job_id=job["job_id"],
    )


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.post("/lingxing/pull")
def pull_lingxing(
    start_date: str | None = Query(None, description="开始日期 YYYY-MM-DD，默认最近 7 天"),
    end_date: str | None = Query(None, description="结束日期 YYYY-MM-DD，默认今天"),
    cookie: str | None = Query(None, description="覆盖 .env 的 LINGXING_COOKIE"),
) -> dict:
    """从领星 ERP 拉取数据并生成标准分析。

    流程：构造 LingxingClient -> build_canonical_dataframe ->
    analyze_dataframe -> save_analysis。
    前端拿到 file_id 后可调 /api/analysis/{file_id} 等接口。
    """
    # 1) 参数校验（400）
    if start_date and not _DATE_RE.match(start_date):
        raise HTTPException(status_code=400, detail="start_date 格式应为 YYYY-MM-DD")
    if end_date and not _DATE_RE.match(end_date):
        raise HTTPException(status_code=400, detail="end_date 格式应为 YYYY-MM-DD")
    if not end_date:
        end_date = time.strftime("%Y-%m-%d")
    if not start_date:
        start = time.localtime(time.time() - 6 * 86400)
        start_date = time.strftime("%Y-%m-%d", start)

    # 2) Cookie 来源（override > env）
    cookie_value = cookie or os.getenv("LINGXING_COOKIE")
    if not cookie_value:
        raise HTTPException(
            status_code=400,
            detail="缺少领星 Cookie：请设置 .env 的 LINGXING_COOKIE 或请求参数 cookie。",
        )

    # 3) 拉取 + 分析（502 拉取失败 / 500 其他）
    try:
        client = LingxingClient(cookie_value)
        df = build_canonical_dataframe(client, start_date, end_date)
        file_id = f"lingxing_{int(time.time())}"
        bundle = analyze_dataframe(file_id, df)
        save_analysis(file_id, bundle.to_dict())
    except DataValidationError as exc:
        raise HTTPException(status_code=502, detail=f"领星数据拉取或解析失败：{exc}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"领星配置错误：{exc}")
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一为 502
        raise HTTPException(status_code=502, detail=f"领星数据拉取失败：{exc}")

    return {"file_id": file_id, "rows": len(df), "source": "lingxing"}
