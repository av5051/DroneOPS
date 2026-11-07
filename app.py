#!/usr/bin/env python3
"""DroneOps MVP - connect to a drone computer, browse, download, preview, targets -> KML."""
import posixpath
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import paramiko
from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSizePolicy, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core import (Cancelled, Connection, download_file, is_dir, load_cfg, local_path_for,
                  save_cfg, walk_remote)
from targets import build_kml, preview_text

TEXT_EXT = {".txt", ".csv", ".json", ".yaml", ".yml", ".log", ".md", ".kml", ".gpx",
            ".xml", ".cfg", ".conf", ".ini", ".py", ".sh", ".launch", ".param"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp"}
KML_SOURCE_EXT = {".txt", ".csv", ".log", ".json"}
DATA_PREVIEW_BYTES = 512 * 1024   # targets / JSON logs are read further so summaries are meaningful
MAX_KML_FILE = 5_000_000
MAX_KML_FILES = 2000
MAX_IMG_BYTES = 25 * 1024 * 1024
CONN_ERRORS = (paramiko.SSHException, EOFError, ConnectionError, socket.timeout, socket.gaierror)
MAX_LINK_RETRIES = 5

STATUS_COLORS = {
    "QUEUED": "#777", "RUNNING": "#1565c0", "WAITING_FOR_CONNECTION": "#ef6c00",
    "COMPLETED": "#2e7d32", "FAILED": "#c62828", "CANCELLED": "#777",
}


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def kml_ok(sel):
    """KML can be built from any mix of folders and .txt/.csv/.log/.json files."""
    return bool(sel) and all(isd or posixpath.splitext(p)[1].lower() in KML_SOURCE_EXT for p, isd, _ in sel)


def open_path(p):
    """Open a local file with the right program (Google Earth for KML if installed)."""
    if p.lower().endswith((".kml", ".kmz")):
        ge = shutil.which("google-earth-pro") or shutil.which("google-earth")
        if ge:
            subprocess.Popen([ge, p])
            return
    QDesktopServices.openUrl(QUrl.fromLocalFile(p))


# ------------------------------------------------------------ task engine
class Task:
    def __init__(self, tid, name, fn):
        self.id, self.name, self.fn = tid, name, fn
        self.status, self.progress = "QUEUED", 0.0
        self.info, self.output = "", ""
        self.retries = 0
        self.cancel = threading.Event()
        self.prog = lambda v: None


class _Runner(QRunnable):
    def __init__(self, mgr, task):
        super().__init__()
        self.m, self.t = mgr, task

    def run(self):
        t, m = self.t, self.m
        if t.cancel.is_set():
            t.status = "CANCELLED"
            m.changed.emit(t.id)
            return
        t.status = "RUNNING"
        m.changed.emit(t.id)
        last = [-1]

        def prog(v):
            t.progress = v
            if int(v * 100) != last[0]:
                last[0] = int(v * 100)
                m.changed.emit(t.id)

        t.prog = prog
        attempt = 0
        try:
            while True:
                try:
                    out = t.fn(t)
                    break
                except Cancelled:
                    raise
                except CONN_ERRORS:
                    # drone out of range / Wi-Fi blip: wait and resume, don't fail at once
                    attempt += 1
                    if attempt > MAX_LINK_RETRIES:
                        raise
                    t.status = "WAITING_FOR_CONNECTION"
                    t.info = f"link lost, retry {attempt}/{MAX_LINK_RETRIES}"
                    m.changed.emit(t.id)
                    for _ in range(50):
                        if t.cancel.is_set():
                            raise Cancelled()
                        time.sleep(0.1)
                    t.status = "RUNNING"
                    m.changed.emit(t.id)
            if out:
                t.output = out
            t.progress = 1.0
            t.status = "COMPLETED"
        except Cancelled:
            t.status = "CANCELLED"
        except Exception as e:  # one failing task never affects the others
            t.status = "FAILED"
            t.info = f"{type(e).__name__}: {e}"
        m.changed.emit(t.id)


class TaskManager(QObject):
    changed = Signal(int)

    def __init__(self, workers=3):
        super().__init__()
        self.tasks, self._next, self._lock = {}, 1, threading.Lock()
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(workers)

    def submit(self, name, fn):
        with self._lock:
            tid = self._next
            self._next += 1
            t = Task(tid, name, fn)
            self.tasks[tid] = t
        self._enqueue(t)
        return t

    def _enqueue(self, t):
        t.status = "QUEUED"
        self.changed.emit(t.id)
        self.pool.start(_Runner(self, t))

    def retry(self, t):
        if t.status in ("FAILED", "CANCELLED"):
            t.cancel.clear()
            t.retries += 1
            t.info = f"retry #{t.retries}"
            self._enqueue(t)

    def cancel(self, t):
        if t.status in ("QUEUED", "RUNNING", "WAITING_FOR_CONNECTION"):
            t.cancel.set()


# ------------------------------------------------------------- widgets
class ImageLabel(QLabel):
    def __init__(self):
        super().__init__()
        self._pm = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumHeight(120)

    def set_image(self, pm):
        self._pm = pm
        self._fit()

    def clear_image(self):
        self._pm = None
        self.clear()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        if self._pm is not None and not self._pm.isNull():
            self.setPixmap(self._pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))


