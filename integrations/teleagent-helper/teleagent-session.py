#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TeleAgent 会话管理器 —— 保存 / 切换 / 新建会话（基于 NTFS Junction）

目录布局（SHARE 为 用户目录 .local\\share）：
    TeleAgent         <- 应用实际读写的数据目录
    TeleAgent-<名字>   <- 会话快照目录（真实目录）

设计：
    读取/切换：列出 TeleAgent-*，选一个 → 把 TeleAgent 指向它的 Junction（mklink /J）
    保存：      输入名字 → 把当前 TeleAgent 整棵复制为 TeleAgent-<名字>（Junction 不变）
    新建：      输入名字 → 新建空目录 TeleAgent-<名字> → 把 TeleAgent 的 Junction 指向它
"""

import os
import re
import shutil
import subprocess
import sys

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SHARE = os.environ.get("SESSION_TOOL_SHARE") or os.path.expandvars(r"%USERPROFILE%\.local\share")
ACTIVE_NAME = "TeleAgent"
PREFIX = "TeleAgent-"
APP_EXE = r"C:\Program Files\TeleAgent\TeleAgent.exe"  # 仅供提示，不强制


# ---------------------------------------------------------------- 基础工具

def is_junction(path):
    """判断 path 是否为 Junction（目录 + 重解析点 + reparse tag 0xA0000000 开头）。"""
    if not os.path.isdir(path):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (st.st_file_attributes & 0x400) != 0 and (st.st_reparse_tag & 0xA0000000) == 0xA0000000


def junction_target(path):
    """返回 Junction 指向的真实路径；不是 Junction 则返回 None。"""
    if not is_junction(path):
        return None
    try:
        return os.readlink(path)
    except OSError:
        return None


def make_junction(link, target):
    """mklink /J link target（等价于用户要求的 mklink /j）。"""
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                       capture_output=True, text=True, encoding="mbcs", errors="replace",
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("mklink /J 失败: {}\n{}".format(link, (r.stderr or r.stdout).strip()))


def remove_junction(path):
    """只删除 Junction 本身（rmdir），绝不触碰目标目录。"""
    os.rmdir(path)


def list_sessions():
    """列出 SHARE 下所有 TeleAgent-xxx 真实目录。返回 [(名字, 绝对路径), ...]。"""
    if not os.path.isdir(SHARE):
        return []
    out = []
    for name in os.listdir(SHARE):
        if not name.startswith(PREFIX):
            continue
        full = os.path.join(SHARE, name)
        if os.path.isdir(full) and not is_junction(full):
            out.append((name[len(PREFIX):], full))
    return sorted(out)


def current_session():
    """当前激活会话名（TeleAgent Junction 的指向目录名，去前缀）。"""
    target = junction_target(os.path.join(SHARE, ACTIVE_NAME))
    if not target:
        return None
    base = os.path.basename(os.path.normpath(target))
    return base[len(PREFIX):] if base.startswith(PREFIX) else base


def validate_name(name):
    return bool(re.fullmatch(r"[0-9A-Za-z_.\-]{1,40}", name))


def teleagent_running():
    """精确匹配进程名 TeleAgent.exe（避免 tasklist 匹配到其它含相同片段的进程）。"""
    if os.environ.get("SESSION_TOOL_FORCE_NOT_RUNNING") == "1":
        return False  # 测试用开关：绕过运行中检测
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


def ask(prompt, default=None):
    tip = " [{}]: ".format(default) if default else ": "
    raw = input(prompt + tip).strip()
    return raw or default or ""


def confirm(prompt):
    return ask(prompt + " (y/n)", "n").lower() in ("y", "yes")


def mb_to_str(nbytes):
    if nbytes < 1024 * 1024:
        return "{:.0f} KB".format(nbytes / 1024)
    return "{:.2f} MB".format(nbytes / 1024 / 1024)


def dir_size(path):
    """递归统计目录大小；跳过 Junction/符号链接，避免绕圈。"""
    total = 0
    for root, dirs, files in os.walk(path):
        for d in list(dirs):
            if is_junction(os.path.join(root, d)):
                dirs.remove(d)
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ------------------------------------------------------------ 核心操作

def switch():
    """读取/切换：列出所有 TeleAgent-xxx，选一个后把 TeleAgent 的 Junction 指向它。"""
    sessions = list_sessions()
    if not sessions:
        print("没有任何已保存的会话（未找到 {}* 目录）。".format(PREFIX))
        return

    cur = current_session()
    print("\n已保存的会话：")
    for i, (name, path) in enumerate(sessions, 1):
        size = dir_size(path)
        tag = "  <- 当前" if name == cur else ""
        print("  {:>2}. {}  ({}){}".format(i, name, mb_to_str(size), tag))
    print("   0. 取消")

    pick = ask("选择要切换的会话编号", "0")
    if not pick.isdigit() or int(pick) == 0:
        print("已取消。")
        return
    idx = int(pick)
    if not (1 <= idx <= len(sessions)):
        print("编号超出范围。")
        return
    name, path = sessions[idx - 1]
    if name == cur:
        print("「{}」已经是当前会话，无需切换。".format(name))
        return

    if teleagent_running():
        print("注意：TeleAgent 正在运行。切换后应用下次启动才会读新目录；"
              "为避免数据写回旧目录，建议先退出应用再切换。")
        if not confirm("仍然继续？"):
            print("已取消。")
            return

    link = os.path.join(SHARE, ACTIVE_NAME)
    if is_junction(link):
        remove_junction(link)
    elif os.path.exists(link):
        if not confirm("TeleAgent 目前是真实目录（不是 Junction）。先把它保存为会话 default 再切换？"):
            print("已取消。")
            return
        _save_current_as("default")
    make_junction(link, path)
    print("已切换到会话「{}」。".format(name))


def _save_current_as(name):
    """把当前 TeleAgent（Junction 背后的真实目录）复制为快照 TeleAgent-<name>。"""
    link = os.path.join(SHARE, ACTIVE_NAME)
    src = junction_target(link) or link
    dst = os.path.join(SHARE, PREFIX + name)
    if os.path.exists(dst) or is_junction(dst):
        raise RuntimeError("会话「{}」已存在：{}".format(name, dst))

    print("正在复制 {} -> {} ...".format(src, dst))
    # 跳过目录内所有 Junction / 符号链接，只复制真实文件，避免快照间互相污染
    shutil.copytree(src, dst, ignore=_skip_links, copy_function=shutil.copy2)
    size = dir_size(dst)
    print("已保存会话「{}」（{}）。".format(name, mb_to_str(size)))


def _skip_links(directory, entries):
    """copytree 的 ignore 回调：跳过所有链接项（Junction/symlink）。"""
    skipped = []
    for e in entries:
        full = os.path.join(directory, e)
        if os.path.islink(full) or is_junction(full):
            skipped.append(e)
    return skipped


def save():
    """保存：输入名字，把当前 TeleAgent 复制为 TeleAgent-<名字>，当前激活状态不变。"""
    cur = current_session()
    print("\n当前会话：{}".format(cur or "（TeleAgent 不是 Junction，将直接复制 TeleAgent 目录）"))
    name = ask("保存为会话名（字母数字-_点）")
    if not name:
        print("名字不能为空。")
        return
    if not validate_name(name):
        print("名字不合法：只允许字母数字、下划线、点、连字符，长度 1-40。")
        return
    if os.path.exists(os.path.join(SHARE, PREFIX + name)) or is_junction(os.path.join(SHARE, PREFIX + name)):
        print("会话「{}」已存在。".format(name))
        return
    if teleagent_running():
        print("注意：TeleAgent 正在运行，保存的可能是中间状态（建议先退出应用再保存）。")

    _save_current_as(name)


def status():
    """显示当前状态：TeleAgent 指向、已保存会话列表。"""
    link = os.path.join(SHARE, ACTIVE_NAME)
    target = junction_target(link)
    if target:
        print("当前激活：{} -> {}".format(ACTIVE_NAME, target))
    elif os.path.exists(link):
        print("当前激活：{}（真实目录，未使用 Junction；可先 save 或 create 建立快照机制）".format(ACTIVE_NAME))
    else:
        print("未找到 {}（TeleAgent 可能尚未运行过）。".format(link))

    sessions = list_sessions()
    if not sessions:
        print("已保存会话：无")
        return
    cur = current_session()
    print("已保存会话：")
    for name, path in sessions:
        size = dir_size(path)
        tag = "  <- 当前" if name == cur else ""
        print("  - {}  ({}){}".format(name, mb_to_str(size), tag))


MENU = """
========================================
  TeleAgent 会话管理器
  目录：{share}
