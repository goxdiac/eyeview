"""
EyeView - System Monitor
Version: 1.0.0
Requirements: pip install PyQt6 psutil pywin32
GitHub: https://github.com/yourusername/eyeview
License: MIT
"""

import sys
import os
import time
import json
import winreg
import psutil
from collections import deque
from datetime import timedelta, datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QScrollArea, QDialog,
    QColorDialog, QSpinBox, QFormLayout, QDialogButtonBox,
    QSizePolicy, QSystemTrayIcon, QMenu, QCheckBox, QTabWidget,
    QDoubleSpinBox, QGroupBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPoint, QSize, QTimer
from PyQt6.QtGui import (
    QPalette, QColor, QPainter, QPen, QBrush, QPolygonF,
    QIcon, QPixmap, QAction
)
from PyQt6.QtCore import QPointF

try:
    import win32gui
    import win32process
    WIN32_OK = True
except ImportError:
    WIN32_OK = False

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
VERSION = "1.0.0"
APP_NAME = "EyeView"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".eyeview_config.json")
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
HISTORY_LEN = 60  # seconds of graph history

DEFAULT_THEME = {
    "bg_main":   "#0b0b18",
    "bg_card":   "#0f0f22",
    "bg_border": "#1a1a35",
    "text_main": "#dddddd",
    "text_dim":  "#444455",
    "accent_dl": "#00ff88",
    "accent_ul": "#ff6b6b",
    "accent_ui": "#00d4ff",
    "accent_cpu": "#ffaa00",
    "accent_ram": "#aa66ff",
    "font_size": 11,
}

DEFAULT_CONFIG = {
    "theme": DEFAULT_THEME.copy(),
    "window_x": 100,
    "window_y": 100,
    "window_w": 480,
    "window_h": 760,
    "startup": False,
    "alerts_enabled": True,
    "cpu_alert_threshold": 90.0,
    "ram_alert_threshold": 90.0,
    "alert_cooldown": 60,  # seconds between repeat alerts
}