class ProfileDialog(QDialog):
    def __init__(self, parent, prof=None):
        super().__init__(parent)
        self.setWindowTitle("Drone profile")
        prof = prof or {}
        self.name = QLineEdit(prof.get("name", ""))
        self.host = QLineEdit(prof.get("host", ""))
        self.port = QLineEdit(str(prof.get("port", 22)))
        self.user = QLineEdit(prof.get("user", ""))
        self.key = QLineEdit(prof.get("key", ""))
        self.key.setPlaceholderText("optional, e.g. ~/.ssh/id_ed25519 (empty = ask for password)")
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Host / IP", self.host)
        form.addRow("Port", self.port)
        form.addRow("User", self.user)
        form.addRow("SSH key file", self.key)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(bb)
        self.setMinimumWidth(420)

    def _ok(self):
        if not (self.name.text().strip() and self.host.text().strip() and self.user.text().strip()):
            QMessageBox.warning(self, "Profile", "Name, host and user are required.")
            return
        if not self.port.text().strip().isdigit():
            QMessageBox.warning(self, "Profile", "Port must be a number.")
            return
        self.accept()

    def value(self):
        return {"name": self.name.text().strip(), "host": self.host.text().strip(),
                "port": int(self.port.text()), "user": self.user.text().strip(),
                "key": self.key.text().strip()}


