#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TeleAgent 数据管理器（左右两列版，PyQt6）

模型：一个「会话」就是一份完整、独立可用的数据，里面带有若干个账号。
     左侧列出所有会话，右侧显示所选会话里的账号；只有带对勾标记（正在
     使用）的会话才允许对其中的账号执行切换 / 新建 / 改名 / 删除等操作。
"""
import os, re, shutil, subprocess, sys, json
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QAction, QKeySequence, QIcon, QPainter, QPixmap, QColor, QPen
from PyQt6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
                             QWidget, QTreeWidget, QTreeWidgetItem, QGroupBox,
                             QListWidget, QListWidgetItem,
                             QLabel, QPushButton, QLineEdit, QMessageBox,
                             QProgressBar, QStatusBar, QAbstractItemView,
                             QStyle, QFileIconProvider, QDialog,
                             QDialogButtonBox, QFrame, QCheckBox)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import teleagent_accounts as ta

SHARE = ta.SHARE
ACTIVE_NAME = ta.ACTIVE_NAME
PREFIX = ta.PREFIX
USERS_DIR = "users"
NAME_RE = re.compile(r"^[0-9A-Za-z_.\-]{1,40}$")
BANNER_FLAG = "teleagent-gui-banner-shown.json"
REAL_ROW = "__current_real__"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _make_button_icons(style):
    """统一的按钮图标：同一操作在两侧面板使用同一图标。"""
    pm = QPixmap(16, 16)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#3574a6"), 1.2)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(4, 3, 8, 10, 2, 2)
    p.drawLine(6, 7, 10, 7)
    p.drawLine(6, 10, 9, 10)
    p.end()
    return {
        "switch": style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton),
        "save": style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton),
        "new": style.standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder),
        "delete": style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon),
        "refresh": style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
        "rename": QIcon(pm),
        "detach": style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward),
        "import": style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton),
    }


# ================================================================ 基础工具

def is_junction(path):
    if not os.path.isfile(path) and not os.path.isdir(path):
        return False
    try:
        st = os.lstat(path)
        return bool(st.st_file_attributes & 0x400)
    except OSError:
        return False


def junction_target(path):
    if not is_junction(path):
        return None
    try:
        return os.readlink(path)
    except OSError:
        return None


def make_junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                       capture_output=True, text=True, encoding="mbcs", errors="replace",
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("mklink 失败: {}\n{}".format(link, (r.stderr or r.stdout).strip()))


def remove_junction(path):
    os.rmdir(path)


def list_sessions():
    if not os.path.isdir(SHARE):
        return []
    out = []
    for name in os.listdir(SHARE):
        if not name.startswith(PREFIX):
            continue
        full = os.path.join(SHARE, name)
        if not os.path.isdir(full):
            continue
        if is_junction(full):
            continue
        out.append((name[len(PREFIX):], full))
    return sorted(out)


def current_session():
    target = junction_target(os.path.join(SHARE, ACTIVE_NAME))
    if not target:
        return None
    base = os.path.basename(os.path.normpath(target))
    if base.startswith(PREFIX):
        return base[len(PREFIX):]
    return base


def teleagent_running():
    if os.environ.get("SESSION_TOOL_FORCE_NOT_RUNNING") == "1":
        return False
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, encoding="mbcs", errors="replace",
                           creationflags=_NO_WINDOW)
        for line in r.stdout.splitlines():
            parts = line.split('","')
            if len(parts) > 1 and parts[0].lstrip('"').strip().split('.')[0].lower() == "teleagent":
                return True
    except Exception:
        pass
    return False


def dir_size(path):
    total = 0
    try:
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
    except OSError:
        return 0


def mb_str(n):
    if n < 1_048_576:
        return "{:.0f} KB".format(n / 1024)
    if n < 1_073_741_824:
        return "{:.2f} MB".format(n / 1024 / 1024)
    return "{:.2f} GB".format(n / 1024 / 1024 / 1024)


def _skip_links(directory, entries):
    skipped = []
    for e in entries:
        full = os.path.join(directory, e)
        if os.path.islink(full) or is_junction(full):
            skipped.append(e)
    return skipped


def copy_session(src, dst, on_progress=None, cancelled=None):
    total = dir_size(src)
    done = [0]

    def _cp(s, d):
        shutil.copy2(s, d)
        try:
            done[0] += os.path.getsize(s)
        except OSError:
            pass
        if on_progress:
            on_progress(done[0], total)
        if cancelled and cancelled():
            raise RuntimeError("操作已取消")

    shutil.copytree(src, dst, ignore=_skip_links, copy_function=_cp)
    return total


# ================================================================ 后台线程

class CopyWorker(QThread):
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, src, dst, name):
        super().__init__()
        self.src, self.dst, self.name = src, dst, name
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            copy_session(self.src, self.dst,
                         on_progress=lambda d, t: self.progress.emit(d, t),
                         cancelled=lambda: self._cancel)
            self.finished_ok.emit(self.name)
        except Exception as e:
            self.failed.emit(str(e))


class SwitchWorker(QThread):
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, name):
        super().__init__()
        self.name = name

    def run(self):
        try:
            link = os.path.join(SHARE, ACTIVE_NAME)
            if is_junction(link):
                remove_junction(link)
            elif os.path.isdir(link):
                shutil.rmtree(link, ignore_errors=True)   # 真实目录已先保存为快照，可安全移除
            make_junction(link, os.path.join(SHARE, PREFIX + self.name))
            self.finished_ok.emit(self.name)
        except Exception as e:
            self.failed.emit(str(e))


# ================================================================ 主窗口

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TeleAgent 数据管理器")
        self.resize(680, 500)
        self.sessions = []
        self._cur = None
        self.accounts = []
        self._acct_cur = None
        self.worker = None
        self._pending = None          # ("switch", 目标会话名) 等保存 default 完成后继续
        self._build_ui()
        self.refresh_all()
        self._maybe_show_banner()

    # ------------------------------------------------------- 界面

    def _build_ui(self):
        style = self.style()
        icons = _make_button_icons(style)
        ico_refresh = icons["refresh"]
        ico_save = icons["save"]
        ico_new = icons["new"]
        ico_delete = icons["delete"]
        ico_about = style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)
        ico_quit = style.standardIcon(QStyle.StandardPixmap.SP_DialogCloseButton)
        provider = QFileIconProvider()
        ico_folder = provider.icon(QFileIconProvider.IconType.Folder)

        # ---- 菜单栏
        menubar = self.menuBar()
        m_file = menubar.addMenu("文件(&F)")
        self.act_refresh = QAction(ico_refresh, "刷新列表(&R)", self)
        self.act_refresh.setShortcut(QKeySequence("F5"))
        self.act_refresh.triggered.connect(self.refresh_all)
        m_file.addAction(self.act_refresh)
        m_file.addSeparator()
        self.act_quit = QAction(ico_quit, "退出(&X)", self)
        self.act_quit.setShortcut(QKeySequence("Alt+F4"))
        self.act_quit.triggered.connect(self.close)
        m_file.addAction(self.act_quit)

        m_help = menubar.addMenu("帮助(&H)")
        self.act_guide = QAction(ico_about, "使用说明(&G)", self)
        self.act_guide.triggered.connect(lambda: self._show_banner(True))
        m_help.addAction(self.act_guide)
        self.act_about = QAction(ico_about, "关于(&A)", self)
        self.act_about.triggered.connect(self._about)
        m_help.addAction(self.act_about)

        # ---- 中央区域：引导横幅 + 左右两列
        central = QWidget()
        self.root_lay = QVBoxLayout(central)
        self.root_lay.setContentsMargins(4, 4, 4, 4)
        self.root_lay.setSpacing(2)
        self.banner = None
        columns = QHBoxLayout()
        columns.setSpacing(6)
        p_left = self._build_panel_sessions(icons)
        p_left.setMaximumWidth(360)
        p_right = self._build_panel_accounts(icons)
        columns.addWidget(p_left, 2)
        columns.addWidget(p_right, 3)
        self.root_lay.addLayout(columns, 1)
        self.setCentralWidget(central)

        # ---- 状态栏
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.st_msg = QLabel()
        sb.addWidget(self.st_msg, 2)
        self.st_count = QLabel()
        sb.addPermanentWidget(self.st_count)
        self.st_run = QLabel()
        sb.addPermanentWidget(self.st_run)
        self.progress = QProgressBar()
        self.progress.setMaximumHeight(16)
        self.progress.setVisible(False)
        sb.addWidget(self.progress)

        # Delete 键：光标在哪一列，就删除哪一项（会话或账号）
        self.act_del_shortcut = QAction("delete", self)
        self.act_del_shortcut.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        self.act_del_shortcut.triggered.connect(self._on_delete_pressed)
        self.addAction(self.act_del_shortcut)

    def _build_panel_accounts(self, icons):
        self.acct_group = QGroupBox("账号")
        v = QVBoxLayout(self.acct_group)
        v.setContentsMargins(6, 4, 6, 6)
        v.setSpacing(4)
        self.acct_list = QTreeWidget()
        self.acct_list.setColumnCount(4)
        self.acct_list.setHeaderLabels(["名称", "编号", "大小", "状态"])
        self.acct_list.setRootIsDecorated(False)
        self.acct_list.setAlternatingRowColors(True)
        self.acct_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.acct_list.setIconSize(QSize(16, 16))
        hdr = self.acct_list.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, hdr.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, hdr.ResizeMode.ResizeToContents)
        self.acct_list.currentItemChanged.connect(lambda _a, _b: self._on_acct_select())
        self.acct_list.itemDoubleClicked.connect(lambda _i: self._do_account_switch())
        v.addWidget(self.acct_list, 1)
        self.acct_hint = QLabel()
        self.acct_hint.setWordWrap(True)
        v.addWidget(self.acct_hint)

        self.btn_acct_switch = QPushButton(icons["switch"], "设为当前")
        self.btn_acct_switch.setToolTip("将所选账号设为当前正在使用的账号，仅更新配置，不移动数据")
        self.btn_acct_switch.clicked.connect(self._do_account_switch)
        self.btn_acct_switch.setEnabled(False)
        self.btn_alias = QPushButton(icons["rename"], "改名")
        self.btn_alias.setToolTip("为所选账号设置一个便于识别的名称")
        self.btn_alias.clicked.connect(self._do_alias)
        self.btn_alias.setEnabled(False)
        self.btn_acct_new = QPushButton(icons["new"], "新建账号")
        self.btn_acct_new.setToolTip("在当前会话中创建一个空白账号：重新启动 TeleAgent 后登录，该账号融入本会话")
        self.btn_acct_new.clicked.connect(self._do_new_account)
        self.btn_acct_detach = QPushButton(icons["detach"], "转为正式")
        self.btn_acct_detach.setToolTip("将所选接入账号的数据完整复制到当前会话，解除对原会话的依赖")
        self.btn_acct_detach.clicked.connect(self._do_detach_account)
        self.btn_acct_detach.setEnabled(False)
        self.btn_acct_delete = QPushButton(icons["delete"], "删除")
        self.btn_acct_delete.setToolTip("删除所选账号：接入账号仅解除关联且原数据保留；正式账号则永久删除")
        self.btn_acct_delete.clicked.connect(self._do_account_delete)
        self.btn_acct_delete.setEnabled(False)
        self.btn_acct_refresh = QPushButton(icons["refresh"], "刷新")
        self.btn_acct_refresh.setToolTip("重新扫描账号列表")
        self.btn_acct_refresh.clicked.connect(self.refresh_all)
        self.btn_import = QPushButton(icons["import"], "接入 / 转换旧数据…")
        self.btn_import.setToolTip("把旧版本数据目录接入当前会话：新版格式直接关联，旧版格式转为账号，原目录均保留")
        self.btn_import.clicked.connect(self._import_or_convert)

        row1 = QHBoxLayout()
        row1.addWidget(self.btn_acct_switch)
        row1.addWidget(self.btn_acct_new)
        row1.addWidget(self.btn_alias)
        v.addLayout(row1)
        row2 = QHBoxLayout()
        row2.addWidget(self.btn_acct_detach)
        row2.addWidget(self.btn_acct_delete)
        row2.addWidget(self.btn_acct_refresh)
        v.addLayout(row2)
        v.addWidget(self.btn_import)
        return self.acct_group

    def _build_panel_sessions(self, icons):
        self.ses_group = QGroupBox("会话")
        v = QVBoxLayout(self.ses_group)
        v.setContentsMargins(6, 4, 6, 6)
        v.setSpacing(4)
        self.ses_list = QTreeWidget()
        self.ses_list.setColumnCount(3)
        self.ses_list.setHeaderLabels(["会话", "大小", "状态"])
        self.ses_list.setRootIsDecorated(False)
        self.ses_list.setAlternatingRowColors(True)
        self.ses_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.ses_list.setIconSize(QSize(16, 16))
        hdr = self.ses_list.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, hdr.ResizeMode.Stretch)
        for c in (1, 2):
            hdr.setSectionResizeMode(c, hdr.ResizeMode.ResizeToContents)
        self.ses_list.currentItemChanged.connect(lambda _a, _b: self._on_ses_select())
        self.ses_list.itemDoubleClicked.connect(lambda _i: self._do_session_switch())
        v.addWidget(self.ses_list, 1)

        self.btn_ses_switch = QPushButton(icons["switch"], "设为当前")
        self.btn_ses_switch.setToolTip("将所选会话设为当前正在使用的会话；双击或按回车亦可")
        self.btn_ses_switch.clicked.connect(self._do_session_switch)
        self.btn_ses_switch.setEnabled(False)
        self.btn_ses_save = QPushButton(icons["save"], "保存")
        self.btn_ses_save.setToolTip("把当前正在使用的数据复制为一份新会话并存档")
        self.btn_ses_save.clicked.connect(self._do_save)
        self.btn_ses_new = QPushButton(icons["new"], "新建")
        self.btn_ses_new.setToolTip("新建一份空白会话并切换至它，需输入会话名称")
        self.btn_ses_new.clicked.connect(self._do_new)
        self.btn_ses_delete = QPushButton(icons["delete"], "删除")
        self.btn_ses_delete.setToolTip("永久删除所选会话，需输入会话名称二次确认，删除后不可恢复")
        self.btn_ses_delete.clicked.connect(self._guard_session_delete)
        self.btn_ses_delete.setEnabled(False)
        row = QHBoxLayout()
        row.addWidget(self.btn_ses_switch)
        row.addWidget(self.btn_ses_save)
        row.addWidget(self.btn_ses_new)
        row.addWidget(self.btn_ses_delete)
        v.addLayout(row)
        return self.ses_group

    # ------------------------------------------------------- 首次引导横幅

    def _banner_flag_path(self):
        return os.path.join(SHARE, BANNER_FLAG)

    def _maybe_show_banner(self):
        if os.path.isfile(self._banner_flag_path()):
            return
        self._show_banner(False)

    def _show_banner(self, force):
        if self.banner is None:
            self.banner = QFrame()
            self.banner.setFrameShape(QFrame.Shape.StyledPanel)
            self.banner.setStyleSheet("QFrame { background: #fff8dc; }")
            bv = QHBoxLayout(self.banner)
            bv.setContentsMargins(8, 6, 8, 6)
            btext = QLabel("一个「会话」就是一份完整、可独立使用的数据，里面带有多个账号。\n\n"
                           "左侧选择会话，右侧即显示该会话里的全部账号。\n"
                           "带对勾标记的是正在使用的会话，只有它里面的账号才允许操作。")
            btext.setWordWrap(True)
            self.banner_check = QCheckBox("以后不再显示")
            self.banner_ok = QPushButton("知道了")

            def _close():
                if self.banner_check.isChecked():
                    try:
                        with open(self._banner_flag_path(), "w", encoding="utf-8") as f:
                            json.dump({"shown": True}, f)
                    except OSError:
                        pass
                self.banner.hide()

            self.banner_ok.clicked.connect(_close)
            bv.addWidget(btext, 1)
            bv.addWidget(self.banner_check)
            bv.addWidget(self.banner_ok)
            self.root_lay.insertWidget(0, self.banner)
        self.banner.setVisible(True)

    # ------------------------------------------------------- 刷新

    def refresh_all(self):
        self._refresh_sessions()
        self._refresh_accounts()
        self._update_status()

    def _refresh_sessions(self):
        self.sessions = list_sessions()
        self._cur = current_session()
        self.ses_list.clear()
        folder_icon = QFileIconProvider().icon(QFileIconProvider.IconType.Folder)
        chk_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DialogYesButton)
        active_name = self._cur
        if not active_name and os.path.isdir(os.path.join(SHARE, ACTIVE_NAME)):
            item = QTreeWidgetItem(["当前数据目录，尚未纳入会话",
                                    mb_str(dir_size(os.path.join(SHARE, ACTIVE_NAME))),
                                    "当前"])
            item.setData(0, Qt.ItemDataRole.UserRole, (REAL_ROW, os.path.join(SHARE, ACTIVE_NAME)))
            item.setIcon(0, chk_icon)
            f = item.font(0); f.setBold(True); item.setFont(0, f)
            self.ses_list.addTopLevelItem(item)
            active_name = REAL_ROW
        for name, path in self.sessions:
            is_cur = (name == self._cur)
            item = QTreeWidgetItem([name, mb_str(dir_size(path)), "当前" if is_cur else ""])
            item.setData(0, Qt.ItemDataRole.UserRole, (name, path))
            item.setIcon(0, chk_icon if is_cur else folder_icon)
            if is_cur:
                f = item.font(0); f.setBold(True); item.setFont(0, f)
            self.ses_list.addTopLevelItem(item)
        for i in range(self.ses_list.topLevelItemCount()):
            if self.ses_list.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)[0] == active_name:
                self.ses_list.setCurrentItem(self.ses_list.topLevelItem(i))
                break

    def _selected_session(self):
        item = self.ses_list.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _is_active_session(self, name):
        if name == REAL_ROW:
            return self._cur is None and os.path.isdir(os.path.join(SHARE, ACTIVE_NAME))
        return name is not None and name == self._cur

    def _refresh_accounts(self):
        sel = self._selected_session()
        sess_name = sel[0] if sel else None
        if sess_name in (REAL_ROW, None):
            self.accounts = ta.list_session_accounts(None)
        else:
            self.accounts = ta.list_session_accounts(sess_name)
        cur = ta.read_current_owner()
        self._acct_cur = cur["owner_dir"] if cur else None
        self.acct_list.clear()
        folder_icon = QFileIconProvider().icon(QFileIconProvider.IconType.Folder)
        drive_icon = QFileIconProvider().icon(QFileIconProvider.IconType.Drive)
        chk_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DialogYesButton)
        for a in self.accounts:
            nm = a["alias"] or a["user_id"]
            status = "当前" if a["current"] else "接入" if a["linked"] else ""
            item = QTreeWidgetItem([nm, a["owner_dir"], mb_str(a["size"]), status])
            item.setData(0, Qt.ItemDataRole.UserRole, a["owner_dir"])
            if a["current"]:
                item.setIcon(0, chk_icon)
                f = item.font(0); f.setBold(True); item.setFont(0, f)
            else:
                item.setIcon(0, drive_icon if a["linked"] else folder_icon)
            self.acct_list.addTopLevelItem(item)
        sess_label = sess_name if sess_name != REAL_ROW and sess_name else "当前数据目录"
        self.acct_group.setTitle("「{}」的账号".format(sess_label))
        if not self.accounts:
            if self._is_active_session(sess_name):
                self.acct_hint.setText("这个会话里还没有账号。可点击「新建账号」新建并登录，或点击「接入 / 转换旧数据…」把旧数据接入。")
            else:
                self.acct_hint.setText("该会话不是当前正在使用的会话，无法对其账号进行操作。请点击左侧「设为当前」。")
        else:
            if self._is_active_session(sess_name):
                self.acct_hint.setText("列表里带对勾的就是当前账号。选中后点「设为当前」或双击，即可切换。")
            else:
                self.acct_hint.setText("当前可查看该会话的账号。要操作它们，请先在左侧点「设为当前」，使其成为当前会话。")
        self._btn_enable_accounts(self.acct_list.currentItem() is not None)

    # ------------------------------------------------------- 状态栏

    def _update_status(self):
        sel = self._selected_session()
        sess_name = sel[0] if sel else None
        active = self._is_active_session(sess_name)
        if active:
            if sess_name == REAL_ROW:
                self.st_msg.setText("当前为普通数据目录，尚未纳入会话机制。右侧即其中账号，可直接操作；建议先「保存当前会话」。")
            elif self._acct_cur:
                cur = next((a for a in self.accounts if a["owner_dir"] == self._acct_cur), None)
                nm = (cur["alias"] or cur["user_id"]) if cur else self._acct_cur
                self.st_msg.setText("当前会话：「{}」；当前账号：「{}」。切换账号后需重新启动 TeleAgent 生效。".format(sess_name, nm))
            else:
                self.st_msg.setText("当前会话：「{}」；尚无当前账号。".format(sess_name))
        else:
            self.st_msg.setText("选中的会话「{}」不是当前会话。先点击左侧「设为当前」，才能操作其中的账号。".format(sess_name or "无"))
        self.st_count.setText("会话 {} 个 · 账号 {} 个".format(len(self.sessions), len(self.accounts)))
        self._btn_enable_sessions(self.ses_list.currentItem() is not None)
        self._btn_enable_accounts(self.acct_list.currentItem() is not None)
        running = teleagent_running()
        if running:
            self.st_run.setText("TeleAgent：运行中")
            self.st_run.setStyleSheet("color: #e74c3c;")
        else:
            self.st_run.setText("TeleAgent：未运行")
            self.st_run.setStyleSheet("color: #27ae60;")

    def _btn_enable_accounts(self, has):
        sel = self._selected_session()
        sess_active = bool(sel and self._is_active_session(sel[0]))
        acct = self._selected_account()
        is_cur = acct is not None and acct == self._acct_cur
        busy = self._busy()
        acc_info = next((a for a in self.accounts if a["owner_dir"] == acct), None) if acct else None
        linked = bool(acc_info and acc_info["linked"])
        self.btn_acct_switch.setEnabled(sess_active and has and acct is not None and not is_cur and not busy)
        self.btn_alias.setEnabled(sess_active and has and acct is not None and not busy)
        self.btn_acct_new.setEnabled(sess_active and not busy)
        self.btn_acct_detach.setEnabled(sess_active and has and acct is not None and linked and not busy)
        self.btn_acct_delete.setEnabled(sess_active and has and acct is not None and not busy)
        self.btn_acct_refresh.setEnabled(not busy)
        self.btn_import.setEnabled(sess_active and not busy)

    def _btn_enable_sessions(self, has):
        sel = self._selected_session()
        busy = self._busy()
        is_cur = bool(sel and ((sel[0] == REAL_ROW and not self._cur)
                               or (self._cur is not None and sel[0] == self._cur)))
        self.btn_ses_switch.setEnabled(has and sel is not None and not is_cur and not busy)
        self.btn_ses_save.setEnabled(not busy)
        self.btn_ses_new.setEnabled(not busy)
        self.btn_ses_delete.setEnabled(has and sel is not None and not busy)
        self.act_refresh.setEnabled(not busy)

    def _busy(self):
        return self.worker is not None and self.worker.isRunning()

    def _selected_account(self):
        item = self.acct_list.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _selected_session(self):
        item = self.ses_list.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _require_active_session(self):
        """账号操作仅对“当前会话”生效；返回 True 表示当前选中的就是当前会话。"""
        sel = self._selected_session()
        if sel and self._is_active_session(sel[0]):
            return True
        self._info("请先在左侧选中带对勾的当前会话，才能操作其中的账号。")
        return False

    def _on_acct_select(self):
        self._update_status()

    def _on_ses_select(self):
        self._refresh_accounts()
        self._update_status()

    # ------------------------------------------------------- 账号操作（右侧）

    def _do_account_switch(self):
        acct = self._selected_account()
        if not acct:
            return
        if not self._require_active_session():
            return
        if acct == self._acct_cur:
            self._info("「{}」已经是当前账号，无需切换。".format(acct))
            return
        if not self._ensure_teleagent_closed():
            return
        info = next((a for a in self.accounts if a["owner_dir"] == acct), None)
        name = (info["alias"] or info["user_id"]) if info else acct
        if not self._confirm_yesno(
                "确认切换账号？",
                "是否将当前账号切换为「{}」？\n\n"
                "本操作仅更新一个很小的配置文件，不移动、不删除任何数据；\n"
                "切换后需重新启动 TeleAgent 方可生效。".format(name)):
            return
        try:
            ta.switch_owner(acct)
        except Exception as e:
            self._error(str(e))
            return
        self.refresh_all()
        self.st_msg.setText("已将当前账号切换为「{}」。请重新启动 TeleAgent 后生效。".format(name))

    def _do_alias(self):
        acct = self._selected_account()
        if not acct:
            return
        if not self._require_active_session():
            return
        cur = ta.load_aliases().get(acct, "")
        alias, ok = self._input_dialog("设置账号名称",
                                       "请为账号 {} 设置一个便于识别的名称；不输入内容将清除现有名称：".format(acct), cur)
        if not ok:
            return
        alias = alias.strip()
        ta.set_alias(acct, alias)
        self.refresh_all()
        if alias:
            self.st_msg.setText("已为账号 {} 设置名称：{}".format(acct, alias))
        else:
            self.st_msg.setText("已清除账号 {} 的名称。".format(acct))

    def _do_new_account(self):
        if not self._require_active_session():
            return
        if not self._ensure_teleagent_closed():
            return
        alias, ok = self._input_dialog(
            "新建账号",
            "请为新账号设置一个便于识别的名称，可留空；后续可用「改名」修改。\n\n"
            "新建后请重新启动 TeleAgent 并登录，该账号的数据将保存在新账号名下。", "")
        if not ok:
            return
        try:
            uid, name = ta.new_owner(alias.strip())
        except Exception as e:
            self._error(str(e))
            return
        self.refresh_all()
        if name:
            self.st_msg.setText("已新建账号「{}」。请重新启动 TeleAgent 并登录，数据将保存在该账号名下。".format(name))
        else:
            self.st_msg.setText("已新建账号。请重新启动 TeleAgent 并登录，数据将保存在该账号名下。")

    def _do_detach_account(self):
        acct = self._selected_account()
        if not acct:
            return
        if not self._require_active_session():
            return
        info = next((a for a in self.accounts if a["owner_dir"] == acct), None)
        if not info or not info["linked"]:
            self._info("「{}」不是接入账号，无需转换。".format(acct))
            return
        nm = info["alias"] or info["user_id"]
        if not self._confirm_yesno(
                "转为正式副本",
                "将把接入账号「{}」的数据完整复制到当前数据目录，\n"
                "并解除与原会话的关联。\n\n"
                "复制完成后，原会话即可安全删除。是否继续？".format(nm)):
            return
        try:
            ta.detach_account(acct)
        except Exception as e:
            self._error(str(e))
            return
        self.refresh_all()
        self.st_msg.setText("已将账号「{}」转为正式副本，数据已完整复制进来。原会话现在可以安全删除。".format(nm))

    def _do_account_delete(self):
        acct = self._selected_account()
        if not acct:
            return
        if not self._require_active_session():
            return
        info = next((a for a in self.accounts if a["owner_dir"] == acct), None)
        if not info:
            return
        nm = info["alias"] or info["user_id"]
        if info["linked"]:
            if not self._confirm_yesno(
                    "解除账号关联",
                    "账号「{}」是从其他会话接入的，删除仅解除关联，\n"
                    "其数据仍完整保留在原会话中，之后可随时重新接入。\n\n是否确认解除关联？".format(nm)):
                return
            try:
                ta.delete_account(acct)
            except Exception as e:
                self._error(str(e))
                return
            self.refresh_all()
            self.st_msg.setText("已解除账号「{}」的关联，数据保留在原会话中。".format(nm))
            return
        if not self._confirm_typed(
                "确认删除账号",
                "账号「{}」是当前会话里的正式数据，删除将永久移除其全部数据及登录状态，不可恢复。\n\n"
                "如确认删除，请在下方输入该账号：{}".format(nm, info["user_id"]),
                info["user_id"]):
            return
        try:
            ta.delete_account(acct)
        except Exception as e:
            self._error(str(e))
            return
        self.refresh_all()
        self.st_msg.setText("已删除账号「{}」。".format(nm))

    # ------------------------------------------------------- 会话操作（左侧）

    def _auto_snapshot_name(self):
        base = "default"
        if not os.path.exists(os.path.join(SHARE, PREFIX + base)):
            return base
        i = 1
        while os.path.exists(os.path.join(SHARE, PREFIX + "{}-{}".format(base, i))):
            i += 1
        return "{}-{}".format(base, i)

    def _do_session_switch(self):
        sel = self._selected_session()
        if not sel:
            return
        name, _path = sel
        if name == REAL_ROW:
            self._info("当前数据目录就是当前会话，无需切换。")
            return
        if name == self._cur:
            self._info("「{}」已经是当前会话。".format(name))
            return
        if not self._ensure_teleagent_closed():
            return
        link = os.path.join(SHARE, ACTIVE_NAME)
        if is_junction(link):
            self._start_switch(name)
            return
        if self._switch_from_real_dir(name):
            self._begin_switch_from_real_dir(name)

    def _switch_from_real_dir(self, target):
        link = os.path.join(SHARE, ACTIVE_NAME)
        if not os.path.exists(link):
            return self._confirm_yesno(
                "确认切换会话？",
                "当前不存在可用的 {} 数据目录。\n\n"
                "将直接建立对所选存档「{}」的指向，无需先保存。是否继续？".format(ACTIVE_NAME, target))
        name = self._auto_snapshot_name()
        return self._confirm_yesno(
            "是否先将当前目录存档？",
            "{} 目前是普通数据目录，尚未纳入存档机制。\n\n"
            "切换前将先把当前整份数据复制为存档「{}」，原目录保留且数据不会丢失，\n"
            "然后使程序数据指向所选存档「{}」。\n\n是否继续？".format(ACTIVE_NAME, name, target))

    def _begin_switch_from_real_dir(self, target):
        save_as = self._auto_snapshot_name()
        self._pending = ("switch", target)
        self._progress_msg("正在将当前数据保存为存档「{}」…".format(save_as), save_as)

    def _do_save(self):
        name, ok = self._input_dialog("保存当前会话",
                                      "请为存档设置名称，仅限字母、数字、下划线、点、连字符，长度 1 至 40：", "")
        if not ok or not self._validate(name):
            return
        dst = os.path.join(SHARE, PREFIX + name)
        if os.path.exists(dst) or is_junction(dst):
            self._warn("存档「{}」已存在。".format(name))
            return
        if teleagent_running():
            self._warn("TeleAgent 正在运行，本次保存的可能属于运行中的中间状态，建议先退出应用再保存。")
        self._progress_msg("正在将当前数据保存为存档「{}」…".format(name), name)

    def _do_new(self):
        name, ok = self._input_dialog("新建会话",
                                      "请为新会话设置名称，仅限字母、数字、下划线、点、连字符，长度 1 至 40：", "")
        if not ok or not self._validate(name):
            return
        dst = os.path.join(SHARE, PREFIX + name)
        if os.path.exists(dst) or is_junction(dst):
            self._warn("存档「{}」已存在。".format(name))
            return
        if not self._ensure_teleagent_closed():
            return
        link = os.path.join(SHARE, ACTIVE_NAME)
        if is_junction(link):
            os.makedirs(dst, exist_ok=True)
            self._start_switch(name)
            return
        if self._switch_from_real_dir(name):
            self._pending = ("switch", name)
            os.makedirs(dst, exist_ok=True)
            save_as = self._auto_snapshot_name()
            self._progress_msg("正在将当前数据保存为存档「{}」…".format(save_as), save_as)

    def _on_delete_pressed(self):
        # Delete 键：光标在哪一侧就删除哪一项，会话或账号其一
        if self.acct_list.hasFocus():
            self._do_account_delete()
        elif self.ses_list.hasFocus():
            self._do_session_delete()

    def _guard_session_delete(self):
        self._do_session_delete()

    def _do_session_delete(self):
        sel = self._selected_session()
        if not sel:
            return
        name, path = sel
        if name == REAL_ROW:
            self._warn("“当前数据目录”不是会话，无法在此处删除。如要清理数据，请使用系统资源管理器。")
            return
        if name == self._cur:
            self._warn("「{}」为当前正在使用的会话，请先切换到其他会话后再删除。".format(name))
            return
        links = ta.snapshot_links(name)
        if links:
            self._warn(
                "无法删除「{}」：已有账号从该会话接入到当前数据目录，\n"
                "直接删除该会话将导致这些账号的数据丢失。\n\n"
                "涉及的关联：{}。\n\n"
                "请先在右侧把相应账号「转为正式副本」，将数据完整复制进来并解除关联，然后再删除本会话。".format(
                    name, "；".join(lab for _p, lab in links)))
            return
        if not self._ensure_teleagent_closed():
            return
        if not self._confirm_typed(
                "确认删除会话",
                "删除会话「{}」将永久删除整份存档，大小 {}，删除后不可恢复。\n\n"
                "如确认删除，请在下方输入该会话的名称：{}".format(name, mb_str(dir_size(path)), name),
                name):
            return
        try:
            shutil.rmtree(path)
            self.refresh_all()
            self.st_msg.setText("已删除会话「{}」。".format(name))
        except Exception as e:
            self._error(str(e))

    # ------------------------------------------------------- 后台操作

    def _progress_msg(self, msg, name):
        link = os.path.join(SHARE, ACTIVE_NAME)
        src = junction_target(link) or link
        dst = os.path.join(SHARE, PREFIX + name)
        self.st_msg.setText(msg)
        self._set_busy(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.worker = CopyWorker(src, dst, name)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_copy_done)
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _start_switch(self, name):
        self._set_busy(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.worker = SwitchWorker(name)
        self.worker.finished_ok.connect(self._on_switch_done)
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _set_busy(self, busy):
        self.act_refresh.setEnabled(not busy)
        self.btn_acct_switch.setEnabled(not busy)
        self.btn_alias.setEnabled(not busy)
        self.btn_acct_new.setEnabled(not busy)
        self.btn_acct_detach.setEnabled(not busy)
        self.btn_acct_delete.setEnabled(not busy)
        self.btn_acct_refresh.setEnabled(not busy)
        self.btn_import.setEnabled(not busy)
        self.btn_ses_switch.setEnabled(not busy)
        self.btn_ses_save.setEnabled(not busy)
        self.btn_ses_new.setEnabled(not busy)
        self.btn_ses_delete.setEnabled(not busy)

    def _on_progress(self, done, total):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)

    def _on_copy_done(self, name):
        if self._pending:
            target = self._pending[1]
            self._pending = None
            self._start_switch(target)
            return
        self._op_done("已将当前数据保存为会话「{}」。".format(name))

    def _on_switch_done(self, name):
        self._op_done("已切换至会话「{}」。请重新启动 TeleAgent 后生效。".format(name))

    def _on_fail(self, msg):
        self._pending = None
        self._set_busy(False)
        self.progress.setVisible(False)
        self._error(msg)

    def _op_done(self, msg):
        self._pending = None
        self._set_busy(False)
        self.progress.setVisible(False)
        self.refresh_all()
        self.st_msg.setText(msg)

    # ------------------------------------------------------- 接入 / 转换旧数据

    def _import_or_convert(self):
        if not self._require_active_session():
            return
        snapshots = []
        if os.path.isdir(SHARE):
            for e in sorted(os.listdir(SHARE)):
                full = os.path.join(SHARE, e)
                if e.startswith(PREFIX) and os.path.isdir(full) and not is_junction(full):
                    snapshots.append(e[len(PREFIX):])
        if not snapshots:
            self._info("没有找到可用的旧数据目录。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("接入 / 转换旧数据")
        dlg.resize(560, 400)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("请选择要接入的旧数据目录：\n"
                             "· 新版格式：直接关联，不复制不移动，原目录保留。\n"
                             "· 旧版格式：复制并转为账号，原目录保留。"))
        lst = QListWidget()
        for n in snapshots:
            kind = ta.snapshot_kind(n)
            tag = {"new": "新版格式：直接关联", "legacy": "旧版格式：复制并转为账号", "none": "无可用数据"}.get(kind, "无可用数据")
            item = QListWidgetItem("{}    {}    {}".format(
                n, mb_str(dir_size(os.path.join(SHARE, PREFIX + n))), tag))
            item.setData(Qt.ItemDataRole.UserRole, n)
            lst.addItem(item)
        lay.addWidget(lst, 1)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("开始")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        item = lst.currentItem()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        kind = ta.snapshot_kind(name)
        if kind == "none":
            self._info("这个目录里没有可用的账号数据。")
            return
        if not self._ensure_teleagent_closed():
            return
        if kind == "new":
            if not self._confirm_yesno(
                    "确认接入旧账号数据？",
                    "旧数据目录「{}」属于新版格式。\n\n"
                    "将把其中包含的账号直接关联到本程序，不复制、不移动、不删除任何数据，\n"
                    "原目录保持不变。是否继续？".format(name)):
                return
            try:
                imported = ta.import_session(name)
            except RuntimeError as e:
                self._warn(str(e))
                return
            self.refresh_all()
            self.st_msg.setText("已接入旧数据中的账号：{}。在右侧选中相应账号即可使用。".format(", ".join(imported)))
        else:
            self._convert_legacy_dlg(name)

    def _convert_legacy_dlg(self, name):
        default = "v1_public_" + name if name.isdigit() else ""
        owner_dir = ""
        if default:
            owner_dir = default
        else:
            while True:
                owner_dir, ok = self._input_dialog(
                    "转换旧数据",
                    "旧数据目录「{}」不是纯数字名称，无法自动生成账号内部编号。\n"
                    "请为该账号指定内部编号，格式为 v1_public_数字；账号对外名称仍沿用旧目录名。".format(name),
                    "")
                if not ok or not owner_dir:
                    return
                owner_dir = owner_dir.strip()
                if not ta.parse_owner_dir(owner_dir):
                    self._warn("编号格式错误，应为 v1_public_数字，例如 v1_public_13812345678。")
                    continue
                target = os.path.join(ta.active_dir(), USERS_DIR, owner_dir)
                if os.path.exists(target) or is_junction(target):
                    self._warn("该账号编号已存在，请改用其他编号。")
                    continue
                break
        show_name = name
        if not self._confirm_yesno(
                "确认转换为账号？",
                "旧数据目录「TeleAgent-{}」，约 {}，属于旧版格式。\n\n"
                "转换后将复制该目录中的数据并建立账号，账号名称沿用旧目录名称「{}」；\n"
                "原目录保持不变，转换完成后该账号自动设为当前账号。\n\n是否继续？".format(
                    name, mb_str(dir_size(os.path.join(SHARE, PREFIX + name))), show_name)):
            return
        try:
            ta.convert_session(name, owner_dir)
        except Exception as e:
            self._error(str(e))
            return
        if owner_dir != "v1_public_" + name:
            ta.set_alias(owner_dir, name)
        self.refresh_all()
        self.st_msg.setText("已将旧数据目录「TeleAgent-{}」转为账号「{}」，并设为当前账号。\n"
                            "请重新启动 TeleAgent；如登录状态未恢复，请使用该账号重新登录。".format(name, show_name))

    # ------------------------------------------------------- 通用对话框

    def _confirm_yesno(self, title, text):
        r = QMessageBox.question(self, title, text,
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                 QMessageBox.StandardButton.No)
        return r == QMessageBox.StandardButton.Yes

    def _ensure_teleagent_closed(self):
        """切换/删除前确保 TeleAgent 已退出；返回 True 表示可以继续。"""
        qapp = QApplication.instance()
        if not ta.teleagent_running():
            return True
        if not self._confirm_yesno(
                "TeleAgent 正在运行",
                "检测到 TeleAgent 正在运行。为保证数据一致，切换或删除前必须完全退出该应用。\n\n"
                "是否由本工具自动退出 TeleAgent？"):
            self._warn("请先手动关闭 TeleAgent，再重试本次操作。")
            return False
        ta.terminate_teleagent()
        for _ in range(40):
            if qapp:
                qapp.processEvents()
            if not ta.teleagent_running():
                return True
            import time as _t; _t.sleep(0.05)
        if self._confirm_yesno(
                "未能正常退出",
                "TeleAgent 未能在 2 秒内正常退出，可能正在写入数据。\n\n"
                "是否强制结束其进程？未保存的数据可能丢失。"):
            ta.terminate_teleagent(force=True)
            for _ in range(10):
                if qapp:
                    qapp.processEvents()
                import time as _t; _t.sleep(0.05)
                if not ta.teleagent_running():
                    return True
        self._warn("TeleAgent 仍在运行，已中止本次操作。请手动退出后重试。")
        return False

    def _confirm_typed(self, title, text, expected):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(420, 160)
        lay = QVBoxLayout(dlg)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        edit = QLineEdit()
        lay.addWidget(edit)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("确认删除")
        btns.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        edit.textChanged.connect(
            lambda t: btns.button(QDialogButtonBox.StandardButton.Ok).setEnabled(t == expected))
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted and edit.text() == expected:
            return True
        return False

    def _input_dialog(self, title, label, default):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(380, 120)
        lay = QVBoxLayout(dlg)
        lbl = QLabel(label)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        edit = QLineEdit()
        edit.setText(default)
        edit.selectAll()
        lay.addWidget(edit)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        edit.returnPressed.connect(dlg.accept)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return edit.text().strip(), True
        return "", False

    def _validate(self, name):
        if not name:
            self._warn("请输入会话名称。")
            return False
        if not NAME_RE.match(name):
            self._warn("会话名称不符合要求：仅限字母、数字、下划线、点、连字符，长度 1 至 40。")
            return False
        return True

    def _info(self, msg):
        QMessageBox.information(self, "提示", msg)

    def _warn(self, msg):
        QMessageBox.warning(self, "提示", msg)

    def _error(self, msg):
        QMessageBox.critical(self, "错误", msg)

    def _about(self):
        QMessageBox.about(self, "关于",
                          "TeleAgent 数据管理器\n\n"
                          "一个「会话」就是一份完整、可独立使用的数据，里面带有多个账号。\n"
                          "左侧选择会话，右侧显示该会话里的账号；带对勾标记的会话才允许操作其中账号。\n\n"
                          "常用操作：\n"
                          "· 切换账号：选中账号后点「设为当前」；\n"
                          "· 全新登录：点「新建账号」，重启应用后登录；\n"
                          "· 整套备份：点左侧「保存当前会话」；\n"
                          "· 迁移旧数据：点「接入 / 转换旧数据…」。\n\n"
                          "数据目录：{}".format(SHARE))


def main():
    app = QApplication(sys.argv)
    app.setStyle("windowsvista")
    if not os.path.isdir(SHARE):
        QMessageBox.critical(None, "错误", "目录不存在：{}".format(SHARE))
        return 1
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()