"""RPA 控制台集成（octopus-rpa-console-dashboard）。

把 ``integrations/octopus-rpa-console-dashboard`` 这份八爪鱼 RPA 控制台作为
EcomPilot 的**托管子服务**运行，并通过 ``/console/*`` 反向代理暴露给前端：

- 子服务使用它自带的纯标准库 HTTP 服务（``local_server.py``），监听 127.0.0.1，
  端口由 ``ECOMPILOT_CONSOLE_PORT`` 控制（默认 ``8010``）；
- 浏览器访问 ``http://<ecompilot>:8000/console/`` 即可打开完整控制台，
  页面里的 ``/console/api/*`` 与 ``/console/*`` 静态资源都由本代理转发；
- 因此 EcomPilot 只需启动一个进程（``uvicorn backend.main:app``）。

可通过环境变量 ``ECOMPILOT_ENABLE_CONSOLE=0`` 关闭该子服务（关闭后相关接口返回 503）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from urllib import error as urlerror
from urllib import request as urlrequest

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONSOLE_DIR = os.path.join(PROJECT_ROOT, "integrations", "octopus-rpa-console-dashboard")
CONSOLE_ENTRY = os.path.join(CONSOLE_DIR, "local_server.py")
CONSOLE_LOG = os.path.join(CONSOLE_DIR, "console.log")

CONSOLE_HOST = "127.0.0.1"
CONSOLE_PORT = int(os.environ.get("ECOMPILOT_CONSOLE_PORT", "8010"))
CONSOLE_URL = "http://%s:%d" % (CONSOLE_HOST, CONSOLE_PORT)

ENABLED = os.environ.get("ECOMPILOT_ENABLE_CONSOLE", "1").lower() not in ("0", "false", "no", "off")

# 访问 localhost 必须绕过系统/沙箱 HTTP 代理，否则会被代理层拦截成 502
_OPENER = urlrequest.build_opener(urlrequest.ProxyHandler({}))

_PROC: subprocess.Popen | None = None
_LOCK = threading.Lock()

router = APIRouter(tags=["console"])

# 转发时不应透传的逐跳响应头
_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                "te", "trailers", "transfer-encoding", "upgrade"}


def _port_open(host: str = CONSOLE_HOST, port: int = CONSOLE_PORT, timeout: float = 1.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _spawn() -> subprocess.Popen:
    os.makedirs(os.path.join(CONSOLE_DIR, "output"), exist_ok=True)
    env = dict(os.environ)
    env["CONSOLE_PORT"] = str(CONSOLE_PORT)
    env["PYTHONIOENCODING"] = "utf-8"
    # 子进程自身访问外网仍需代理，这里保留原样；只对"我们访问子服务"这一步绕过代理
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = open(CONSOLE_LOG, "ab", buffering=0)
    return subprocess.Popen(
        [sys.executable, CONSOLE_ENTRY],
        cwd=CONSOLE_DIR,
        env=env,
        stdout=log,
        stderr=log,
        **kwargs,
    )


def ensure_started(wait: float = 15.0) -> bool:
    """确保控制台子服务已启动；返回是否可用。"""
    if not ENABLED:
        return False
    if _port_open():
        return True
    if not os.path.exists(CONSOLE_ENTRY):
        return False
    global _PROC
    with _LOCK:
        if _port_open():
            return True
        if _PROC is None or _PROC.poll() is not None:
            _PROC = _spawn()
        deadline = time.time() + wait
        while time.time() < deadline:
            if _port_open():
                return True
            if _PROC.poll() is not None:      # 子进程已退出，不必再等
                return False
            time.sleep(0.3)
        return _port_open()


def stop_console() -> None:
    """停止控制台子服务（幂等）。"""
    global _PROC
    with _LOCK:
        if _PROC is not None and _PROC.poll() is None:
            try:
                _PROC.terminate()
                _PROC.wait(timeout=5)
            except Exception:
                try:
                    _PROC.kill()
                except Exception:
                    pass
        _PROC = None


def console_state() -> dict:
    running = _port_open(timeout=0.4)
    return {
        "enabled": ENABLED,
        "running": running,
        "available": bool(ENABLED and os.path.exists(CONSOLE_ENTRY)),
        "port": CONSOLE_PORT,
        "url": "/console/",
        "source": "integrations/octopus-rpa-console-dashboard",
    }


def _forward(method: str, path: str, query: str, headers: dict, body: bytes | None):
    target = "%s/%s" % (CONSOLE_URL, path.lstrip("/"))
    if query:
        target += "?" + query
    req = urlrequest.Request(target, data=body, method=method)
    for k, v in headers.items():
        lk = k.lower()
        if lk in _HOP_HEADERS or lk in ("host", "content-length", "accept-encoding"):
            continue
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urlerror.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except Exception as e:  # 连接被拒 / 超时
        return 0, {}, str(e).encode("utf-8")


@router.get("/api/console/status")
def console_status() -> dict:
    """控制台子服务状态（前端据此决定是否显示入口）。"""
    return console_state()


@router.post("/api/console/start")
def console_start() -> dict:
    ok = ensure_started()
    st = console_state()
    st["ok"] = ok
    return st


@router.post("/api/console/stop")
def console_stop() -> dict:
    stop_console()
    st = console_state()
    st["ok"] = True
    return st


@router.get("/console")
def console_redirect() -> RedirectResponse:
    return RedirectResponse(url="/console/")


@router.api_route("/console/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"])
async def console_proxy(path: str, request: Request) -> Response:
    """把 /console/* 反向代理到控制台子服务。"""
    if not ENABLED:
        return JSONResponse(
            {"ok": False, "error": "控制台子服务已禁用（ECOMPILOT_ENABLE_CONSOLE=0）"},
            status_code=503,
        )
    if not await run_in_threadpool(ensure_started):
        return JSONResponse(
            {"ok": False, "error": "控制台子服务启动失败，请查看 %s" % CONSOLE_LOG,
             "state": console_state()},
            status_code=503,
        )

    body = await request.body() if request.method in ("POST", "PUT", "PATCH", "DELETE") else None
    status, headers, content = await run_in_threadpool(
        _forward, request.method, path, request.url.query, dict(request.headers), body
    )
    if status == 0:
        return JSONResponse(
            {"ok": False, "error": "无法连接控制台子服务", "detail": content.decode("utf-8", "replace")},
            status_code=502,
        )

    out_headers = {k: v for k, v in headers.items() if k.lower() not in _HOP_HEADERS}
    media_type = headers.get("Content-Type") or headers.get("content-type")
    return Response(content=content, status_code=status, headers=out_headers, media_type=media_type)