# ─────────────────────────────────────────────
# Config Manager
# ─────────────────────────────────────────────
class Config:
    def __init__(self):
        self.data = DEFAULT_CONFIG.copy()
        self.data["theme"] = DEFAULT_THEME.copy()
        self.load()

    def load(self):
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    # Deep merge theme
                    if "theme" in saved:
                        self.data["theme"].update(saved.pop("theme"))
                    self.data.update(saved)
        except Exception as e:
            print(f"Config load error: {e}")

    def save(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            print(f"Config save error: {e}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

# ─────────────────────────────────────────────
# Startup Manager
# ─────────────────────────────────────────────
def set_startup(enabled: bool):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            exe_path = os.path.abspath(sys.argv[0])
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"Startup error: {e}")
        return False

def get_startup() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            winreg.CloseKey(key)
            return False
    except:
        return False

# ─────────────────────────────────────────────
# Network Monitor Thread
# ─────────────────────────────────────────────
class NetMonitor(QThread):
    updated = pyqtSignal(float, float, float, float)
    # dl_speed, ul_speed, session_dl_total, session_ul_total (MB)

    def __init__(self):
        super().__init__()
        self._running = False
        self._prev = psutil.net_io_counters()
        self._prev_time = time.time()
        self._session_dl = 0.0
        self._session_ul = 0.0

    def run(self):
        self._running = True
        while self._running:
            time.sleep(1)
            now = psutil.net_io_counters()
            t = time.time()
            dt = t - self._prev_time
            if dt > 0:
                dl = (now.bytes_recv - self._prev.bytes_recv) / dt / 1024 / 1024
                ul = (now.bytes_sent - self._prev.bytes_sent) / dt / 1024 / 1024
                dl = max(0.0, dl)
                ul = max(0.0, ul)
                self._session_dl += dl * dt / 1024  # GB
                self._session_ul += ul * dt / 1024
                self.updated.emit(dl, ul, self._session_dl * 1024, self._session_ul * 1024)
            self._prev = now
            self._prev_time = t

    def stop(self):
        self._running = False
        self.wait()

# ─────────────────────────────────────────────
# System Monitor Thread
# ─────────────────────────────────────────────
class SysMonitor(QThread):
    updated = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._running = False
        # Per-process net tracking
        self._proc_net_prev = {}
        self._proc_net_session = {}  # bytes per pid
        self._prev_net_time = time.time()

    def run(self):
        self._running = True
        while self._running:
            try:
                self.updated.emit(self._collect())
            except Exception as e:
                print(f"SysMonitor error: {e}")
            time.sleep(1)

    def _collect(self):
        # CPU & RAM
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        ram_pct = ram.percent
        ram_used = ram.used / 1024**3
        ram_total = ram.total / 1024**3

        # Uptime
        uptime_sec = int(time.time() - psutil.boot_time())

        # Visible windows
        windows = self._get_windows()

        # App net usage (session total, approximated via connection count + bytes if available)
        app_net = self._get_app_net()

        return {
            "cpu": cpu,
            "ram_pct": ram_pct,
            "ram_used": ram_used,
            "ram_total": ram_total,
            "uptime": self._fmt(uptime_sec),
            "windows": windows,
            "app_net": app_net,
        }

    def _get_windows(self):
        results = []
        if WIN32_OK:
            seen_pids = {}
            def cb(hwnd, _):
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return
                    title = win32gui.GetWindowText(hwnd)
                    if not title:
                        return
                    rect = win32gui.GetWindowRect(hwnd)
                    if (rect[2]-rect[0]) < 100 or (rect[3]-rect[1]) < 50:
                        return
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid in seen_pids:
                        return
                    seen_pids[pid] = True
                    try:
                        proc = psutil.Process(pid)
                        age = int(time.time() - proc.create_time())
                        pname = proc.name().replace(".exe", "")
                        cpu_p = proc.cpu_percent(interval=None)
                        mem_mb = proc.memory_info().rss / 1024**2
                    except:
                        return
                    results.append({
                        "title": title[:44],
                        "name": pname,
                        "age": self._fmt(age),
                        "cpu": cpu_p,
                        "mem": mem_mb,
                    })
                except:
                    pass
            win32gui.EnumWindows(cb, None)
        else:
            for proc in psutil.process_iter(["pid", "name", "create_time"]):
                try:
                    age = int(time.time() - proc.info["create_time"])
                    name = proc.info["name"].replace(".exe", "")
                    results.append({
                        "title": name, "name": name,
                        "age": self._fmt(age), "cpu": 0, "mem": 0,
                    })
                except:
                    continue
        return results[:18]

    def _get_app_net(self):
        """Approximate per-app network via active connections count."""
        app_conns = {}
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                conns = len(proc.net_connections())
                if conns > 0:
                    name = proc.info["name"].replace(".exe", "")
                    if name in app_conns:
                        app_conns[name] += conns
                    else:
                        app_conns[name] = conns
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        sorted_apps = sorted(app_conns.items(), key=lambda x: x[1], reverse=True)
        return sorted_apps[:8]

    def _fmt(self, s):
        if s < 60: return f"{s}s"
        elif s < 3600: return f"{s//60}m {s%60}s"
        elif s < 86400: return f"{s//3600}h {(s%3600)//60}m"
        else: return f"{s//86400}d {(s%86400)//3600}h"

    def stop(self):
        self._running = False
        self.wait()

# ─────────────────────────────────────────────
# Live Graph Widget
# ─────────────────────────────────────────────
class LiveGraph(QWidget):
    def __init__(self, color1="#00ff88", color2="#ff6b6b", label1="", label2="", parent=None):
        super().__init__(parent)
        self.color1 = QColor(color1)
        self.color2 = QColor(color2)
        self.label1 = label1
        self.label2 = label2
        self.hist1 = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
        self.hist2 = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def push(self, v1, v2=None):
        self.hist1.append(v1)
        if v2 is not None:
            self.hist2.append(v2)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#0a0a16"))

        # Grid
        grid = QPen(QColor("#15152a")); grid.setWidth(1); p.setPen(grid)
        for i in range(1, 4):
            y = int(h * i / 4)
            p.drawLine(0, y, w, y)

        has2 = any(v > 0 for v in self.hist2)
        max_v = max(max(self.hist1), max(self.hist2) if has2 else 0, 0.1)

        def draw_series(hist, color):
            data = list(hist); n = len(data)
            pts = []
            for i, v in enumerate(data):
                x = int(i * w / max(n - 1, 1))
                y = int(h - (v / max_v) * (h - 6) - 3)
                pts.append(QPointF(x, y))
            if len(pts) < 2:
                return
            fc = QColor(color); fc.setAlpha(30)
            poly = [QPointF(0, h)] + pts + [QPointF(w, h)]
            p.setBrush(QBrush(fc)); p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF(poly))
            pen = QPen(color); pen.setWidth(2); p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(len(pts) - 1):
                p.drawLine(pts[i], pts[i + 1])

        if has2:
            draw_series(self.hist2, self.color2)
        draw_series(self.hist1, self.color1)
        p.end()

