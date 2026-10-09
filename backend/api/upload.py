from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Response, UploadFile
import pandas as pd

from backend.database.db import save_analysis
from backend.models.schema import UploadResponse
from backend.services.data_cleaner import DataValidationError
from backend.services.pipeline import analyze_dataframe, analyze_upload

router = APIRouter(prefix="/api", tags=["upload"])


@router.post("/upload", response_model=UploadResponse)
async def upload_file(file: UploadFile = File(...)) -> UploadResponse:
    content = await file.read()
    file_id = f"file_{uuid4().hex[:12]}"
    try:
        bundle = analyze_upload(file_id, content, file.filename or "upload.csv")
    except DataValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="文件分析失败，请检查数据格式。") from exc

    save_analysis(file_id, bundle.to_dict())
    return UploadResponse(file_id=file_id, message="上传成功，分析已完成。", warnings=bundle.warnings)


@router.post("/demo", response_model=UploadResponse)
def load_demo() -> UploadResponse:
    sample_path = Path(__file__).resolve().parents[2] / "sample_data" / "ecommerce_demo.csv"
    if not sample_path.exists():
        raise HTTPException(status_code=404, detail="演示数据不存在。")

    file_id = "demo_latest"
    try:
        dataframe = pd.read_csv(sample_path, encoding="utf-8-sig")
        bundle = analyze_dataframe(file_id, dataframe)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="演示数据分析失败。") from exc

    save_analysis(file_id, bundle.to_dict())
    return UploadResponse(file_id=file_id, message="演示数据已载入。", warnings=bundle.warnings)


@router.get("/template/download")
def download_template() -> Response:
    template_path = Path(__file__).resolve().parents[2] / "sample_data" / "ecommerce_template.csv"
    if not template_path.exists():
        raise HTTPException(status_code=404, detail="数据模板不存在。")

    return Response(
        content=template_path.read_text(encoding="utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ecompilot_upload_template.csv"'},
    )
