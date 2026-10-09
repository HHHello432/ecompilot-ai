from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    file_id: str
    message: str
    warnings: list[str] = Field(default_factory=list)


class AnalysisResponse(BaseModel):
    file_id: str
    summary: dict[str, Any]
    products: list[dict[str, Any]]
    warnings: list[str] = Field(default_factory=list)


class DiagnosisResponse(BaseModel):
    file_id: str
    summary: str
    alerts: list[str]
    suggestions: list[str]
    source: str


class ReportResponse(BaseModel):
    file_id: str
    report: str


class QuestionRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)


class AnswerResponse(BaseModel):
    file_id: str
    answer: str


class HistoryItem(BaseModel):
    file_id: str
    saved_at: float
    summary: dict[str, Any]
    diagnosis_summary: str = ""
    warnings: list[str] = Field(default_factory=list)


class HistoryResponse(BaseModel):
    items: list[HistoryItem]


class TaskStateRequest(BaseModel):
    completed: dict[str, bool] = Field(default_factory=dict)


class TaskStateResponse(BaseModel):
    file_id: str
    completed: dict[str, bool] = Field(default_factory=dict)


class PlatformStatusItem(BaseModel):
    id: str
    name: str
    enabled: bool
    configured: bool
    authorized: bool = False
    status: str
    capabilities: list[str] = Field(default_factory=list)
    next_step: str
    redirect_uri: str = ""
    last_sync_at: float | None = None


class PlatformListResponse(BaseModel):
    platforms: list[PlatformStatusItem]


class PlatformAuthStartResponse(BaseModel):
    platform_id: str
    status: str
    message: str
    auth_url: str = ""
    state: str = ""
    redirect_uri: str = ""


class PlatformCallbackResponse(BaseModel):
    platform_id: str
    status: str
    message: str
    shop_id: str = ""


class PlatformSyncResponse(BaseModel):
    platform_id: str
    status: str
    message: str
    job_id: str = ""
