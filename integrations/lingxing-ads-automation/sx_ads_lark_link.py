import json
import os
import sys
import time
import hmac
import hashlib
import base64
from typing import Dict, Optional, Set

import lark_oapi as lark
import requests
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.api.application.v6.model.p2_application_bot_menu_v6 import P2ApplicationBotMenuV6

# === config start
CONFIG_FILE = "config.json"  # string, 配置文件路径（不入库，凭据放这里）
DEFAULT_CONFIG = {
    "app_id": "",
    "app_secret": "",
    "chat_id": "",  # 群聊 ID（保留备用）
    "download_dir": ".\\waiting",
    "finished_dir": ".\\finished",
    "rpa_webhook_url": "",
    "rpa_webhook_secret": "",
    "menu_event_key_ads_progress": "ads_progress",
    "processed_ids_file": "processed_ids.txt",
    "processed_ids_max": 10000,
    "request_timeout": [5, 60],
    "token_refresh_margin": 300,
    "webhook_retry": 3,
    "webhook_retry_delay": 2,
}


def _load_config(path: str) -> dict:
    """加载配置文件，与内置默认值合并（文件值优先）"""
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(path):
        print(f"[配置] 未找到 {path}，使用内置默认值", file=sys.stderr)
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
        print(f"[配置] 已从 {path} 加载 {len(user_cfg)} 个配置项")
    except Exception as e:
        print(f"[配置] 加载 {path} 失败: {e}，使用内置默认值", file=sys.stderr)
    return cfg


_config = _load_config(CONFIG_FILE)

APP_ID: str = _config["app_id"]
APP_SECRET: str = _config["app_secret"]
CHAT_ID: str = _config["chat_id"]

DOWNLOAD_DIR: str = _config["download_dir"]
FINISHED_DIR: str = _config["finished_dir"]

RPA_WEBHOOK_URL: str = _config["rpa_webhook_url"]
RPA_WEBHOOK_SECRET: str = _config["rpa_webhook_secret"]

MENU_EVENT_KEY_ADS_PROGRESS: str = _config["menu_event_key_ads_progress"]
WEBHOOK_RETRY: int = int(_config["webhook_retry"])
WEBHOOK_RETRY_DELAY: int = int(_config["webhook_retry_delay"])
PROCESSED_IDS_FILE: str = _config["processed_ids_file"]
PROCESSED_IDS_MAX: int = int(_config["processed_ids_max"])
REQUEST_TIMEOUT: tuple = tuple(_config["request_timeout"])
TOKEN_REFRESH_MARGIN: int = int(_config["token_refresh_margin"])
# === config end

# tenant_access_token 缓存: {"token": str, "expires_at": float}
_token_cache: Dict[str, object] = {"token": "", "expires_at": 0.0}

# 已处理消息ID集合（main 中从持久化文件加载）
processed_message_ids: Set[str] = set()


def load_processed_ids(filepath: str) -> Set[str]:
    """从文件加载已处理的消息ID集合，超过上限时裁剪掉最早的一半"""
    ids: Set[str] = set()
    if not os.path.exists(filepath):
        print(f"[持久化] 文件 {filepath} 不存在，将创建新文件")
        return ids
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    ids.add(line)
        print(f"[持久化] 从 {filepath} 加载了 {len(ids)} 个已处理消息ID")
        # 裁剪：只保留最新的一半，避免文件无限膨胀
        if len(ids) > PROCESSED_IDS_MAX:
            keep = sorted(ids)[-PROCESSED_IDS_MAX // 2:]
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(keep) + "\n")
            print(f"[持久化] 超过 {PROCESSED_IDS_MAX} 条，已裁剪至 {len(keep)} 条")
    except Exception as e:
        print(f"[持久化] 加载文件失败: {e}，将使用空集合", file=sys.stderr)
    return ids


def save_processed_id(filepath: str, message_id: str) -> None:
    """将单个消息ID追加写入持久化文件"""
    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(message_id + "\n")
    except Exception as e:
        print(f"[持久化] 写入文件失败: {e}", file=sys.stderr)


