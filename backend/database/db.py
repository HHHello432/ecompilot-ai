from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STORAGE_DIR = Path(__file__).resolve().parents[1] / "storage"
TASK_STATE_DIR = STORAGE_DIR / "task_state"
PLATFORM_DIR = STORAGE_DIR / "platforms"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
TASK_STATE_DIR.mkdir(parents=True, exist_ok=True)
PLATFORM_DIR.mkdir(parents=True, exist_ok=True)


def save_analysis(file_id: str, payload: dict[str, Any]) -> None:
    path = _analysis_path(file_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_analysis(file_id: str) -> dict[str, Any] | None:
    path = _analysis_path(file_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_analysis_ids() -> list[str]:
    return sorted(path.stem for path in STORAGE_DIR.glob("*.json"))


def list_analysis_records(limit: int = 8) -> list[dict[str, Any]]:
    paths = sorted(STORAGE_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    records: list[dict[str, Any]] = []
    for path in paths[:limit]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records.append(
            {
                "file_id": path.stem,
                "saved_at": path.stat().st_mtime,
                "summary": payload.get("summary", {}),
                "diagnosis_summary": payload.get("diagnosis", {}).get("summary", ""),
                "warnings": payload.get("warnings", []),
            }
        )
    return records


def load_task_state(file_id: str) -> dict[str, bool]:
    path = _task_state_path(file_id)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {str(key): bool(value) for key, value in payload.get("completed", {}).items()}


def save_task_state(file_id: str, completed: dict[str, bool]) -> dict[str, bool]:
    clean_completed = {str(key): bool(value) for key, value in completed.items()}
    path = _task_state_path(file_id)
    path.write_text(json.dumps({"completed": clean_completed}, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean_completed


def save_platform_auth_state(state: str, payload: dict[str, Any]) -> None:
    path = _platform_state_path(state)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_platform_auth_state(state: str) -> dict[str, Any] | None:
    path = _platform_state_path(state)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_platform_connection(platform_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = _platform_connection_path(platform_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_platform_connection(platform_id: str) -> dict[str, Any] | None:
    path = _platform_connection_path(platform_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_platform_connections() -> dict[str, dict[str, Any]]:
    connections: dict[str, dict[str, Any]] = {}
    for path in PLATFORM_DIR.glob("*_connection.json"):
        platform_id = path.name.removesuffix("_connection.json")
        connections[platform_id] = json.loads(path.read_text(encoding="utf-8"))
    return connections


def save_platform_sync_job(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = _platform_sync_job_path(job_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def list_platform_sync_jobs(limit: int = 8) -> list[dict[str, Any]]:
    paths = sorted(PLATFORM_DIR.glob("sync_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths[:limit]]


def _analysis_path(file_id: str) -> Path:
    safe_id = "".join(char for char in file_id if char.isalnum() or char in {"-", "_"})
    return STORAGE_DIR / f"{safe_id}.json"


def _task_state_path(file_id: str) -> Path:
    safe_id = "".join(char for char in file_id if char.isalnum() or char in {"-", "_"})
    return TASK_STATE_DIR / f"{safe_id}.json"


def _platform_state_path(state: str) -> Path:
    safe_state = "".join(char for char in state if char.isalnum() or char in {"-", "_"})
    return PLATFORM_DIR / f"state_{safe_state}.json"


def _platform_connection_path(platform_id: str) -> Path:
    safe_id = "".join(char for char in platform_id if char.isalnum() or char in {"-", "_"})
    return PLATFORM_DIR / f"{safe_id}_connection.json"


def _platform_sync_job_path(job_id: str) -> Path:
    safe_id = "".join(char for char in job_id if char.isalnum() or char in {"-", "_"})
    return PLATFORM_DIR / f"sync_{safe_id}.json"
