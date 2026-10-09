from __future__ import annotations

import os

from backend.database.db import load_platform_connection
from backend.platforms.base import PlatformStatus


PLATFORMS = {
    "doudian": {
        "name": "抖店",
        "capabilities": ["商品", "订单", "售后", "库存", "经营数据"],
        "auth_mode": "oauth",
        "env": ("DOUDIAN_APP_KEY", "DOUDIAN_APP_SECRET"),
        "client_id_env": "DOUDIAN_APP_KEY",
        "client_secret_env": "DOUDIAN_APP_SECRET",
        "auth_url_env": "DOUDIAN_AUTH_URL",
        "token_url_env": "DOUDIAN_TOKEN_URL",
        "auth_url": "https://op.jinritemai.com/oauth/authorize",
        "token_url": "https://openapi-fxg.jinritemai.com/oauth2/access_token",
        "client_param": "app_key",
        "secret_param": "app_secret",
        "next_step": "在抖店开放平台创建应用，申请店铺授权后填入 App Key / Secret。",
    },
    "pdd": {
        "name": "拼多多",
        "capabilities": ["商品", "订单", "售后", "物流", "库存"],
        "auth_mode": "oauth",
        "env": ("PDD_CLIENT_ID", "PDD_CLIENT_SECRET"),
        "client_id_env": "PDD_CLIENT_ID",
        "client_secret_env": "PDD_CLIENT_SECRET",
        "auth_url_env": "PDD_AUTH_URL",
        "token_url_env": "PDD_TOKEN_URL",
        "auth_url": "https://oauth.pinduoduo.com/authorize",
        "token_url": "https://open-api.pinduoduo.com/oauth/token",
        "client_param": "client_id",
        "secret_param": "client_secret",
        "next_step": "在拼多多开放平台创建应用，申请商品、交易、售后权限后填入密钥。",
    },
    "taobao": {
        "name": "淘宝 / 天猫",
        "capabilities": ["商品", "订单", "退款", "物流"],
        "auth_mode": "oauth",
        "env": ("TAOBAO_APP_KEY", "TAOBAO_APP_SECRET"),
        "client_id_env": "TAOBAO_APP_KEY",
        "client_secret_env": "TAOBAO_APP_SECRET",
        "auth_url_env": "TAOBAO_AUTH_URL",
        "token_url_env": "TAOBAO_TOKEN_URL",
        "auth_url": "https://oauth.taobao.com/authorize",
        "token_url": "https://oauth.taobao.com/token",
        "client_param": "client_id",
        "secret_param": "client_secret",
        "next_step": "在淘宝开放平台创建应用，配置回调地址并完成店铺授权。",
    },
    "jd": {
        "name": "京东",
        "capabilities": ["商品", "订单", "售后", "库存"],
        "auth_mode": "oauth",
        "env": ("JD_APP_KEY", "JD_APP_SECRET"),
        "client_id_env": "JD_APP_KEY",
        "client_secret_env": "JD_APP_SECRET",
        "auth_url_env": "JD_AUTH_URL",
        "token_url_env": "JD_TOKEN_URL",
        "auth_url": "https://oauth.jd.com/oauth/authorize",
        "token_url": "https://oauth.jd.com/oauth/token",
        "client_param": "client_id",
        "secret_param": "client_secret",
        "next_step": "在京东宙斯/JOS 创建应用，按店铺类型申请对应 API 权限。",
    },
    "lingxing": {
        "name": "领星 ERP",
        "capabilities": ["商品表现", "广告", "库存", "发货", "采购", "财务"],
        "auth_mode": "cookie",
        "env": ("LINGXING_COOKIE",),
        "next_step": (
            "在浏览器登录领星 ERP 后复制 Cookie（需含 auth-token/company_id/"
            "env_key/uid/zid）填入 .env 的 LINGXING_COOKIE。"
        ),
    },
}


def connectors_enabled() -> bool:
    return os.getenv("ECOMPILOT_ENABLE_PLATFORM_CONNECTORS", "false").lower() == "true"


def callback_base_url() -> str:
    return os.getenv("ECOMPILOT_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def platform_config(platform_id: str) -> dict[str, object] | None:
    config = PLATFORMS.get(platform_id)
    if not config:
        return None
    # Cookie 模式（领星）：无 OAuth 的 client_id/client_secret/redirect_uri
    if config.get("auth_mode", "oauth") == "cookie":
        return {
            **config,
            "client_id": "",
            "client_secret": "",
            "redirect_uri": "",
        }
    auth_url = os.getenv(str(config["auth_url_env"]), str(config["auth_url"]))
    token_url = os.getenv(str(config["token_url_env"]), str(config["token_url"]))
    return {
        **config,
        "auth_url": auth_url,
        "token_url": token_url,
        "client_id": os.getenv(str(config["client_id_env"]), ""),
        "client_secret": os.getenv(str(config["client_secret_env"]), ""),
        "redirect_uri": os.getenv(
            f"{platform_id.upper()}_REDIRECT_URI",
            f"{callback_base_url()}/api/platforms/{platform_id}/callback",
        ),
    }


def list_platform_status() -> list[PlatformStatus]:
    enabled = connectors_enabled()
    statuses: list[PlatformStatus] = []
    for platform_id, config in PLATFORMS.items():
        resolved = platform_config(platform_id) or config
        required_env = config["env"]
        configured = all(os.getenv(key) for key in required_env)

        # Cookie 模式（领星）：configured 即视为已授权（Cookie 有效即可用）
        if config.get("auth_mode", "oauth") == "cookie":
            authorized = configured
            redirect_uri = ""
            if not enabled:
                status = "未启用"
            elif authorized:
                status = "已配置 Cookie"
            else:
                status = "缺少 Cookie"
        else:
            connection = load_platform_connection(platform_id)
            authorized = bool(connection and connection.get("access_token"))
            redirect_uri = str(resolved.get("redirect_uri", ""))
            if not enabled:
                status = "未启用"
            elif authorized:
                status = "已授权"
            elif configured:
                status = "已配置，等待授权流程"
            else:
                status = "缺少密钥"

        statuses.append(
            PlatformStatus(
                id=platform_id,
                name=config["name"],
                enabled=enabled,
                configured=configured,
                authorized=authorized,
                status=status,
                capabilities=config["capabilities"],
                next_step=config["next_step"],
                redirect_uri=redirect_uri,
                last_sync_at=None,
            )
        )
    return statuses


def get_platform_status(platform_id: str) -> PlatformStatus | None:
    return next((item for item in list_platform_status() if item.id == platform_id), None)