def get_tenant_access_token() -> str:
    """获取 tenant_access_token（带缓存，有效期内复用，过期前提前刷新）

    Returns:
        str: tenant_access_token

    Raises:
        RuntimeError: 获取失败
    """
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]  # type: ignore[return-value]

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        resp = requests.post(
            url,
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        raise RuntimeError(f"请求 tenant_access_token 失败: {e}") from e

    if result.get("code", 0) != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {result.get('msg', result)}")

    expire = int(result.get("expire", 7200))
    _token_cache["token"] = result["tenant_access_token"]
    _token_cache["expires_at"] = time.time() + expire - TOKEN_REFRESH_MARGIN
    return _token_cache["token"]  # type: ignore[return-value]


def send_text_message(tenant_access_token: str, receive_id: str, content: str) -> None:
    """发送文本消息给用户

    Raises:
        RuntimeError: 发送失败
    """
    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    payload = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": content}, ensure_ascii=False),
    }
    headers = {
        "Authorization": f"Bearer {tenant_access_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        resp = requests.post(
            url,
            params={"receive_id_type": "open_id"},
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        raise RuntimeError(f"发送消息失败: {e}") from e

    if result.get("code", 0) != 0:
        raise RuntimeError(f"发送消息失败: {result.get('msg', result)}")


def _post_webhook_once() -> None:
    """构造签名并单次调用 RPA webhook

    Raises:
        RuntimeError: 本次调用失败
    """
    timestamp = str(int(time.time()))
    sign = ""
    if RPA_WEBHOOK_SECRET:
        string_to_sign = f"{timestamp}\n{RPA_WEBHOOK_SECRET}"
        hmac_code = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
        sign = base64.b64encode(hmac_code).decode()

    payload = {
        "Timestamp": timestamp,
        "Sign": sign,
        "SpecifiedBot": "",
        "Params": {},
    }
    try:
        resp = requests.post(RPA_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"RPA webhook 触发失败: {e}") from e
    print(f"[RPA] webhook 响应: {resp.text}")


def trigger_rpa_webhook() -> None:
    """触发八爪鱼 RPA webhook（带签名，失败自动重试）

    Raises:
        RuntimeError: 全部重试均失败
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, WEBHOOK_RETRY + 1):
        try:
            _post_webhook_once()
            return
        except Exception as e:
            last_err = e
            print(f"[RPA] 第 {attempt}/{WEBHOOK_RETRY} 次触发失败: {e}", file=sys.stderr)
            if attempt < WEBHOOK_RETRY:
                time.sleep(WEBHOOK_RETRY_DELAY)
    raise RuntimeError(f"RPA webhook 触发失败（已重试 {WEBHOOK_RETRY} 次）: {last_err}") from last_err


def download_message_file(tenant_access_token: str, message_id: str, file_key: str, save_path: str) -> int:
    """下载消息中的文件资源并保存到本地

    Returns:
        int: 文件大小（字节）

    Raises:
        RuntimeError: 下载失败
    """
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
    headers = {"Authorization": f"Bearer {tenant_access_token}"}
    try:
        resp = requests.get(url, headers=headers, params={"type": "file"}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"下载文件失败: {e}") from e

    # 飞书下载接口失败时返回 JSON 错误体
    if resp.headers.get("content-type", "").startswith("application/json"):
        result = resp.json()
        if result.get("code", 0) != 0:
            raise RuntimeError(f"下载文件失败: {result}")

    with open(save_path, "wb") as f:
        f.write(resp.content)
    return len(resp.content)


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    """处理接收消息事件：下载符合条件的文件 -> 通知发送人 -> 触发 RPA

    幂等策略：只有完整走完「下载 -> 通知 -> 触发」才标记已处理，
    中途失败不标记，等待飞书重推时自动重试。
    """
    msg = data.event.message
    if msg.message_type != "file":
        return

    message_id = msg.message_id
    if message_id in processed_message_ids:
        print(f"[跳过] 消息 {message_id} 已处理")
        return

    # --- 解析消息内容（解析失败无法重试，标记已处理避免反复报错） ---
    try:
        content = json.loads(msg.content)
    except json.JSONDecodeError as e:
        print(f"[错误] 解析消息内容失败: {e}, content: {msg.content}", file=sys.stderr)
        processed_message_ids.add(message_id)
        save_processed_id(PROCESSED_IDS_FILE, message_id)
        return

    file_key = content.get("file_key")
    file_name = content.get("file_name")
    if not file_key or not file_name:
        print(f"[错误] 消息内容缺少 file_key/file_name, content: {msg.content}", file=sys.stderr)
        processed_message_ids.add(message_id)
        save_processed_id(PROCESSED_IDS_FILE, message_id)
        return

    # --- 过滤（跳过类无副作用，标记已处理避免重推时重复打日志） ---
    if "广告" not in file_name:
        print(f"[跳过] 文件名不含'广告': {file_name}")
        processed_message_ids.add(message_id)
        save_processed_id(PROCESSED_IDS_FILE, message_id)
        return

    base_name, ext = os.path.splitext(file_name)
    if ext.lower() not in (".xls", ".xlsx"):
        print(f"[跳过] 非表格文件: {file_name}")
        processed_message_ids.add(message_id)
        save_processed_id(PROCESSED_IDS_FILE, message_id)
        return

    # --- 核心流程（任一步失败则不标记，等飞书重推重试） ---
    open_id = data.event.sender.sender_id.open_id
    timestamp = time.strftime("%Y%m%d%H%M%S")
    safe_filename = f"[{open_id}]{timestamp}-{base_name}{ext}"
    file_path = os.path.join(DOWNLOAD_DIR, safe_filename)

    try:
        token = get_tenant_access_token()

        size = download_message_file(token, message_id, file_key, file_path)
        print(f"[下载] {file_name} -> {file_path} ({size} 字节)")

        # 通知失败不阻断流程（文件已落盘，不该因此丢触发）
        try:
            send_text_message(token, open_id, f"【甲组_广告】【{file_name}】进入排队序列。")
        except Exception as e:
            print(f"[警告] 通知发送人失败（继续触发RPA）: {e}", file=sys.stderr)

        trigger_rpa_webhook()
    except Exception as e:
        # 未标记已处理，飞书重推时会自动重试
        print(f"[错误] 处理消息 {message_id} 失败（等待重推重试）: {e}", file=sys.stderr)
        # 下载了一半的残文件清理掉
        if os.path.exists(file_path) and os.path.getsize(file_path) == 0:
            try:
                os.remove(file_path)
            except OSError:
                pass
        return

    # --- 全部成功，标记已处理 ---
    processed_message_ids.add(message_id)
    save_processed_id(PROCESSED_IDS_FILE, message_id)
    print(f"[完成] {message_id} 处理完毕")


def build_queue_progress_text() -> str:
    """扫描 waiting / finished 目录，构建排队进度文本"""
    waiting_files = sorted(
        f for f in os.listdir(DOWNLOAD_DIR)
        if os.path.isfile(os.path.join(DOWNLOAD_DIR, f))
        and os.path.splitext(f)[1].lower() in (".xls", ".xlsx")
    )

    today = time.strftime("%Y%m%d")
    finished_today = 0
    for f in os.listdir(FINISHED_DIR):
        fp = os.path.join(FINISHED_DIR, f)
        if os.path.isfile(fp) and time.strftime("%Y%m%d", time.localtime(os.path.getmtime(fp))) == today:
            finished_today += 1

    lines = [f"【甲组_广告】排队进度", f"待处理 {len(waiting_files)} 个 | 今日已完成 {finished_today} 个"]
    if waiting_files:
        for name in waiting_files[:5]:
            lines.append(f"  - {name}")
        if len(waiting_files) > 5:
            lines.append(f"  ...等 {len(waiting_files) - 5} 个")
    else:
        lines.append("队列空闲，无待处理文件")
    return "\n".join(lines)


def do_p2_application_bot_menu_v6(data: P2ApplicationBotMenuV6) -> None:
    """处理机器人菜单点击事件：event_key=ads_progress 时向点击者回复排队进度"""
    event_key = data.event.event_key
    if event_key != MENU_EVENT_KEY_ADS_PROGRESS:
        print(f"[菜单] 忽略未关注的 event_key: {event_key}")
        return

    open_id = data.event.operator.operator_id.open_id
    print(f"[菜单] {open_id} 点击 ads_progress，查询排队进度")
    try:
        token = get_tenant_access_token()
        send_text_message(token, open_id, build_queue_progress_text())
    except Exception as e:
        print(f"[错误] 回复排队进度失败: {e}", file=sys.stderr)


event_handler = lark.EventDispatcherHandler.builder("", "") \
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
    .register_p2_application_bot_menu_v6(do_p2_application_bot_menu_v6) \
    .build()


def set_terminal_title(title: str) -> None:
    """设置当前终端窗口的标题（跨平台）"""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            os.system(f"title {title}")
    else:
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()


def main() -> None:
    set_terminal_title("【甲组_广告】260908.1040")
    if not APP_ID or not APP_SECRET:
        print(f"[错误] 缺少 app_id/app_secret，请参照 config.example.json 配置 {CONFIG_FILE}", file=sys.stderr)
        sys.exit(1)

    global processed_message_ids
    processed_message_ids = load_processed_ids(PROCESSED_IDS_FILE)

    # 启动时确保目录存在，消息处理中不再重复创建
    for d in (DOWNLOAD_DIR, FINISHED_DIR):
        if not os.path.exists(d):
            os.makedirs(d)

    cli = lark.ws.Client(APP_ID, APP_SECRET,
                         event_handler=event_handler,
                         log_level=lark.LogLevel.INFO)
    cli.start()


if __name__ == "__main__":
    main()
