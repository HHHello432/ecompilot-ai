from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.database.db import list_analysis_records, load_analysis, load_task_state, save_task_state
from backend.models.schema import (
    AnalysisResponse,
    AnswerResponse,
    DiagnosisResponse,
    HistoryResponse,
    QuestionRequest,
    TaskStateRequest,
    TaskStateResponse,
)
from backend.services.ai_diagnosis import answer_question
from backend.services.pipeline import bundle_from_dict

router = APIRouter(prefix="/api", tags=["analysis"])


@router.get("/analysis/{file_id}", response_model=AnalysisResponse)
def get_analysis(file_id: str) -> AnalysisResponse:
    bundle = _get_bundle(file_id)
    return AnalysisResponse(
        file_id=file_id,
        summary=bundle.summary,
        products=bundle.products.to_dict(orient="records"),
        warnings=bundle.warnings,
    )


@router.get("/diagnosis/{file_id}", response_model=DiagnosisResponse)
def get_diagnosis(file_id: str) -> DiagnosisResponse:
    bundle = _get_bundle(file_id)
    return DiagnosisResponse(file_id=file_id, **bundle.diagnosis)


@router.post("/question/{file_id}", response_model=AnswerResponse)
def ask_question(file_id: str, payload: QuestionRequest) -> AnswerResponse:
    bundle = _get_bundle(file_id)
    answer = answer_question(payload.question, bundle.summary, bundle.products)
    return AnswerResponse(file_id=file_id, answer=answer)


@router.get("/history", response_model=HistoryResponse)
def get_history(limit: int = 8) -> HistoryResponse:
    safe_limit = max(1, min(limit, 20))
    return HistoryResponse(items=list_analysis_records(safe_limit))


@router.get("/tasks/{file_id}", response_model=TaskStateResponse)
def get_task_state(file_id: str) -> TaskStateResponse:
    _get_bundle(file_id)
    return TaskStateResponse(file_id=file_id, completed=load_task_state(file_id))


@router.put("/tasks/{file_id}", response_model=TaskStateResponse)
def update_task_state(file_id: str, payload: TaskStateRequest) -> TaskStateResponse:
    _get_bundle(file_id)
    completed = save_task_state(file_id, payload.completed)
    return TaskStateResponse(file_id=file_id, completed=completed)


def _get_bundle(file_id: str):
    payload = load_analysis(file_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="没有找到这份分析，请重新上传数据。")
    return bundle_from_dict(payload)
