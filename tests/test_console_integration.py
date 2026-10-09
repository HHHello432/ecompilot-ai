"""RPA 控制台集成测试。

只验证「代理层」与「状态接口」的逻辑，**不真正启动子进程、不发网络请求**：
- 反向代理：monkeypatch `_forward` 返回伪造响应，断言透传正确；
- 禁用开关：monkeypatch `ENABLED=False`，断言 503；
- 静态资源前缀：直接读移植后的 `index.html`，断言没有漏加 `/console` 前缀的绝对路径。
"""

from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from backend.api import console as console_mod
from backend.main import app

CLIENT = TestClient(app)

CONSOLE_DIR = console_mod.CONSOLE_DIR


# ---------------------------------------------------------------- 状态接口

def test_console_state_shape():
    state = console_mod.console_state()
    for key in ("enabled", "running", "available", "port", "url", "source"):
        assert key in state
    assert state["url"] == "/console/"
    assert isinstance(state["port"], int)


def test_status_endpoint():
    resp = CLIENT.get("/api/console/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "/console/"
    assert body["source"] == "integrations/octopus-rpa-console-dashboard"


def test_console_redirect():
    resp = CLIENT.get("/console", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/console/"


# ---------------------------------------------------------------- 反向代理

def test_proxy_forwards_body_and_status(monkeypatch):
    captured = {}

    def fake_forward(method, path, query, headers, body):
        captured.update(method=method, path=path, query=query, body=body)
        return 200, {"Content-Type": "text/html"}, b"<html>console</html>"

    monkeypatch.setattr(console_mod, "ENABLED", True)
    monkeypatch.setattr(console_mod, "ensure_started", lambda *a, **k: True)
    monkeypatch.setattr(console_mod, "_forward", fake_forward)

    resp = CLIENT.get("/console/index.html?x=1")
    assert resp.status_code == 200
    assert resp.content == b"<html>console</html>"
    assert captured["path"] == "index.html"
    assert captured["query"] == "x=1"
    assert captured["method"] == "GET"


def test_proxy_posts_body(monkeypatch):
    captured = {}

    def fake_forward(method, path, query, headers, body):
        captured.update(method=method, path=path, body=body)
        return 200, {"Content-Type": "application/json"}, b'{"ok":true}'

    monkeypatch.setattr(console_mod, "ENABLED", True)
    monkeypatch.setattr(console_mod, "ensure_started", lambda *a, **k: True)
    monkeypatch.setattr(console_mod, "_forward", fake_forward)

    resp = CLIENT.post("/console/api/rerun", json={"run": 1})
    assert resp.status_code == 200
    assert captured["method"] == "POST"
    assert b"run" in captured["body"]


def test_proxy_returns_502_when_subservice_down(monkeypatch):
    monkeypatch.setattr(console_mod, "ENABLED", True)
    monkeypatch.setattr(console_mod, "ensure_started", lambda *a, **k: True)
    monkeypatch.setattr(console_mod, "_forward", lambda *a, **k: (0, {}, b"conn refused"))

    resp = CLIENT.get("/console/")
    assert resp.status_code == 502
    assert resp.json()["ok"] is False


def test_proxy_disabled_returns_503(monkeypatch):
    monkeypatch.setattr(console_mod, "ENABLED", False)
    resp = CLIENT.get("/console/")
    assert resp.status_code == 503


def test_proxy_503_when_start_fails(monkeypatch):
    monkeypatch.setattr(console_mod, "ENABLED", True)
    monkeypatch.setattr(console_mod, "ensure_started", lambda *a, **k: False)
    resp = CLIENT.get("/console/")
    assert resp.status_code == 503
    assert resp.json()["ok"] is False


def test_start_stop_endpoints(monkeypatch):
    monkeypatch.setattr(console_mod, "ensure_started", lambda *a, **k: True)
    monkeypatch.setattr(console_mod, "stop_console", lambda: None)
    assert CLIENT.post("/api/console/start").json()["ok"] is True
    assert CLIENT.post("/api/console/stop").json()["ok"] is True


# ---------------------------------------------------------------- 静态资源前缀

@pytest.mark.skipif(not os.path.exists(os.path.join(CONSOLE_DIR, "web", "index.html")),
                    reason="控制台未随仓库提供")
def test_index_html_all_absolute_refs_prefixed():
    html = open(os.path.join(CONSOLE_DIR, "web", "index.html"), encoding="utf-8").read()
    refs = re.findall(r'(?:href|src)="([^"]+)"', html)
    assert refs, "index.html 里应至少有若干静态资源引用"
    bad = [r for r in refs if r.startswith("/") and not r.startswith("/console")]
    assert not bad, f"存在未加 /console 前缀的绝对路径: {bad}"


@pytest.mark.skipif(not os.path.exists(os.path.join(CONSOLE_DIR, "web", "app.js")),
                    reason="控制台未随仓库提供")
def test_app_js_api_calls_prefixed():
    js = open(os.path.join(CONSOLE_DIR, "web", "app.js"), encoding="utf-8").read()
    # 不应再有裸 '/api/...' 字符串常量（已统一加 /console 前缀）
    bare = re.findall(r"['\"`](/api/[a-zA-Z0-9_\-/]*)['\"`]", js)
    assert not bare, f"app.js 中仍有未加前缀的 API 路径: {sorted(set(bare))}"


@pytest.mark.skipif(not os.path.exists(os.path.join(CONSOLE_DIR, "local_server.py")),
                    reason="控制台未随仓库提供")
def test_local_server_port_is_configurable():
    src = open(os.path.join(CONSOLE_DIR, "local_server.py"), encoding="utf-8").read()
    assert "CONSOLE_PORT" in src, "端口应可通过 CONSOLE_PORT 环境变量配置"
    assert "PORT = 8000" not in src, "端口不应再硬编码为 8000"