========================================
  1. 切换会话     （TeleAgent -> 已保存的 TeleAgent-名字）
  2. 保存当前会话 （复制当前 TeleAgent 为 TeleAgent-名字）
  3. 新建会话     （新建 TeleAgent-名字 并指向它）
  4. 查看状态
  5. 账号管理器   （Owner 轻量切换，2.5.2+ users/current-owner 结构）
  0. 退出
========================================
""".format(share=SHARE)


def owner_menu():
    """转交账号管理器（teleagent_accounts.py）。当前 SHARE 环境变量一并生效。"""
    import teleagent_accounts
    teleagent_accounts.main()


def main():
    if sys.platform != "win32":
        print("此工具仅支持 Windows（依赖 NTFS Junction）。")
        sys.exit(1)
    if not os.path.isdir(SHARE):
        print("目录不存在：{}".format(SHARE))
        sys.exit(1)

    actions = {"1": switch, "2": save, "3": create, "4": status, "5": owner_menu}
    while True:
        print(MENU)
        choice = ask("请选择操作", "0")
        if choice == "0":
            break
        action = actions.get(choice)
        if action is None:
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


def create():
    """新建：创建 TeleAgent-<名字> 空目录，再把 TeleAgent 的 Junction 指向它。"""
    link = os.path.join(SHARE, ACTIVE_NAME)
    name = ask("请输入新会话名（字母数字-_点）")
    if not name:
        print("名字不能为空。")
        return
    if not validate_name(name):
        print("名字不合法：只允许字母数字、下划线、点、连字符，长度 1-40。")
        return
    dst = os.path.join(SHARE, PREFIX + name)
    if os.path.exists(dst) or is_junction(dst):
        print("会话「{}」已存在。".format(name))
        return

    if teleagent_running():
        print("注意：TeleAgent 正在运行。新建后 Junction 将指向新目录，旧目录原样保留。")
        if not confirm("仍然继续？"):
            print("已取消。")
            return

    os.makedirs(dst, exist_ok=True)
    if is_junction(link):
        remove_junction(link)
    elif os.path.exists(link):
        if not confirm("TeleAgent 目前是真实目录，先把它保存为会话 default 再指向新会话？"):
            print("已取消。")
            return
        _save_current_as("default")
    make_junction(link, dst)
    print("已新建并切换到会话「{}」。".format(name))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()