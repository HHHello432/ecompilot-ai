from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from secrets import token_urlsafe
from typing import Any

from backend.database.db import (
    load_platform_auth_state,
    load_platform_connection,
    save_platform_auth_state,
    save_platform_connection,
    save_platform_sync_job,
)
from backend.platforms.registry import connectors_enabled, platform_config


def begin_authorization(platform_id: str) -> dict[str, Any]:
    config = platform_config(platform_id)
    if not config:
        return {"status": "not_found", "message": "不支持的平台"}
    if not connectors_enabled():
        return {"status": "disabled", "message": "平台连接器未启用，上传文件和演示数据仍可正常运行。"}
    if not config["client_id"] or not config["client_secret"]:
        return {"status": "missing_credentials", "message": f"{config['name']} 缺少应用密钥。"}

    state = token_urlsafe(24)
    redirect_uri = str(config["redirect_uri"])
    query = {
        str(config["client_param"]): str(config["client_id"]),
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if platform_id == "taobao":
        query["view"] = "web"

    auth_url = f"{config['auth_url']}?{urllib.parse.urlencode(query)}"
    save_platform_auth_state(
        state,
        {
            "platform_id": platform_id,
            "created_at": time.time(),
            "redirect_uri": redirect_uri,
        },
    )
    return {
        "status": "ready",
        "message": "授权地址已生成，请在平台页面完成店铺授权。",
        "auth_url": auth_url,
        "state": state,
        "redirect_uri": redirect_uri,
    }


def complete_authorization(platform_id: str, code: str, state: str, shop_id: str = "") -> dict[str, Any]:
    saved_state = load_platform_auth_state(state)
    if not saved_state or saved_state.get("platform_id") != platform_id:
        return {"status": "invalid_state", "message": "授权状态校验失败，请重新发起授权。"}

    config = platform_config(platform_id)
    if not config:
        return {"status": "not_found", "message": "不支持的平台"}

    token_payload = exchange_token(config, code, str(saved_state["redirect_uri"]))
    connection = {
        "platform_id": platform_id,
        "shop_id": shop_id or extract_shop_id(token_payload),
        "authorized_at": time.time(),
        "last_sync_at": None,
        "access_token": token_payload.get("access_token", ""),
        "refresh_token": token_payload.get("refresh_token", ""),
        "expires_in": token_payload.get("expires_in"),
        "raw_token_keys": sorted(token_payload.keys()),
        "token_exchange_status": token_payload.get("_exchange_status", "success"),
        "token_exchange_message": token_payload.get("_exchange_message", ""),
    }
    save_platform_connection(platform_id, connection)
    if connection["access_token"]:
        return {
            "status": "authorized",
            "message": "店铺授权已保存，后续可从同步入口拉取商品、订单和售后数据。",
            "shop_id": connection["shop_id"],
        }
    return {
        "status": "code_saved",
        "message": "授权回调已接收，但令牌交换未完成；请检查平台密钥、回调地址和开放平台权限。",
        "shop_id": connection["shop_id"],
    }


def create_sync_job(platform_id: str) -> dict[str, Any]:
    config = platform_config(platform_id)
    connection = load_platform_connection(platform_id)
    job_id = f"{platform_id}_{int(time.time())}"
    if not config:
        status = "not_found"
        message = "不支持的平台"
    elif not connectors_enabled():
        status = "disabled"
        message = "平台连接器未启用，当前项目仍可使用演示数据或上传文件运行。"
    elif not connection or not connection.get("access_token"):
        status = "not_authorized"
        message = f"{config['name']} 尚未完成店铺授权，不能同步真实店铺数据。"
    else:
        status = "queued"
        message = "同步任务已创建。当前版本先记录任务，后续连接具体商品/订单接口时会在这里执行分页拉取。"
        connection["last_sync_at"] = time.time()
        save_platform_connection(platform_id, connection)

    return save_platform_sync_job(
        job_id,
        {
            "job_id": job_id,
            "platform_id": platform_id,
            "status": status,
            "message": message,
            "created_at": time.time(),
        },
    )


def exchange_token(config: dict[str, Any], code: str, redirect_uri: str) -> dict[str, Any]:
    data = {
        str(config["client_param"]): str(config["client_id"]),
        str(config["secret_param"]): str(config["client_secret"]),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    request = urllib.request.Request(
        str(config["token_url"]),
        data=urllib.parse.urlencode(data).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        return {
            "_exchange_status": "failed",
            "_exchange_message": str(error),
        }
    if "data" in payload and isinstance(payload["data"], dict):
        payload = {**payload, **payload["data"]}
    payload["_exchange_status"] = "success" if payload.get("access_token") else "missing_access_token"
    return payload


def extract_shop_id(payload: dict[str, Any]) -> str:
    for key in ("shop_id", "shopId", "seller_id", "taobao_user_id", "owner_id", "mall_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""
