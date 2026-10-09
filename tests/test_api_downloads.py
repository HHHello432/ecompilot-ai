from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import app


def test_template_download_endpoint() -> None:
    client = TestClient(app)

    response = client.get("/api/template/download")

    assert response.status_code == 200
    assert "商品名,曝光量,点击量" in response.text
    assert response.headers["content-type"].startswith("text/csv")


def test_html_report_download_endpoint() -> None:
    client = TestClient(app)
    upload = client.post("/api/demo")
    file_id = upload.json()["file_id"]

    response = client.get(f"/api/report/{file_id}/html")

    assert response.status_code == 200
    assert "EcomPilot AI 运营日报" in response.text
    assert "今日运营总结" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_history_endpoint_returns_recent_analysis() -> None:
    client = TestClient(app)
    client.post("/api/demo")

    response = client.get("/api/history?limit=3")

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"]
    assert "summary" in payload["items"][0]
    assert "file_id" in payload["items"][0]


def test_task_state_can_be_saved_and_loaded() -> None:
    client = TestClient(app)
    upload = client.post("/api/demo")
    file_id = upload.json()["file_id"]

    saved = client.put(f"/api/tasks/{file_id}", json={"completed": {"daily-review": True}})
    loaded = client.get(f"/api/tasks/{file_id}")

    assert saved.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["completed"]["daily-review"] is True


def test_platform_connectors_are_optional() -> None:
    client = TestClient(app)

    response = client.get("/api/platforms")
    sync = client.post("/api/platforms/doudian/sync")
    auth = client.post("/api/platforms/doudian/auth/start")

    assert response.status_code == 200
    assert sync.status_code == 200
    assert auth.status_code == 200
    assert {item["id"] for item in response.json()["platforms"]} >= {"doudian", "pdd"}
    assert sync.json()["status"] == "disabled"
    assert auth.json()["status"] == "disabled"


def test_platform_callback_requires_code_and_state() -> None:
    client = TestClient(app)

    response = client.get("/api/platforms/taobao/callback")

    assert response.status_code == 200
    assert response.json()["status"] == "missing_code"


def test_platform_auth_url_can_be_generated_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("ECOMPILOT_ENABLE_PLATFORM_CONNECTORS", "true")
    monkeypatch.setenv("TAOBAO_APP_KEY", "app_key")
    monkeypatch.setenv("TAOBAO_APP_SECRET", "app_secret")
    client = TestClient(app)

    response = client.post("/api/platforms/taobao/auth/start")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert "https://oauth.taobao.com/authorize" in payload["auth_url"]
    assert "client_id=app_key" in payload["auth_url"]