# ------------------------------------------------------------ main window
class MainWindow(QMainWindow):
    invoke = Signal(object)  # run a callable on the GUI thread (used by worker threads)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DroneOps")
        self.resize(1400, 850)
        self.cfg = load_cfg()
        self.conn = None
        self.cwd = "/"
        self.entries = []
        self._req = 0       # navigation request counter (ignore stale results)
        self._pv = 0        # preview request counter
        self.task_items = {}
        self.tm = TaskManager()
        self.tm.changed.connect(self.on_task_changed)
        self.invoke.connect(self._run_callable)
        self._build_ui()
        self.refresh_profiles()
        self.refresh_presets()
        self.link_timer = QTimer(self)
        self.link_timer.timeout.connect(self.check_link)
        self.link_timer.start(3000)

    # ---- plumbing
    @Slot(object)
    def _run_callable(self, f):
        f()

    def bg(self, work, done, fail=None):
        def runner():
            try:
                r = work()
            except Exception as e:
                if fail:
                    self.invoke.emit(lambda e=e: fail(e))
                return
            self.invoke.emit(lambda r=r: done(r))
        threading.Thread(target=runner, daemon=True).start()

    def say(self, msg):
        self.statusBar().showMessage(msg, 8000)

    # ---- UI
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)

        # top bar
        top = QHBoxLayout()
        title = QLabel("<b>DroneOps</b>")
        title.setStyleSheet("font-size: 16px;")
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(160)
        self.profile_combo.currentTextChanged.connect(self._profile_changed)
        pbtn = QPushButton("Profiles")
        pm = QMenu(pbtn)
        pm.addAction("New profile...", self.new_profile)
        pm.addAction("Edit current...", self.edit_profile)
        pm.addAction("Delete current", self.delete_profile)
        pbtn.setMenu(pm)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(self.toggle_connection)
        self.link_label = QLabel("Offline")
        self.coord_combo = QComboBox()
        self.coord_combo.addItems(["lat,lon", "lon,lat"])
        self.coord_combo.setCurrentText(self.cfg.get("coord_order", "lat,lon"))
        self.coord_combo.currentTextChanged.connect(self._coord_changed)
        self.coord_combo.setToolTip("Order of the two numbers in targets files")
        self.dl_label = QLabel()
        btn_dl_dir = QPushButton("Download folder...")
        btn_dl_dir.clicked.connect(self.choose_download_dir)
        for w in (title, self.profile_combo, pbtn, self.btn_connect, self.link_label):
            top.addWidget(w)
        top.addStretch(1)
        top.addWidget(QLabel("Coords:"))
        top.addWidget(self.coord_combo)
        top.addWidget(self.dl_label)
        top.addWidget(btn_dl_dir)
        outer.addLayout(top)
        self._update_dl_label()

        # left: quick access
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("<b>Quick access</b>"))
        self.preset_list = QListWidget()
        self.preset_list.itemActivated.connect(self.open_preset)
        self.preset_list.itemClicked.connect(self.open_preset)
        self.preset_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.preset_list.customContextMenuRequested.connect(self.preset_menu)
        ll.addWidget(self.preset_list, 1)
        b1 = QPushButton("Bookmark this folder")
        b1.clicked.connect(lambda: self.add_preset(self.cwd, True))
        b2 = QPushButton("Bookmark selected")
        b2.clicked.connect(self.bookmark_selected)
        ll.addWidget(b1)
        ll.addWidget(b2)

        # centre: browser
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        nav = QHBoxLayout()
        btn_up = QPushButton("Up")
        btn_up.clicked.connect(self.go_up)
        btn_ref = QPushButton("Refresh")
        btn_ref.clicked.connect(lambda: self.navigate(self.cwd))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Remote path (Enter to go)")
        self.path_edit.returnPressed.connect(lambda: self.navigate(posixpath.normpath(self.path_edit.text().strip() or "/")))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter...")
        self.filter_edit.setMaximumWidth(180)
        self.filter_edit.textChanged.connect(self.apply_filter)
        nav.addWidget(btn_up)
        nav.addWidget(self.path_edit, 1)
        nav.addWidget(self.filter_edit)
        nav.addWidget(btn_ref)
        cl.addLayout(nav)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Size", "Modified"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.on_select)
        self.table.cellDoubleClicked.connect(self.on_double_click)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.table_menu)
        cl.addWidget(self.table, 1)

        # right: preview
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.pv_title = QLabel("<b>Nothing selected</b>")
        self.pv_title.setWordWrap(True)
        self.pv_meta = QLabel("")
        self.img = ImageLabel()
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setStyleSheet("font-family: monospace;")
        rl.addWidget(self.pv_title)
        rl.addWidget(self.pv_meta)
        rl.addWidget(self.img, 1)
        rl.addWidget(self.text, 1)
        self.img.hide()
        btns = QHBoxLayout()
        self.btn_dl = QPushButton("Download")
        self.btn_dl.clicked.connect(self.download_selected)
        self.btn_kml = QPushButton("Make KML")
        self.btn_kml.clicked.connect(self.kml_selected)
        btns.addWidget(self.btn_dl)
        btns.addWidget(self.btn_kml)
        rl.addLayout(btns)
        self.auto_open = QCheckBox("Open KML when ready")
        self.auto_open.setChecked(True)
        rl.addWidget(self.auto_open)
        self.btn_dl.setEnabled(False)
        self.btn_kml.setEnabled(False)

        hs = QSplitter(Qt.Orientation.Horizontal)
        hs.addWidget(left)
        hs.addWidget(centre)
        hs.addWidget(right)
        hs.setSizes([220, 700, 420])

        # bottom: task monitor
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Tasks</b>"))
        head.addStretch(1)
        bclear = QPushButton("Clear finished")
        bclear.clicked.connect(self.clear_finished)
        head.addWidget(bclear)
        bl.addLayout(head)
        self.task_table = QTableWidget(0, 5)
        self.task_table.setHorizontalHeaderLabels(["#", "Task", "Status", "Progress", "Info"])
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.task_table.verticalHeader().hide()
        th = self.task_table.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        th.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.task_table.setColumnWidth(3, 160)
        th.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.task_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_table.customContextMenuRequested.connect(self.task_menu)
        self.task_table.cellDoubleClicked.connect(lambda r, c: self._open_task_output(r))
        bl.addWidget(self.task_table)

        vs = QSplitter(Qt.Orientation.Vertical)
        vs.addWidget(hs)
        vs.addWidget(bottom)
        vs.setSizes([620, 200])
        outer.addWidget(vs, 1)

    # ---- profiles
    def refresh_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for p in self.cfg["profiles"]:
            self.profile_combo.addItem(p["name"])
        last = self.cfg.get("last_profile")
        if last:
            self.profile_combo.setCurrentText(last)
        self.profile_combo.blockSignals(False)

    def cur_profile(self):
        name = self.profile_combo.currentText()
        return next((p for p in self.cfg["profiles"] if p["name"] == name), None)

    def _profile_changed(self, name):
        self.cfg["last_profile"] = name
        save_cfg(self.cfg)
        self.refresh_presets()

    def new_profile(self):
        dlg = ProfileDialog(self)
        if dlg.exec():
            p = dlg.value()
            if any(x["name"] == p["name"] for x in self.cfg["profiles"]):
                QMessageBox.warning(self, "Profile", "A profile with that name already exists.")
                return
            self.cfg["profiles"].append(p)
            self.cfg["last_profile"] = p["name"]
            save_cfg(self.cfg)
            self.refresh_profiles()
            self.refresh_presets()

    def edit_profile(self):
        p = self.cur_profile()
        if not p:
            return
        dlg = ProfileDialog(self, p)
        if dlg.exec():
            new = dlg.value()
            old_name = p["name"]
            p.update(new)
            if new["name"] != old_name:
                for pr in self.cfg["presets"]:
                    if pr["profile"] == old_name:
                        pr["profile"] = new["name"]
                self.cfg["last_profile"] = new["name"]
            save_cfg(self.cfg)
            self.refresh_profiles()
            self.refresh_presets()

    def delete_profile(self):
        p = self.cur_profile()
        if p and QMessageBox.question(self, "Delete", f"Delete profile '{p['name']}' and its bookmarks?") \
                == QMessageBox.StandardButton.Yes:
            self.cfg["profiles"].remove(p)
            self.cfg["presets"] = [x for x in self.cfg["presets"] if x["profile"] != p["name"]]
            save_cfg(self.cfg)
            self.refresh_profiles()
            self.refresh_presets()

    # ---- settings
    def _coord_changed(self, v):
        self.cfg["coord_order"] = v
        save_cfg(self.cfg)

    def _update_dl_label(self):
        d = self.cfg["download_dir"]
        self.dl_label.setText(d if len(d) < 38 else "..." + d[-35:])
        self.dl_label.setToolTip(d)

    def choose_download_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Download folder", self.cfg["download_dir"])
        if d:
            self.cfg["download_dir"] = d
            save_cfg(self.cfg)
            self._update_dl_label()

    # ---- connection
    def toggle_connection(self):
        if self.conn:
            self.conn.close()
            self.conn = None
            self.btn_connect.setText("Connect")
            self.table.setRowCount(0)
            self.entries = []
            self.say("Disconnected")
            self.check_link()
            return
        prof = self.cur_profile()
        if not prof:
            QMessageBox.information(self, "DroneOps", "Create a drone profile first (Profiles > New profile).")
            return
        pw = None
        if not prof.get("key"):
            pw, ok = QInputDialog.getText(self, "Password", f"Password for {prof['user']}@{prof['host']}",
                                          QLineEdit.EchoMode.Password)
            if not ok:
                return
        conn = Connection(prof, pw)
        self.btn_connect.setEnabled(False)
        self.link_label.setText("Connecting...")

        def work():
            conn.connect()
            return conn.sftp.normalize(".")

        def done(home):
            self.btn_connect.setEnabled(True)
            self.conn = conn
            self.btn_connect.setText("Disconnect")
            self.say(f"Connected to {prof['name']}")
            self.check_link()
            self.navigate(home)

        def fail(e):
            self.btn_connect.setEnabled(True)
            self.link_label.setText("Offline")
            QMessageBox.warning(self, "Connection failed", f"{type(e).__name__}: {e}")

        self.bg(work, done, fail)

    def check_link(self):
        if not self.conn:
            self.link_label.setText("<span style='color:#777'>&#9679; Offline</span>")
        elif self.conn.alive():
            self.link_label.setText("<span style='color:#2e7d32'>&#9679; Connected</span>")
        else:
            self.link_label.setText("<span style='color:#ef6c00'>&#9679; Link lost (reconnects on next action)</span>")

    # ---- browsing
    def navigate(self, path, select=None):
        if not self.conn:
            self.say("Not connected")
            return
        self._req += 1
        req, conn = self._req, self.conn
        self.say(f"Loading {path} ...")

        def done(entries):
            if req != self._req:
                return
            entries = sorted(entries, key=lambda a: (not is_dir(a), a.filename.lower()))
            self.cwd, self.entries = path, entries
            self.path_edit.setText(path)
            self.table.setRowCount(len(entries))
            for r, a in enumerate(entries):
                d = is_dir(a)
                vals = [a.filename + ("/" if d else ""), "" if d else human_size(a.st_size),
                        datetime.fromtimestamp(a.st_mtime).strftime("%Y-%m-%d %H:%M") if a.st_mtime else ""]
                for c, v in enumerate(vals):
                    self.table.setItem(r, c, QTableWidgetItem(v))
            self.apply_filter()
            self.say(f"{len(entries)} items in {path}")
            if select:
                for r, a in enumerate(entries):
                    if a.filename == select:
                        self.table.selectRow(r)
                        break
            else:
                self.on_select()

        def fail(e):
            if req == self._req:
                self.say(f"Cannot open {path}: {e}")
                QMessageBox.warning(self, "Cannot open folder", f"{path}\n\n{type(e).__name__}: {e}")

        self.bg(lambda: conn.listdir(path), done, fail)

    def go_up(self):
        self.navigate(posixpath.dirname(self.cwd.rstrip("/")) or "/")

    def apply_filter(self):
        f = self.filter_edit.text().lower()
        for r, a in enumerate(self.entries):
            self.table.setRowHidden(r, bool(f) and f not in a.filename.lower())

    def selected_entries(self):
        rows = sorted({i.row() for i in self.table.selectedItems()})
        out = []
        for r in rows:
            if r < len(self.entries):
                a = self.entries[r]
                out.append((posixpath.join(self.cwd, a.filename), is_dir(a), a))
        return out

    def on_double_click(self, row, col):
        if row < len(self.entries):
            a = self.entries[row]
            if is_dir(a):
                self.navigate(posixpath.join(self.cwd, a.filename))
            else:
                self.queue_file(posixpath.join(self.cwd, a.filename))

    def table_menu(self, pos):
        sel = self.selected_entries()
        if not sel:
            return
        m = QMenu(self)
        m.addAction("Download", self.download_selected)
        if kml_ok(sel):
            m.addAction("Make KML", self.kml_selected)
        m.addAction("Bookmark", self.bookmark_selected)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ---- preview
    def _show_text(self, s):
        self.img.hide()
        self.text.show()
        self.text.setPlainText(s)

    def on_select(self):
        sel = self.selected_entries()
        self._pv += 1
        tok = self._pv
        self.img.clear_image()
        self.btn_dl.setEnabled(bool(sel))
        self.btn_kml.setEnabled(False)
        self.text.clear()
        self.pv_meta.setText("")
        if not sel:
            self.pv_title.setText("<b>Nothing selected</b>")
            self._show_text("")
            return
        if len(sel) > 1:
            self.pv_title.setText(f"<b>{len(sel)} items selected</b>")
            self._show_text("")
            self.btn_kml.setEnabled(kml_ok(sel))
            return
        path, isd, attr = sel[0]
        self.pv_title.setText(f"<b>{posixpath.basename(path)}</b>")
        if isd:
            self.pv_meta.setText("Folder - Download fetches everything inside; Make KML plots its data files")
            self.btn_kml.setEnabled(True)
            self._show_text("")
            return
        self.pv_meta.setText(f"{human_size(attr.st_size)}   {path}")
        ext = posixpath.splitext(path)[1].lower()
        self.btn_kml.setEnabled(ext in KML_SOURCE_EXT)
        conn = self.conn

        if ext in IMG_EXT:
            if attr.st_size > MAX_IMG_BYTES:
                self._show_text("Image too large to preview. Use Download.")
                return
            self._show_text("Loading image ...")

            def work():
                s = conn.new_sftp()
                try:
                    with s.open(path, "rb") as f:
                        return f.read()
                finally:
                    s.close()

            def done(data):
                if tok != self._pv:
                    return
                pm = QPixmap()
                if pm.loadFromData(data):
                    self.text.hide()
                    self.img.show()
                    self.img.set_image(pm)
                else:
                    self._show_text("Could not decode image.")

            self.bg(work, done, lambda e: tok == self._pv and self._show_text(f"Preview failed: {e}"))
        elif ext in TEXT_EXT:
            self._show_text("Loading ...")

            def done(data):
                if tok != self._pv:
                    return
                if b"\x00" in data:
                    self._show_text("Binary file - no preview.")
                    return
                s = data.decode("utf-8", errors="replace")
                truncated = attr.st_size > len(data)
                pretty = preview_text(posixpath.basename(path), s, truncated)
                if pretty:
                    s = pretty
                if truncated:
                    s += f"\n\n... (showing first {human_size(len(data))} of {human_size(attr.st_size)})"
                self._show_text(s)

            nbytes = DATA_PREVIEW_BYTES if ext in (".txt", ".json") else 65536
            self.bg(lambda: conn.read_head(path, nbytes), done,
                    lambda e: tok == self._pv and self._show_text(f"Preview failed: {e}"))
        else:
            self._show_text("No preview for this file type. Use Download.")

    # ---- transfers / actions
    def download_selected(self):
        for path, isd, _ in self.selected_entries():
            self.queue_dir(path) if isd else self.queue_file(path)

    def queue_file(self, remote):
        conn = self.conn
        if not conn:
            return
        local = local_path_for(self.cfg["download_dir"], conn.profile["name"], remote)

        def fn(task):
            return download_file(conn, remote, local, task.prog, task.cancel)

        self.tm.submit(f"Download {posixpath.basename(remote)}", fn)

    def queue_dir(self, remote):
        conn = self.conn
        if not conn:
            return
        root = self.cfg["download_dir"]
        prof = conn.profile["name"]

        def fn(task):
            n = 0
            for p, _size in walk_remote(conn, remote):
                if task.cancel.is_set():
                    raise Cancelled()
                self.queue_file_from(conn, p)
                n += 1
            task.info = f"{n} files queued"
            task.prog(1.0)
            return str(local_path_for(root, prof, remote))

        self.tm.submit(f"Scan folder {posixpath.basename(remote.rstrip('/')) or '/'}", fn)

    def queue_file_from(self, conn, remote):
        local = local_path_for(self.cfg["download_dir"], conn.profile["name"], remote)

        def fn(task):
            return download_file(conn, remote, local, task.prog, task.cancel)

        self.tm.submit(f"Download {posixpath.basename(remote)}", fn)

    def kml_selected(self):
        sel = self.selected_entries()
        if not self.conn or not kml_ok(sel):
            return
        conn = self.conn
        order = self.cfg.get("coord_order", "lat,lon")
        out_dir = Path(self.cfg["download_dir"]) / conn.profile["name"] / "kml"
        auto = self.auto_open.isChecked()
        items = [(p, isd) for p, isd, _ in sel]
        label = posixpath.basename(items[0][0].rstrip("/")) or "/"
        if len(items) > 1:
            label += f" +{len(items) - 1}"

        def fn(task):
            paths = []
            for p, isd in items:  # folders are scanned recursively for data files
                if isd:
                    for fp, size in walk_remote(conn, p):
                        if posixpath.splitext(fp)[1].lower() in KML_SOURCE_EXT and size <= MAX_KML_FILE:
                            paths.append(fp)
                else:
                    paths.append(p)
            if not paths:
                raise ValueError("no .txt / .csv / .log / .json files found")
            paths = paths[:MAX_KML_FILES]
            files = []
            s = conn.new_sftp()
            try:
                for i, p in enumerate(paths):
                    if task.cancel.is_set():
                        raise Cancelled()
                    with s.open(p, "rb") as f:
                        files.append((posixpath.basename(p), f.read(MAX_KML_FILE).decode("utf-8", errors="replace")))
                    task.prog(0.8 * (i + 1) / len(paths))
            finally:
                s.close()
            kml, plotted, summary, notes = build_kml(files, order, doc_name=label)
            if not plotted:
                raise ValueError("nothing to plot. " + "; ".join(notes[:2]))
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = posixpath.splitext(posixpath.basename(items[0][0].rstrip("/")))[0] or "root"
            out = out_dir / f"{stem}{'_etc' if len(items) > 1 or len(paths) > 1 else ''}_{stamp}.kml"
            out.write_text(kml, encoding="utf-8")
            task.info = summary + (f" | {len(notes)} notes: {notes[0]}" if notes else "")
            task.prog(1.0)
            if auto:
                self.invoke.emit(lambda: open_path(str(out)))
            return str(out)

        self.tm.submit(f"KML from {label}", fn)

    # ---- bookmarks
    def refresh_presets(self):
        self.preset_list.clear()
        name = self.profile_combo.currentText()
        for i, p in enumerate(self.cfg["presets"]):
            if p["profile"] == name:
                it = QListWidgetItem(p["name"] + ("/" if p["is_dir"] else ""))
                it.setData(Qt.ItemDataRole.UserRole, i)
                it.setToolTip(p["path"])
                self.preset_list.addItem(it)

    def add_preset(self, path, is_dir_):
        name = self.profile_combo.currentText()
        if not name or not self.conn:
            self.say("Connect first")
            return
        label, ok = QInputDialog.getText(self, "Bookmark", "Name:", text=posixpath.basename(path.rstrip("/")) or "/")
        if ok and label.strip():
            self.cfg["presets"].append({"name": label.strip(), "profile": name, "path": path, "is_dir": is_dir_})
            save_cfg(self.cfg)
            self.refresh_presets()

    def bookmark_selected(self):
        sel = self.selected_entries()
        if sel:
            self.add_preset(sel[0][0], sel[0][1])
        else:
            self.say("Select a file or folder first")

    def open_preset(self, item):
        p = self.cfg["presets"][item.data(Qt.ItemDataRole.UserRole)]
        if not self.conn:
            self.say("Connect first")
            return
        if p["is_dir"]:
            self.navigate(p["path"])
        else:
            self.navigate(posixpath.dirname(p["path"]) or "/", select=posixpath.basename(p["path"]))

    def preset_menu(self, pos):
        item = self.preset_list.itemAt(pos)
        if not item:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        m = QMenu(self)

        def rename():
            n, ok = QInputDialog.getText(self, "Rename", "Name:", text=self.cfg["presets"][idx]["name"])
            if ok and n.strip():
                self.cfg["presets"][idx]["name"] = n.strip()
                save_cfg(self.cfg)
                self.refresh_presets()

        def remove():
            del self.cfg["presets"][idx]
            save_cfg(self.cfg)
            self.refresh_presets()

        def download():
            p = self.cfg["presets"][idx]
            if self.conn:
                self.queue_dir(p["path"]) if p["is_dir"] else self.queue_file(p["path"])

        m.addAction("Download", download)
        m.addAction("Rename...", rename)
        m.addAction("Remove", remove)
        m.exec(self.preset_list.viewport().mapToGlobal(pos))

    # ---- task monitor
    @Slot(int)
    def on_task_changed(self, tid):
        t = self.tm.tasks[tid]
        item = self.task_items.get(tid)
        if item is None:
            self.task_table.insertRow(0)
            item = QTableWidgetItem(str(tid))
            item.setData(Qt.ItemDataRole.UserRole, tid)
            self.task_items[tid] = item
            self.task_table.setItem(0, 0, item)
            self.task_table.setItem(0, 1, QTableWidgetItem(t.name))
            self.task_table.setItem(0, 2, QTableWidgetItem(""))
            bar = QProgressBar()
            bar.setRange(0, 100)
            self.task_table.setCellWidget(0, 3, bar)
            self.task_table.setItem(0, 4, QTableWidgetItem(""))
        r = self.task_table.row(item)
        st = self.task_table.item(r, 2)
        st.setText(t.status)
        st.setForeground(QColor(STATUS_COLORS.get(t.status, "#000")))
        self.task_table.cellWidget(r, 3).setValue(int(t.progress * 100))
        self.task_table.item(r, 4).setText(t.info)
        self.task_table.item(r, 4).setToolTip(t.info)

    def _tasks_for_rows(self):
        rows = sorted({i.row() for i in self.task_table.selectedItems()})
        out = []
        for r in rows:
            it = self.task_table.item(r, 0)
            if it:
                out.append(self.tm.tasks[it.data(Qt.ItemDataRole.UserRole)])
        return out

    def task_menu(self, pos):
        tasks = self._tasks_for_rows()
        if not tasks:
            return
        m = QMenu(self)
        m.addAction("Retry", lambda: [self.tm.retry(t) for t in tasks])
        m.addAction("Cancel", lambda: [self.tm.cancel(t) for t in tasks])
        if len(tasks) == 1 and tasks[0].output:
            m.addAction("Open output", lambda: open_path(tasks[0].output))
            m.addAction("Show folder", lambda: open_path(str(Path(tasks[0].output).parent)))
        m.exec(self.task_table.viewport().mapToGlobal(pos))

    def _open_task_output(self, row):
        it = self.task_table.item(row, 0)
        if it:
            t = self.tm.tasks[it.data(Qt.ItemDataRole.UserRole)]
            if t.output and Path(t.output).exists():
                open_path(t.output)

    def clear_finished(self):
        for tid, item in list(self.task_items.items()):
            if self.tm.tasks[tid].status in ("COMPLETED", "CANCELLED"):
                self.task_table.removeRow(self.task_table.row(item))
                del self.task_items[tid]

    def closeEvent(self, e):
        running = [t for t in self.tm.tasks.values() if t.status in ("RUNNING", "QUEUED", "WAITING_FOR_CONNECTION")]
        if running and QMessageBox.question(
                self, "Quit", f"{len(running)} task(s) still running. Quit anyway?\n"
                              "(Partial downloads resume when you download them again.)") \
                != QMessageBox.StandardButton.Yes:
            e.ignore()
            return
        for t in running:
            t.cancel.set()
        if self.conn:
            self.conn.close()
        e.accept()


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()