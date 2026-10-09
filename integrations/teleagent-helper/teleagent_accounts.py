#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TeleAgent 账号管理器 —— Owner 轻量切换 / 快照导入 / 别名

背景（TeleAgent 2.5.2+ 的数据模型）：
    TeleAgent/users/v1_public_<userId>/        <- 一个账号的数据（teleagent.db、token、memory...）
    TeleAgent/Partitions/owner%3Av1_public_<userId>/  <- 该账号的 web 登录分区（Cookies 等）
    TeleAgent/current-owner.json               <- 指针：当前激活的账号（ownerDir/userId/tenantId）

因此"切换账号"只需原子改写 current-owner.json（几百字节），无需复制或切换整个应用目录。
旧版整目录快照（TeleAgent-<名字>）可通过 NTFS Junction 零复制导入 accounts，保留原位。

本模块同时保留旧版《会话管理器》的全部能力（set 方式切换整个目录），见 teleagent-session.py。
"""

import json
import os
import re
import shutil
import subprocess
import sys

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SHARE = os.environ.get("SESSION_TOOL_SHARE") or os.path.expandvars(r"%USERPROFILE%\.local\share")
ACTIVE_NAME = "TeleAgent"
PREFIX = "TeleAgent-"
ALIAS_FILE = os.environ.get("SESSION_TOOL_ALIASES") or os.path.join(SHARE, "teleagent-accounts.json")
OWNER_DIR_RE = re.compile(r"^v1_([A-Za-z0-9_]+)_(\d+)$")
OWNER_PART = "owner%3A"


# ---------------------------------------------------------------- 基础工具

def is_junction(path):
    if not os.path.isdir(path):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (st.st_file_attributes & 0x400) != 0 and (st.st_reparse_tag & 0xA0000000) == 0xA0000000


def junction_target(path):
    if not is_junction(path):
        return None
    try:
        return os.readlink(path)
    except OSError:
        return None


def remove_junction(path):
    os.rmdir(path)


def make_junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                       capture_output=True, text=True, encoding="mbcs", errors="replace",
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("mklink /J 失败: {}\n{}".format(link, (r.stderr or r.stdout).strip()))


def dir_size(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for d in list(dirs):
            full = os.path.join(root, d)
            if is_junction(full) or os.path.islink(full):
                dirs.remove(d)
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def mb_str(n):
    if n < 1024 * 1024:
        return "{:.0f} KB".format(n / 1024)
    if n < 1024 * 1024 * 1024:
        return "{:.2f} MB".format(n / 1024 / 1024)
    return "{:.2f} GB".format(n / 1024 / 1024 / 1024)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json_atomic(path, data):
    tmp = path + ".tmp-{}".format(os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def teleagent_running():
    if os.environ.get("SESSION_TOOL_FORCE_NOT_RUNNING") == "1":
        return False
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, encoding="mbcs", errors="replace",
                           creationflags=_NO_WINDOW)
        for line in r.stdout.splitlines():
            parts = line.split('","')
            if len(parts) > 1 and parts[0].lstrip('"').strip() == "TeleAgent.exe":
                return True
        return False
    except OSError:
        return False


def parse_owner_dir(owner_dir):
    m = OWNER_DIR_RE.match(owner_dir or "")
    if not m:
        return None
    return {"owner_dir": owner_dir, "tenant_id": m.group(1), "user_id": m.group(2)}


def active_dir():
    return os.path.join(SHARE, ACTIVE_NAME)


# ---------------------------------------------------------------- owner 账号

def read_current_owner():
    """读取 current-owner.json，返回 {owner_dir, tenant_id, user_id} 或 None。"""
    data = _read_json(os.path.join(active_dir(), "current-owner.json"))
    if not data or not data.get("ownerDir"):
        return None
    return parse_owner_dir(data["ownerDir"])


def list_accounts():
    """扫描 TeleAgent/users/v1_* 返回账号列表。每个账号：owner_dir/tenant_id/user_id/大小/别名/是否当前。"""
    return list_session_accounts(None)


def list_session_accounts(name=None):
    """列出指定会话内的账号；name=None 表示当前激活目录。

    每个账号：owner_dir/tenant_id/user_id/大小/别名/是否当前/是否接入/是否属于激活会话。
    """
    src = active_dir() if name is None else os.path.join(SHARE, PREFIX + name)
    is_active = os.path.normcase(os.path.normpath(src)) == os.path.normcase(os.path.normpath(active_dir()))
    cur = None
    if is_active:
        cur_owner = read_current_owner()
        if cur_owner:
            cur = cur_owner["owner_dir"]
    aliases = load_aliases()
    users = os.path.join(src, "users")
    out = []
    if os.path.isdir(users):
        for entry in sorted(os.listdir(users)):
            info = parse_owner_dir(entry)
            if not info:
                continue
            path = os.path.join(users, entry)
            if not os.path.isdir(path):
                continue
            part = os.path.join(src, "Partitions", OWNER_PART + entry)
            size = dir_size(path)
            if os.path.isdir(part) and not is_junction(part):
                size += dir_size(part)
            elif os.path.isdir(part):
                size += dir_size(part)
            out.append({
                "owner_dir": entry,
                "tenant_id": info["tenant_id"],
                "user_id": info["user_id"],
                "path": path,
                "part": part,
                "size": size,
                "alias": aliases.get(entry, ""),
                "current": is_active and entry == cur,
                "linked": is_junction(path),
                "active": is_active,
            })
    return out


def load_aliases():
    data = _read_json(ALIAS_FILE)
    return data if isinstance(data, dict) else {}


def save_aliases(aliases):
    _write_json_atomic(ALIAS_FILE, aliases)


def set_alias(owner_dir, alias):
    aliases = load_aliases()
    if alias:
        aliases[owner_dir] = alias
    else:
        aliases.pop(owner_dir, None)
    save_aliases(aliases)
    return alias


def switch_owner(owner_dir):
    """轻量切换：原子改写 current-owner.json 指向 owner_dir。返回写入后的 owner 信息。"""
    info = parse_owner_dir(owner_dir)
    if not info:
        raise ValueError("非法 owner 名：{}".format(owner_dir))
    owner_json = os.path.join(active_dir(), "current-owner.json")
    existing = _read_json(owner_json) or {}
    existing["ownerDir"] = info["owner_dir"]
    existing["tenantId"] = info["tenant_id"]
    existing["userId"] = info["user_id"]
    _write_json_atomic(owner_json, existing)
    return info


def delete_account(owner_dir):
    """删除当前激活会话中的一个账号；返回 "linked" 或 "removed"。

    若该账号是接入（Junction）：仅解除关联，源会话中的原数据保留；
    否则永久删除账号数据及相关分区。当前正在使用的账号不可删除。
    """
    info = parse_owner_dir(owner_dir)
    if not info:
        raise RuntimeError("非法 owner 名：{}".format(owner_dir))
    users = os.path.join(active_dir(), "users", owner_dir)
    if not os.path.isdir(users) and not is_junction(users):
        raise RuntimeError("账号不存在：{}".format(owner_dir))
    cur = read_current_owner()
    if cur and cur["owner_dir"] == owner_dir:
        raise RuntimeError("「{}」为当前正在使用的账号，不能删除。".format(owner_dir))
    part = os.path.join(active_dir(), "Partitions", OWNER_PART + owner_dir)
    if is_junction(users):
        remove_junction(users)
        if is_junction(part):
            remove_junction(part)
        mode = "linked"
    else:
        shutil.rmtree(users, ignore_errors=True)
        shutil.rmtree(part, ignore_errors=True)
        mode = "removed"
    aliases = load_aliases()
    aliases.pop(owner_dir, None)
    save_aliases(aliases)
    return mode


# ---------------------------------------------------------------- 快照导入

def import_session(name):
    """把旧快照 TeleAgent-<name> 的账号数据以 Junction 导入激活目录（零复制）。

    支持两种快照形态：
      1) 新版快照：其 users/v1_* 与 Partitions/owner%3Av1_* 按 owner 分别链接；
      2) 旧版快照（无 users/，账号数据在顶层）：目录名即手机号，没有 ownerDir，
         仅当顶层可识别为账号数据时按 legacy 结构链接到该快照本身（不建立 users/v1_*）。
    返回本次导入的 owner 列表。
    """
    dst = active_dir()
    src = os.path.join(SHARE, PREFIX + name)
    if not os.path.isdir(src) or is_junction(src):
        raise RuntimeError("快照不存在或不是真实目录：{}".format(src))

    imported = []

    # 形态 1：新版快照，含 users/
    src_users = os.path.join(src, "users")
    if os.path.isdir(src_users):
        dst_users = os.path.join(dst, "users")
        os.makedirs(dst_users, exist_ok=True)
        for entry in sorted(os.listdir(src_users)):
            if not parse_owner_dir(entry):
                continue
            s = os.path.join(src_users, entry)
            if not os.path.isdir(s):
                continue
            t = os.path.join(dst_users, entry)
            if os.path.exists(t) or is_junction(t):
                continue
            make_junction(t, s)
            imported.append(entry)
        src_parts = os.path.join(src, "Partitions")
        dst_parts = os.path.join(dst, "Partitions")
        os.makedirs(dst_parts, exist_ok=True)
        for entry in sorted(os.listdir(src_parts)):
            if not entry.startswith(OWNER_PART):
                continue
            s = os.path.join(src_parts, entry)
            if not os.path.isdir(s):
                continue
            t = os.path.join(dst_parts, entry)
            if os.path.exists(t) or is_junction(t):
                continue
            make_junction(t, s)
        # 以快照名作为导入账号的显示名称（仅当尚未设置别名时）
        aliases = load_aliases()
        for owner in imported:
            if not aliases.get(owner):
                aliases[owner] = name
        if aliases:
            save_aliases(aliases)
        return imported

    # 形态 2：旧版快照，账号数据在顶层（im-service / teleagent.db / log / memory ...）
    legacy_markers = ("im-service", "teleagent.db", "memory", "scheduler", "log")
    has_legacy = any(os.path.exists(os.path.join(src, m)) for m in legacy_markers)
    if has_legacy:
        raise RuntimeError(
            "快照「{}」是 2.5.2 之前的旧结构（无 users/ 与 current-owner.json），"
            "无法用 Junction 导入；请改用菜单「转换旧快照为账号」将其建成 v1_public_* 账号树。".format(name))
    raise RuntimeError("快照「{}」里没有可导入的账号数据。".format(name))


# ---------------------------------------------------------------- 快照转换

ACCOUNT_COPY_DIRS = ("im-service", "log", "logs", "memory", "scheduler", "state",
                     "tool-output", "playwright-mcp", "bin", "app-auth")
ACCOUNT_COPY_FILES = ("teleagent.db", "auth.json", "onboarding.json", "skills-metadata.json",
                      "skills-dismissed-updates.json", "session-status.json",
                      "user-agent-config.json", "deleted-session-ids.json")
PARTITION_COPY_ITEMS = ("Local Storage", "Network", "Session Storage", "blob_storage",
                        "Shared Storage", "WebStorage", "InterestGroups")


def _skip_links(directory, entries):
    skipped = []
    for e in entries:
        full = os.path.join(directory, e)
        if os.path.islink(full) or is_junction(full):
            skipped.append(e)
    return skipped


def snapshot_kind(name):
    """判断快照形态：new（新版 users/ 结构）/ legacy(旧结构账号树)/ none。"""
    src = os.path.join(SHARE, PREFIX + name)
    if not os.path.isdir(src) or is_junction(src):
        return "none"
    if os.path.isdir(os.path.join(src, "users")):
        return "new"
    if any(os.path.exists(os.path.join(src, m)) for m in ("im-service", "teleagent.db", "memory", "scheduler")):
        return "legacy"
    return "none"


def convert_session(name, owner_dir=None, switch=True):
    """把旧结构快照 TeleAgent-<name>（2.5.2 之前的顶层账号树）转换为新版账号。

    复制构建 users/v1_public_<id>/ 账号树；旧版默认分区的登录态（Cookies/Local
    Storage 等）复制到 Partitions/owner%3Av1_public_<id>/；保持旧快照不变（可回退）。
    owner_dir 缺省为 v1_public_<快照名>（快照名为纯数字时）。
    """
    src = os.path.join(SHARE, PREFIX + name)
    if not os.path.isdir(src) or is_junction(src):
        raise RuntimeError("快照不存在：{}".format(src))
    if snapshot_kind(name) != "legacy":
        if snapshot_kind(name) == "new":
            raise RuntimeError("「{}」已是新版 users/ 结构，请用「导入」接入而不是转换。".format(name))
        raise RuntimeError("快照「{}」里没有可转换的账号数据。".format(name))

    if not owner_dir:
        if name.isdigit():
            owner_dir = "v1_public_" + name
        else:
            raise RuntimeError("快照名不是纯数字，无法自动生成 ownerDir，请手动指定。")
    info = parse_owner_dir(owner_dir)
    if not info:
        raise RuntimeError("非法 owner 名：{}（格式 v1_<tenant>_<数字userId>）".format(owner_dir))

    dst_root = os.path.join(active_dir(), "users", info["owner_dir"])
    if os.path.exists(dst_root) or is_junction(dst_root):
        raise RuntimeError("账号 {} 已存在，不能重复转换。".format(info["owner_dir"]))

    os.makedirs(dst_root, exist_ok=True)
    copied = []
    part_root = None
    try:
        for item in ACCOUNT_COPY_DIRS + ACCOUNT_COPY_FILES:
            s = os.path.join(src, item)
            if not os.path.exists(s):
                continue
            d = os.path.join(dst_root, item)
            if os.path.isdir(s) and not (os.path.islink(s) or is_junction(s)):
                shutil.copytree(s, d, ignore=_skip_links)
            elif not os.path.isdir(s):
                shutil.copy2(s, d)
            else:
                continue
            copied.append(item)

        part_root = os.path.join(active_dir(), "Partitions", OWNER_PART + info["owner_dir"])
        os.makedirs(part_root, exist_ok=True)
        for item in PARTITION_COPY_ITEMS:
            s = os.path.join(src, item)
            if not os.path.exists(s):
                continue
            d = os.path.join(part_root, item)
            if os.path.isdir(s) and not (os.path.islink(s) or is_junction(s)):
                shutil.copytree(s, d, ignore=_skip_links)
            elif not os.path.isdir(s):
                shutil.copy2(s, d)
            else:
                continue
            copied.append(item)
    except Exception:
        shutil.rmtree(dst_root, ignore_errors=True)
        if part_root is not None:
            shutil.rmtree(part_root, ignore_errors=True)
        raise

    if not copied:
        shutil.rmtree(dst_root, ignore_errors=True)
        shutil.rmtree(part_root, ignore_errors=True)
        raise RuntimeError("快照「{}」中没有任何账号数据可转换。".format(name))

    # 以快照名作为账号显示名称（仅当尚未设置别名时）
    aliases = load_aliases()
    if not aliases.get(info["owner_dir"]):
        set_alias(info["owner_dir"], name)

    if switch:
        switch_owner(info["owner_dir"])
    return info, copied


# ---------------------------------------------------------------- 账号操作工具

def _norm_target(path):
    """规范化 Junction 目标路径（去掉 Windows 设备命名空间前缀）。"""
    p = (path or "").strip()
    if p.startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[len("\\\\?\\UNC\\"):]
    elif p.startswith("\\\\?\\"):
        p = p[len("\\\\?\\"):]
    elif p.startswith("\\??\\"):
        p = p[len("\\??\\"):]
    return os.path.normcase(os.path.normpath(p))


def snapshot_links(name):
    """返回活跃目录中所有指向快照 TeleAgent-<name> 的 Junction 列表，每项 (junction_path, label)。"""
    norm = _norm_target(os.path.join(SHARE, PREFIX + name))
    out = []
    for sub in ("users", "Partitions"):
        base = os.path.join(active_dir(), sub)
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            p = os.path.join(base, entry)
            if not is_junction(p):
                continue
            if _norm_target(junction_target(p)).startswith(norm):
                out.append((p, "{}/{}".format(sub, entry)))
    return out


def detach_account(owner_dir):
    """把已接入（Junction）的账号复制为正式副本，不再依赖原快照；返回原快照路径。"""
    info = parse_owner_dir(owner_dir)
    if not info:
        raise RuntimeError("非法 owner 名：{}".format(owner_dir))
    users = os.path.join(active_dir(), "users", owner_dir)
    if not is_junction(users):
        raise RuntimeError("「{}」不是接入账号，无需转换。".format(owner_dir))
    src = junction_target(users)
    tmp = users + ".copy-tmp-{}".format(os.getpid())
    shutil.copytree(src, tmp, ignore=lambda _d, _fs: [e for e in _fs if os.path.islink(os.path.join(_d, e)) or is_junction(os.path.join(_d, e))])
    remove_junction(users)
    os.rename(tmp, users)
    part = os.path.join(active_dir(), "Partitions", OWNER_PART + owner_dir)
    if is_junction(part):
        psrc = junction_target(part)
        ptmp = part + ".copy-tmp-{}".format(os.getpid())
        shutil.copytree(psrc, ptmp, ignore=lambda _d, _fs: [e for e in _fs if os.path.islink(os.path.join(_d, e)) or is_junction(os.path.join(_d, e))])
        remove_junction(part)
        os.rename(ptmp, part)
    return src


def new_owner(display_name=None):
    """创建一个新的空账号目录并切换到该账号，返回 (owner_dir, display_name)。"""
    import time as _time
    uid = "v1_public_" + str(int(_time.time() * 1000))
    dst_users = os.path.join(active_dir(), "users", uid)
    os.makedirs(dst_users, exist_ok=True)
    dst_parts = os.path.join(active_dir(), "Partitions", OWNER_PART + uid)
    os.makedirs(dst_parts, exist_ok=True)
    switch_owner(uid)
    name = (display_name or "").strip()
    if name:
        set_alias(uid, name)
    return uid, name


def terminate_teleagent(force=False):
    """尝试关闭 TeleAgent；返回 True 表示已结束。"""
    args = ["taskkill", "/IM", "TeleAgent.exe", "/T"]
    if force:
        args.append("/F")
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="mbcs", errors="replace",
                           creationflags=_NO_WINDOW)
        return r.returncode == 0
    except OSError:
        return False


# ---------------------------------------------------------------- CLI

def ask(prompt, default=None):
    tip = " [{}]: ".format(default) if default else ": "
    raw = input(prompt + tip).strip()
    return raw or default or ""


def confirm_prompt(prompt):
    return ask(prompt + " (y/n)", "n").lower() in ("y", "yes")


def _row(info):
    alias = info["alias"]
    label = "{}".format(alias) if alias else info["user_id"]
    tag = "  <- 当前" if info["current"] else ""
    link = " [junction]" if info["linked"] else ""
    return "{}. {}  ({}){}{}".format(label, info["owner_dir"], mb_str(info["size"]), link, tag)


def cmd_list():
    print("\n当前激活目录：{}".format(active_dir()))
    cur = read_current_owner()
    print("current-owner.json: {}".format(cur["owner_dir"] if cur else "（不存在/未初始化）"))
    accounts = list_accounts()
    if not accounts:
        print("\n`users/` 下暂无 v1_* 账号。可先登录一个账号，或用「导入」把旧快照接进来。")
        return
    print("\n账号列表：")
    for i, info in enumerate(accounts, 1):
        print("  " + _row(info))
    print("  0. 返回")


def cmd_switch():
    accounts = list_accounts()
    if not accounts:
        print("\n没有可切换的账号。")
        return
    cur = read_current_owner()
    print("\n账号列表：")
    for i, info in enumerate(accounts, 1):
        print("  {:>2}. {}".format(i, _row(info)))
    pick = ask("\n选择要切换的账号编号", "0")
    if not pick.isdigit() or int(pick) == 0:
        print("已取消。")
        return
    idx = int(pick)
    if not (1 <= idx <= len(accounts)):
        print("编号超出范围。")
        return
    info = accounts[idx - 1]
    if info["current"]:
        print("「{}」已经是当前账号。".format(info["owner_dir"]))
        return
    if teleagent_running():
        print("注意：TeleAgent 正在运行。切换后需重启应用才会加载新账号（current-owner.json 改动后运行中的进程可能回写覆盖）。")
        if not confirm_prompt("仍然继续？"):
            print("已取消。")
            return
    switch_owner(info["owner_dir"])
    print("已切换账号 -> {}（{}）。请重启 TeleAgent。".format(info["owner_dir"], info["alias"] or info["user_id"]))


def cmd_import():
    sessions = []
    if os.path.isdir(SHARE):
        for entry in sorted(os.listdir(SHARE)):
            full = os.path.join(SHARE, entry)
            if entry.startswith(PREFIX) and os.path.isdir(full) and not is_junction(full):
                sessions.append(entry[len(PREFIX):])
    if not sessions:
        print("\n没有可导入的旧快照（未找到 {}*）。".format(PREFIX))
        return
    print("\n旧快照列表（可导入其账号数据）：")
    for i, name in enumerate(sessions, 1):
        print("  {:>2}. {}  ({})".format(i, name, mb_str(dir_size(os.path.join(SHARE, PREFIX + name)))))
    pick = ask("选择要导入的快照编号", "0")
    if not pick.isdigit() or int(pick) == 0:
        print("已取消。")
        return
    idx = int(pick)
    if not (1 <= idx <= len(sessions)):
        print("编号超出范围。")
        return
    name = sessions[idx - 1]
    if snapshot_kind(name) == "legacy":
        print("该快照是 2.5.2 之前的旧结构，请使用菜单「4. 转换旧快照为账号」处理。")
        return
    if teleagent_running() and not confirm_prompt("TeleAgent 正在运行，导入操作最好在退出应用后进行，仍然继续？"):
        return
    try:
        imported = import_session(name)
    except RuntimeError as e:
        print("导入失败：{}".format(e))
        return
    if not imported:
        print("快照「{}」没有可导入的账号。".format(name))
        return
    print("已导入 {} 个账号（Junction 指向快照，零复制）：{}".format(len(imported), ", ".join(imported)))
    print("现在可在「切换账号」中选择它们。建议顺带设置别名：")


def cmd_convert():
    legacy = []
    if os.path.isdir(SHARE):
        for entry in sorted(os.listdir(SHARE)):
            name = entry[len(PREFIX):]
            if entry.startswith(PREFIX) and snapshot_kind(name) == "legacy":
                legacy.append(name)
    if not legacy:
        print("\n没有可转换的旧结构快照（2.5.2 之前的顶层账号树）。")
        return
    print("\n旧结构快照列表（可转换为账号）：")
    for i, name in enumerate(legacy, 1):
        print("  {:>2}. {}  ({})".format(i, name, mb_str(dir_size(os.path.join(SHARE, PREFIX + name)))))
    pick = ask("选择要转换的快照编号", "0")
    if not pick.isdigit() or int(pick) == 0:
        print("已取消。")
        return
    idx = int(pick)
    if not (1 <= idx <= len(legacy)):
        print("编号超出范围。")
        return
    name = legacy[idx - 1]
    default = "v1_public_" + name if name.isdigit() else ""
    hint = "（默认 {}）".format(default) if default else "（快照名非数字，必须指定）"
    owner_dir = ask("目标 ownerDir{}：".format(hint), default or "")
    if not parse_owner_dir(owner_dir or default):
        print("ownerDir 不合法（格式 v1_<tenant>_<数字userId>）或未指定。")
        return
    link = os.path.join(active_dir(), "users", owner_dir or default)
    if os.path.exists(link) or is_junction(link):
        print("账号 {} 已存在。".format(owner_dir or default))
        return
    if teleagent_running() and not confirm_prompt("TeleAgent 正在运行，转换后需重启才生效，仍然继续？"):
        return
    try:
        info, copied = convert_session(name, owner_dir or default, switch=True)
    except RuntimeError as e:
        print("转换失败：{}".format(e))
        return
    print("已转换快照「{}」-> 账号 {}（复制了 {} 项，旧快照保留）。".format(name, info["owner_dir"], len(copied)))
    print("已设为当前账号，请重启 TeleAgent 生效。若登录态未恢复，可重新登录该账号一次。")


def cmd_alias():
    accounts = list_accounts()
    if not accounts:
        print("\n没有账号可设置别名。")
        return
    print("\n账号列表：")
    for i, info in enumerate(accounts, 1):
        print("  {:>2}. {}  [{}]".format(i, info["owner_dir"], info["alias"] or "无别名"))
    pick = ask("选择要设置别名的账号编号", "0")
    if not pick.isdigit() or int(pick) == 0:
        print("已取消。")
        return
    idx = int(pick)
    if not (1 <= idx <= len(accounts)):
        print("编号超出范围。")
        return
    info = accounts[idx - 1]
    alias = ask("别名（留空清除）：", info["alias"]).strip()
    alias = None if not alias else alias
    set_alias(info["owner_dir"], alias or "")
    print("账号 {} 的别名为：{}".format(info["owner_dir"], alias or "（无）"))


MENU = """
========================================
  TeleAgent 账号管理器（Owner 轻量切换）
  目录：{share}
  KEY：current-owner.json + users/v1_*