# ─────────────────────────────────────────────
# Tray Icon
# ─────────────────────────────────────────────
def make_tray_icon(color="#00d4ff"):
    pix = QPixmap(16, 16)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor(color)))
    p.setPen(Qt.PenStyle.NoPen)
    # Eye shape
    p.drawEllipse(1, 4, 14, 8)
    p.setBrush(QBrush(QColor("#0b0b18")))
    p.drawEllipse(5, 5, 6, 6)
    p.setBrush(QBrush(QColor(color)))
    p.drawEllipse(7, 7, 2, 2)
    p.end()
    return QIcon(pix)

# ─────────────────────────────────────────────
# Settings Dialog
# ─────────────────────────────────────────────
class SettingsDialog(QDialog):
    theme_changed = pyqtSignal(dict)
    config_changed = pyqtSignal(dict)

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self.theme = config.data["theme"].copy()
        self.setWindowTitle("EyeView Settings")
        self.setModal(True)
        self.setMinimumWidth(400)
        self._build()
        self._apply_style()

    def _apply_style(self):
        t = self.theme
        self.setStyleSheet(f"""
            QDialog, QWidget {{ background:{t['bg_main']}; color:{t['text_main']};
                font-family:'Segoe UI'; font-size:12px; }}
            QTabWidget::pane {{ border:1px solid {t['bg_border']}; border-radius:6px; }}
            QTabBar::tab {{ background:{t['bg_card']}; color:{t['text_dim']};
                padding:6px 16px; border-radius:4px 4px 0 0; border:1px solid {t['bg_border']}; }}
            QTabBar::tab:selected {{ color:{t['accent_ui']}; border-bottom:2px solid {t['accent_ui']}; }}
            QPushButton {{ background:{t['bg_card']}; border:1px solid {t['bg_border']};
                border-radius:6px; color:{t['text_main']}; padding:6px 14px; }}
            QPushButton:hover {{ border-color:{t['accent_ui']}; color:{t['accent_ui']}; }}
            QCheckBox {{ color:{t['text_main']}; spacing:8px; }}
            QCheckBox::indicator {{ width:16px; height:16px; border:1px solid {t['bg_border']};
                border-radius:3px; background:{t['bg_card']}; }}
            QCheckBox::indicator:checked {{ background:{t['accent_ui']}; }}
            QDoubleSpinBox, QSpinBox {{ background:{t['bg_card']}; border:1px solid {t['bg_border']};
                border-radius:4px; color:{t['text_main']}; padding:4px; }}
            QGroupBox {{ border:1px solid {t['bg_border']}; border-radius:8px;
                margin-top:12px; padding:8px; color:{t['text_dim']}; font-size:10px; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 4px; }}
        """)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("⚙  Settings")
        title.setStyleSheet(f"color:{self.theme['accent_ui']};font-size:15px;font-weight:bold;")
        header.addWidget(title)
        ver = QLabel(f"v{VERSION}")
        ver.setStyleSheet(f"color:{self.theme['text_dim']};font-size:10px;")
        header.addStretch()
        header.addWidget(ver)
        layout.addLayout(header)

        tabs = QTabWidget()
        tabs.addTab(self._appearance_tab(), "Appearance")
        tabs.addTab(self._alerts_tab(), "Alerts")
        tabs.addTab(self._system_tab(), "System")
        layout.addWidget(tabs)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _appearance_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(10)

        form = QFormLayout(); form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._color_btns = {}
        for key, label in [
            ("bg_main",    "Background"),
            ("bg_card",    "Card"),
            ("bg_border",  "Border"),
            ("text_main",  "Text"),
            ("text_dim",   "Dim Text"),
            ("accent_ui",  "UI Accent"),
            ("accent_dl",  "Download"),
            ("accent_ul",  "Upload"),
            ("accent_cpu", "CPU"),
            ("accent_ram", "RAM"),
        ]:
            btn = QPushButton(); btn.setFixedSize(90, 26)
            self._set_color_btn(btn, self.theme[key])
            btn.clicked.connect(lambda _, k=key, b=btn: self._pick_color(k, b))
            form.addRow(label + ":", btn)
            self._color_btns[key] = btn

        layout.addLayout(form)

        fs_row = QHBoxLayout()
        fs_row.addWidget(QLabel("Font Size:"))
        self.font_spin = QSpinBox()
        self.font_spin.setRange(8, 18)
        self.font_spin.setValue(self.theme.get("font_size", 11))
        fs_row.addWidget(self.font_spin); fs_row.addStretch()
        layout.addLayout(fs_row)

        reset_btn = QPushButton("↺  Reset to Default")
        reset_btn.clicked.connect(self._reset_theme)
        layout.addWidget(reset_btn)
        layout.addStretch()
        return w

    def _alerts_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(12)

        self.alerts_check = QCheckBox("Enable threshold alerts")
        self.alerts_check.setChecked(self.config.get("alerts_enabled", True))
        layout.addWidget(self.alerts_check)

        cpu_group = QGroupBox("CPU Alert")
        cg = QFormLayout(cpu_group)
        self.cpu_thresh = QDoubleSpinBox()
        self.cpu_thresh.setRange(10, 100); self.cpu_thresh.setSuffix(" %")
        self.cpu_thresh.setValue(self.config.get("cpu_alert_threshold", 90.0))
        cg.addRow("Trigger when CPU above:", self.cpu_thresh)
        layout.addWidget(cpu_group)

        ram_group = QGroupBox("RAM Alert")
        rg = QFormLayout(ram_group)
        self.ram_thresh = QDoubleSpinBox()
        self.ram_thresh.setRange(10, 100); self.ram_thresh.setSuffix(" %")
        self.ram_thresh.setValue(self.config.get("ram_alert_threshold", 90.0))
        rg.addRow("Trigger when RAM above:", self.ram_thresh)
        layout.addWidget(ram_group)

        cd_row = QHBoxLayout()
        cd_row.addWidget(QLabel("Alert cooldown:"))
        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(10, 600); self.cooldown_spin.setSuffix(" sec")
        self.cooldown_spin.setValue(self.config.get("alert_cooldown", 60))
        cd_row.addWidget(self.cooldown_spin); cd_row.addStretch()
        layout.addLayout(cd_row)
        layout.addStretch()
        return w

    def _system_tab(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(12)

        self.startup_check = QCheckBox("Launch EyeView on Windows startup")
        self.startup_check.setChecked(get_startup())
        layout.addWidget(self.startup_check)

        info_group = QGroupBox("About")
        ig = QVBoxLayout(info_group)
        ig.addWidget(QLabel(f"EyeView v{VERSION}"))
        ig.addWidget(QLabel("Open source system monitor"))
        ig.addWidget(QLabel("MIT License"))
        layout.addWidget(info_group)
        layout.addStretch()
        return w

    def _set_color_btn(self, btn, hex_c):
        c = QColor(hex_c)
        luma = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        txt = "#000" if luma > 128 else "#fff"
        btn.setStyleSheet(f"background:{hex_c};color:{txt};border-radius:4px;font-size:10px;")
        btn.setText(hex_c)

    def _pick_color(self, key, btn):
        c = QColorDialog.getColor(QColor(self.theme[key]), self)
        if c.isValid():
            self.theme[key] = c.name()
            self._set_color_btn(btn, c.name())

    def _reset_theme(self):
        self.theme = DEFAULT_THEME.copy()
        for k, b in self._color_btns.items():
            self._set_color_btn(b, self.theme[k])
        self.font_spin.setValue(self.theme["font_size"])

    def _save(self):
        self.theme["font_size"] = self.font_spin.value()
        self.theme_changed.emit(self.theme)

        cfg_update = {
            "alerts_enabled": self.alerts_check.isChecked(),
            "cpu_alert_threshold": self.cpu_thresh.value(),
            "ram_alert_threshold": self.ram_thresh.value(),
            "alert_cooldown": self.cooldown_spin.value(),
            "startup": self.startup_check.isChecked(),
        }
        set_startup(cfg_update["startup"])
        self.config_changed.emit(cfg_update)
        self.accept()

# ─────────────────────────────────────────────
# Main Window
# ─────────────────────────────────────────────
class EyeViewWindow(QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.theme = config.data["theme"].copy()
        self._last_cpu_alert = 0
        self._last_ram_alert = 0
        self._session_dl = 0.0
        self._session_ul = 0.0

        self.setWindowTitle(f"EyeView v{VERSION}")
        self._restore_geometry()
        self._build_ui()
        self._setup_tray()
        self._apply_theme()
        self._start_monitors()

    def _restore_geometry(self):
        x = self.config.get("window_x", 100)
        y = self.config.get("window_y", 100)
        w = self.config.get("window_w", 480)
        h = self.config.get("window_h", 760)
        self.setGeometry(x, y, w, h)

    # ── Build UI ─────────────────────────────
    def _build_ui(self):
        root_w = QWidget()
        self.setCentralWidget(root_w)
        self.main_layout = QVBoxLayout(root_w)
        self.main_layout.setContentsMargins(14, 12, 14, 12)
        self.main_layout.setSpacing(10)

        # ── Header ───────────────────────────
        hdr = QHBoxLayout()
        icon_lbl = QLabel("👁")
        icon_lbl.setStyleSheet("font-size:20px;")
        self.title_lbl = QLabel("EYEVIEW")
        self.title_lbl.setStyleSheet("font-size:17px;font-weight:bold;letter-spacing:4px;margin-left:4px;")
        hdr.addWidget(icon_lbl)
        hdr.addWidget(self.title_lbl)
        hdr.addStretch()
        self.uptime_lbl = QLabel("⏱  --")
        self.uptime_lbl.setStyleSheet("font-size:10px;padding:3px 8px;border-radius:10px;")
        hdr.addWidget(self.uptime_lbl)
        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setFixedSize(30, 30)
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.clicked.connect(self._open_settings)
        hdr.addWidget(self.settings_btn)
        self.main_layout.addLayout(hdr)
        self.main_layout.addWidget(self._sep())

        # ── Session Summary ───────────────────
        self.main_layout.addWidget(self._sec_label("SESSION SUMMARY"))
        summary_card = self._card()
        summary_layout = QHBoxLayout(summary_card)
        summary_layout.setSpacing(0)

        self.session_dl_lbl = self._stat_block(summary_layout, "▼ Downloaded", "0.00 MB", "accent_dl")
        self._vdiv(summary_layout)
        self.session_ul_lbl = self._stat_block(summary_layout, "▲ Uploaded", "0.00 MB", "accent_ul")
        self._vdiv(summary_layout)
        self.session_time_lbl = self._stat_block(summary_layout, "⏱ Session", "--", "accent_ui")
        self.main_layout.addWidget(summary_card)
        self.main_layout.addWidget(self._sep())

        # ── Network Speed ────────────────────
        self.main_layout.addWidget(self._sec_label("NETWORK"))
        net_card = self._card()
        net_v = QVBoxLayout(net_card)
        net_v.setSpacing(6)

        speed_row = QHBoxLayout()
        speed_row.setSpacing(0)
        self.dl_val = self._speed_val(speed_row, "▼  DOWNLOAD", "accent_dl")
        self._vdiv(speed_row)
        self.ul_val = self._speed_val(speed_row, "▲  UPLOAD", "accent_ul")
        net_v.addLayout(speed_row)

        # Graph legend
        legend = QHBoxLayout()
        self.dl_dot = QLabel("● Download")
        self.ul_dot = QLabel("● Upload")
        legend.addWidget(self.dl_dot); legend.addSpacing(10)
        legend.addWidget(self.ul_dot); legend.addStretch()
        net_v.addLayout(legend)

        self.net_graph = LiveGraph()
        net_v.addWidget(self.net_graph)
        self.main_layout.addWidget(net_card)
        self.main_layout.addWidget(self._sep())

        # ── CPU & RAM ────────────────────────
        self.main_layout.addWidget(self._sec_label("CPU & RAM"))
        sysres_card = self._card()
        sysres_v = QVBoxLayout(sysres_card)
        sysres_v.setSpacing(6)

        cr_row = QHBoxLayout(); cr_row.setSpacing(0)
        self.cpu_val = self._speed_val(cr_row, "⚡  CPU", "accent_cpu")
        self._vdiv(cr_row)
        self.ram_val = self._speed_val(cr_row, "🧠  RAM", "accent_ram")
        sysres_v.addLayout(cr_row)

        # Graph legend
        cr_legend = QHBoxLayout()
        self.cpu_dot = QLabel("● CPU")
        self.ram_dot = QLabel("● RAM")
        cr_legend.addWidget(self.cpu_dot); cr_legend.addSpacing(10)
        cr_legend.addWidget(self.ram_dot); cr_legend.addStretch()
        sysres_v.addLayout(cr_legend)

        self.cpu_ram_graph = LiveGraph()
        sysres_v.addWidget(self.cpu_ram_graph)
        self.main_layout.addWidget(sysres_card)
        self.main_layout.addWidget(self._sep())

        # ── App Net Usage ────────────────────
        self.main_layout.addWidget(self._sec_label("ACTIVE CONNECTIONS BY APP"))
        appnet_card = self._card()
        appnet_v = QVBoxLayout(appnet_card)
        appnet_v.setSpacing(3)
        self.appnet_rows = []
        for _ in range(8):
            row_w, nl, vl = self._list_row(appnet_v, val_color="accent_dl")
            self.appnet_rows.append((row_w, nl, vl))
        self.main_layout.addWidget(appnet_card)
        self.main_layout.addWidget(self._sep())

        # ── Open Windows ─────────────────────
        self.main_layout.addWidget(self._sec_label("OPEN WINDOWS"))
        wins_card = self._card()
        wl = QVBoxLayout(wins_card); wl.setContentsMargins(4, 4, 4, 4)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFixedHeight(240)
        scroll.setStyleSheet("background:transparent;border:none;")
        inner = QWidget(); inner.setStyleSheet("background:transparent;")
        self.wins_vl = QVBoxLayout(inner)
        self.wins_vl.setSpacing(1); self.wins_vl.setContentsMargins(4, 4, 4, 4)
        self.win_rows = []
        for _ in range(18):
            rw, nl, al = self._win_row()
            self.wins_vl.addWidget(rw)
            self.win_rows.append((rw, nl, al))
        self.wins_vl.addStretch()
        scroll.setWidget(inner); wl.addWidget(scroll)
        self.main_layout.addWidget(wins_card)

        # Session timer
        self._session_start = time.time()
        self._session_timer = QTimer()
        self._session_timer.timeout.connect(self._update_session_time)
        self._session_timer.start(1000)

    # ── Widget Helpers ───────────────────────
    def _card(self):
        w = QWidget(); w.setProperty("isCard", True); return w

    def _sep(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine); return f

    def _sec_label(self, text):
        l = QLabel(text); l.setProperty("isSection", True); return l

    def _vdiv(self, layout):
        f = QFrame(); f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet(f"color:{self.theme['bg_border']};")
        layout.addWidget(f)

    def _stat_block(self, parent_layout, label, value, accent_key):
        c = QWidget(); c.setStyleSheet("background:transparent;border:none;")
        l = QVBoxLayout(c); l.setContentsMargins(10, 8, 10, 8); l.setSpacing(2)
        lbl = QLabel(label); lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"font-size:9px;letter-spacing:1px;color:{self.theme['text_dim']};border:none;")
        val = QLabel(value); val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        val.setStyleSheet(f"font-size:14px;font-weight:bold;color:{self.theme[accent_key]};border:none;")
        l.addWidget(lbl); l.addWidget(val)
        parent_layout.addWidget(c, 1)
        return val

    def _speed_val(self, parent_layout, label, accent_key):
        c = QWidget(); c.setStyleSheet("background:transparent;border:none;")
        l = QVBoxLayout(c); l.setContentsMargins(12, 8, 12, 8); l.setSpacing(3)
        lbl = QLabel(label); lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"font-size:9px;letter-spacing:1px;color:{self.theme[accent_key]};font-weight:bold;border:none;")
        val = QLabel("0.00 MB/s"); val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        val.setStyleSheet(f"font-size:21px;font-weight:bold;color:{self.theme[accent_key]};border:none;")
        l.addWidget(lbl); l.addWidget(val)
        parent_layout.addWidget(c, 1)
        return val

    def _list_row(self, parent_layout, val_color="accent_ui"):
        rw = QWidget(); rw.setFixedHeight(28)
        rw.setStyleSheet("background:transparent;")
        hl = QHBoxLayout(rw); hl.setContentsMargins(6, 0, 6, 0); hl.setSpacing(8)
        nl = QLabel("—"); nl.setStyleSheet(f"color:{self.theme['text_main']};font-size:11px;")
        vl = QLabel("")
        vl.setFixedWidth(90)
        vl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        vl.setStyleSheet(f"color:{self.theme[val_color]};font-size:10px;")
        hl.addWidget(nl, 1); hl.addWidget(vl)
        parent_layout.addWidget(rw)
        return rw, nl, vl

    def _win_row(self):
        rw = QWidget(); rw.setFixedHeight(34)
        rw.setStyleSheet("background:transparent;")
        hl = QHBoxLayout(rw); hl.setContentsMargins(6, 2, 6, 2); hl.setSpacing(8)
        nl = QLabel("—")
        nl.setStyleSheet(f"color:{self.theme['text_main']};font-size:11px;")
        al = QLabel("")
        al.setFixedWidth(68)
        al.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        al.setStyleSheet(f"color:{self.theme['accent_ui']};font-size:13px;font-weight:bold;")
        hl.addWidget(nl, 1); hl.addWidget(al)
        return rw, nl, al

    # ── Theme ────────────────────────────────
    def _apply_theme(self):
        t = self.theme
        fs = t["font_size"]
        adl = t["accent_dl"]; aul = t["accent_ul"]
        aui = t["accent_ui"]; acpu = t["accent_cpu"]; aram = t["accent_ram"]

        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color:{t['bg_main']};
                color:{t['text_main']};
                font-family:'Segoe UI',sans-serif;
                font-size:{fs}px;
            }}
            QWidget[isCard="true"] {{
                background:{t['bg_card']};
                border:1px solid {t['bg_border']};
                border-radius:10px;
            }}
            QFrame {{ color:{t['bg_border']}; }}
            QLabel[isSection="true"] {{
                color:{t['text_dim']};
                font-size:{max(8,fs-2)}px;
                letter-spacing:2px;
            }}
            QScrollBar:vertical {{
                background:{t['bg_main']};width:5px;border-radius:3px;
            }}
            QScrollBar::handle:vertical {{
                background:{t['bg_border']};border-radius:3px;
            }}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{ height:0; }}
            QPushButton {{
                background:{t['bg_card']};border:1px solid {t['bg_border']};
                border-radius:6px;color:{t['text_main']};padding:4px 10px;
            }}
            QPushButton:hover {{ border-color:{aui};color:{aui}; }}
        """)

        self.title_lbl.setStyleSheet(
            f"font-size:17px;font-weight:bold;letter-spacing:4px;margin-left:4px;color:{aui};"
        )
        self.uptime_lbl.setStyleSheet(
            f"font-size:10px;padding:3px 8px;border-radius:10px;"
            f"background:{t['bg_card']};border:1px solid {t['bg_border']};color:{t['text_dim']};"
        )

        # Speed values
        for widget, color in [
            (self.dl_val, adl), (self.ul_val, aul),
            (self.cpu_val, acpu), (self.ram_val, aram),
        ]:
            widget.setStyleSheet(
                f"font-size:{fs+10}px;font-weight:bold;color:{color};border:none;"
            )

        # Dots
        self.dl_dot.setStyleSheet(f"color:{adl};font-size:10px;")
        self.ul_dot.setStyleSheet(f"color:{aul};font-size:10px;")
        self.cpu_dot.setStyleSheet(f"color:{acpu};font-size:10px;")
        self.ram_dot.setStyleSheet(f"color:{aram};font-size:10px;")

        # Session labels
        self.session_dl_lbl.setStyleSheet(f"font-size:14px;font-weight:bold;color:{adl};border:none;")
        self.session_ul_lbl.setStyleSheet(f"font-size:14px;font-weight:bold;color:{aul};border:none;")
        self.session_time_lbl.setStyleSheet(f"font-size:14px;font-weight:bold;color:{aui};border:none;")

        # Graphs
        self.net_graph.color1 = QColor(adl)
        self.net_graph.color2 = QColor(aul)
        self.net_graph.update()
        self.cpu_ram_graph.color1 = QColor(acpu)
        self.cpu_ram_graph.color2 = QColor(aram)
        self.cpu_ram_graph.update()

        # Win rows
        for _, nl, al in self.win_rows:
            nl.setStyleSheet(f"color:{t['text_main']};font-size:{fs}px;")
            al.setStyleSheet(f"color:{aui};font-size:{fs+2}px;font-weight:bold;")

        # App net rows
        for _, nl, vl in self.appnet_rows:
            nl.setStyleSheet(f"color:{t['text_main']};font-size:{fs}px;")
            vl.setStyleSheet(f"color:{adl};font-size:{max(9,fs-1)}px;")

        # Tray icon color update
        if hasattr(self, "tray"):
            self.tray.setIcon(make_tray_icon(aui))

    # ── Tray ─────────────────────────────────
    def _setup_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon(self.theme["accent_ui"]), self)
        self.tray.setToolTip("EyeView")
        menu = QMenu()
        show_action = QAction("Show", self)
        show_action.triggered.connect(self.show)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.raise_()
            self.activateWindow()

    # ── Monitors ─────────────────────────────
    def _start_monitors(self):
        self.net_mon = NetMonitor()
        self.net_mon.updated.connect(self._on_net)
        self.net_mon.start()

        self.sys_mon = SysMonitor()
        self.sys_mon.updated.connect(self._on_sys)
        self.sys_mon.start()

    # ── Callbacks ────────────────────────────
    def _on_net(self, dl, ul, session_dl_mb, session_ul_mb):
        self.dl_val.setText(f"{dl:.2f} MB/s")
        self.ul_val.setText(f"{ul:.2f} MB/s")
        self.net_graph.push(dl, ul)

        # Session totals
        if session_dl_mb >= 1024:
            self.session_dl_lbl.setText(f"{session_dl_mb/1024:.2f} GB")
        else:
            self.session_dl_lbl.setText(f"{session_dl_mb:.1f} MB")

        if session_ul_mb >= 1024:
            self.session_ul_lbl.setText(f"{session_ul_mb/1024:.2f} GB")
        else:
            self.session_ul_lbl.setText(f"{session_ul_mb:.1f} MB")

    def _on_sys(self, data):
        cpu = data["cpu"]
        ram_pct = data["ram_pct"]
        ram_used = data["ram_used"]
        ram_total = data["ram_total"]

        self.uptime_lbl.setText(f"⏱  {data['uptime']}")
        self.cpu_val.setText(f"{cpu:.1f}%")
        self.ram_val.setText(f"{ram_pct:.1f}%")
        self.cpu_ram_graph.push(cpu, ram_pct)

        # CPU tooltip
        self.cpu_val.setToolTip(f"CPU Usage: {cpu:.1f}%")
        self.ram_val.setToolTip(f"RAM: {ram_used:.1f} GB / {ram_total:.1f} GB ({ram_pct:.1f}%)")

        # Alerts
        self._check_alerts(cpu, ram_pct)

        # App net
        app_net = data.get("app_net", [])
        for i, (rw, nl, vl) in enumerate(self.appnet_rows):
            if i < len(app_net):
                name, conns = app_net[i]
                nl.setText(name[:28])
                vl.setText(f"{conns} conn")
                rw.show()
            else:
                rw.hide()

        # Windows
        windows = data.get("windows", [])
        for i, (rw, nl, al) in enumerate(self.win_rows):
            if i < len(windows):
                win = windows[i]
                nl.setText(f"{win['name']}  —  {win['title']}")
                al.setText(win["age"])
                rw.show()
            else:
                rw.hide()

    def _update_session_time(self):
        elapsed = int(time.time() - self._session_start)
        if elapsed < 3600:
            self.session_time_lbl.setText(f"{elapsed//60}m {elapsed%60}s")
        else:
            self.session_time_lbl.setText(f"{elapsed//3600}h {(elapsed%3600)//60}m")

    def _check_alerts(self, cpu, ram):
        if not self.config.get("alerts_enabled", True):
            return
        now = time.time()
        cooldown = self.config.get("alert_cooldown", 60)

        if cpu >= self.config.get("cpu_alert_threshold", 90.0):
            if now - self._last_cpu_alert > cooldown:
                self._last_cpu_alert = now
                self.tray.showMessage(
                    "EyeView — CPU Alert",
                    f"CPU usage is at {cpu:.1f}%",
                    QSystemTrayIcon.MessageIcon.Warning,
                    4000
                )

        if ram >= self.config.get("ram_alert_threshold", 90.0):
            if now - self._last_ram_alert > cooldown:
                self._last_ram_alert = now
                self.tray.showMessage(
                    "EyeView — RAM Alert",
                    f"RAM usage is at {ram:.1f}%",
                    QSystemTrayIcon.MessageIcon.Warning,
                    4000
                )

    # ── Settings ─────────────────────────────
    def _open_settings(self):
        dlg = SettingsDialog(self.config, self)
        dlg.theme_changed.connect(self._on_theme_changed)
        dlg.config_changed.connect(self._on_config_changed)
        dlg.exec()

    def _on_theme_changed(self, new_theme):
        self.theme = new_theme
        self.config.data["theme"] = new_theme
        self.config.save()
        self._apply_theme()

    def _on_config_changed(self, cfg):
        for k, v in cfg.items():
            self.config.set(k, v)

    def closeEvent(self, event):
        # Save window position
        geo = self.geometry()
        self.config.set("window_x", geo.x())
        self.config.set("window_y", geo.y())
        self.config.set("window_w", geo.width())
        self.config.set("window_h", geo.height())
        # Minimize to tray instead of closing
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "EyeView",
            "Running in the background. Double-click tray icon to restore.",
            QSystemTrayIcon.MessageIcon.Information,
            2500
        )

# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)  # Stay alive in tray

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,      QColor("#0b0b18"))
    pal.setColor(QPalette.ColorRole.WindowText,  QColor("#dddddd"))
    pal.setColor(QPalette.ColorRole.Base,        QColor("#0f0f22"))
    pal.setColor(QPalette.ColorRole.Text,        QColor("#dddddd"))
    pal.setColor(QPalette.ColorRole.Button,      QColor("#0f0f22"))
    pal.setColor(QPalette.ColorRole.ButtonText,  QColor("#dddddd"))
    pal.setColor(QPalette.ColorRole.Highlight,   QColor("#00d4ff"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#0b0b18"))
    app.setPalette(pal)

    config = Config()
    win = EyeViewWindow(config)
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