========================================
  1. 列出账号
  2. 切换账号     （改 current-owner.json，重启生效）
  3. 导入旧快照   （新版 users/ 快照，Junction 零复制接入）
  4. 转换旧快照   （旧结构 2.5.2 之前快照，复制构建为账号树）
  5. 设置别名
  6. 查看状态
  0. 退出
========================================
""".format(share=SHARE)


def cmd_status():
    dst = active_dir()
    print("\n激活目录：{}".format(dst))
    cur = read_current_owner()
    print("当前账号：{}".format(cur["owner_dir"] if cur else "（未初始化 current-owner.json）"))
    if cur:
        users = os.path.join(dst, "users", cur["owner_dir"])
        part = os.path.join(dst, "Partitions", OWNER_PART + cur["owner_dir"])
        print("  users 目录：{} （{}{}）".format(users, "存在, " if os.path.isdir(users) else "缺失, ", "Junction" if is_junction(users) else "真实目录" if os.path.isdir(users) else "-"))
        print("  partition ：{}（{}）".format(part, "存在" if os.path.isdir(part) else "缺失"))
    accounts = list_accounts()
    print("账号总数：{}".format(len(accounts)))
    for info in accounts:
        print("  - {}  [{}]{}".format(info["owner_dir"], info["alias"] or info["user_id"], "  <- 当前" if info["current"] else ""))


def main():
    if sys.platform != "win32":
        print("此工具仅支持 Windows（依赖 NTFS Junction）。")
        sys.exit(1)
    dst = active_dir()
    if not os.path.isdir(dst):
        print("目录不存在：{}（TeleAgent 可能尚未运行过，请先登录一次任意账号。）".format(dst))
        sys.exit(1)
    actions = {"1": cmd_list, "2": cmd_switch, "3": cmd_import, "4": cmd_convert, "5": cmd_alias, "6": cmd_status}
    while True:
        print(MENU)
        choice = ask("请选择操作", "0")
        if choice == "0":
            break
        action = actions.get(choice)
        if not action:
            print("无效选择。")
            continue
        try:
            action()
        except KeyboardInterrupt:
            print("\n操作已中断。")
        except Exception as e:
            print("出错了：{}".format(e))
        try:
            input("\n按回车返回菜单...")
        except EOFError:
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()