import os
import time
import re
import sys
from functools import partial
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, QThread, Qt, Signal, QEvent, QTimer, QPointF
from PySide6.QtGui import QPainter, QPainterPath, QColor, QPen, QBrush, QLinearGradient, QAction, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QSlider,
    QSystemTrayIcon,
    QMenu,
    QStyle,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


def sanitize_filename(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]+", "_", name).strip() or "video"


QUALITY_PRESETS = {
    "Best (Video + Audio)": {"format": "best"},
    "1080p (MP4)": {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
    },
    "720p (MP4)": {
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "merge_output_format": "mp4",
    },
    "Audio Only (MP3)": {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    },
}


class DownloadWorker(QObject):
    progress = Signal(int, float, str)
    log = Signal(int, str)
    finished = Signal(int, bool, str)
    speed_updated = Signal(int, float)

    def __init__(
        self,
        row: int,
        url: str,
        output_dir: str,
        cookie_file: str = "",
        quality_preset = "Best (Video + Audio)",
    ):
        super().__init__()
        self.row = row
        self.url = url
        self.output_dir = output_dir
        self.cookie_file = cookie_file
        self.quality_preset = quality_preset
        self._cancelled = False
        self.last_update_time = 0.0

    def cancel(self) -> None:
        self._cancelled = True

    def _progress_hook(self, data: dict) -> None:
        if self._cancelled:
            raise DownloadError("Download canceled by user.")

        status = data.get("status", "")
        current_time = time.time()
        
        if status == "downloading":
            # Rate limit to 10Hz (every 100ms) to prevent GUI thread event flooding
            if current_time - self.last_update_time < 0.1:
                return
            self.last_update_time = current_time

            downloaded = data.get("downloaded_bytes", 0)
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            speed = data.get("speed")
            if isinstance(speed, (int, float)):
                self.speed_updated.emit(self.row, float(speed))
            
            if total > 0:
                percent = (downloaded / total) * 100
                eta = data.get("eta")
                speed_text = (
                    f"{speed / 1024 / 1024:.2f} MB/s" if isinstance(speed, (int, float)) else "N/A"
                )
                eta_text = f"{eta}s" if isinstance(eta, int) else "N/A"
                self.progress.emit(self.row, percent, f"{percent:.1f}% | {speed_text} | ETA: {eta_text}")
            else:
                self.progress.emit(self.row, 0.0, "Downloading...")
        elif status == "finished":
            self.progress.emit(self.row, 100.0, "Download complete, processing file...")

    def run(self) -> None:
        try:
            os.makedirs(self.output_dir, exist_ok=True)

            ydl_opts = {
                "outtmpl": str(Path(self.output_dir) / "%(title)s.%(ext)s"),
                "noplaylist": True,
                "restrictfilenames": False,
                "windowsfilenames": True,
                "progress_hooks": [self._progress_hook],
                "quiet": True,
                "no_warnings": True,
            }

            if isinstance(self.quality_preset, dict):
                preset = self.quality_preset
            else:
                preset = QUALITY_PRESETS.get(
                    self.quality_preset, QUALITY_PRESETS["Best (Video + Audio)"]
                )
            
            ydl_opts["format"] = preset.get("format") or preset.get("format_id")
            if "merge_output_format" in preset:
                ydl_opts["merge_output_format"] = preset["merge_output_format"]
            if "postprocessors" in preset:
                ydl_opts["postprocessors"] = list(preset["postprocessors"])

            # Support presenting name or preset string in logs
            preset_name = preset.get("name") if isinstance(self.quality_preset, dict) else self.quality_preset
            self.log.emit(self.row, f"Format: {preset_name}")
            if self.cookie_file and Path(self.cookie_file).exists():
                ydl_opts["cookiefile"] = self.cookie_file
                self.log.emit(self.row, "Using cookies file for authenticated download.")

            self.log.emit(self.row, "Fetching video info...")
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
                title = sanitize_filename(info.get("title", "video"))
                self.log.emit(self.row, f"Title: {title}")
                self.log.emit(self.row, "Starting download...")
                ydl.download([self.url])

            if self._cancelled:
                self.finished.emit(self.row, False, "Download canceled.")
                return

            self.finished.emit(self.row, True, "Download completed successfully.")
        except DownloadError as exc:
            if self._cancelled:
                self.finished.emit(self.row, False, "Download canceled.")
            else:
                self.finished.emit(self.row, False, f"Download failed: {exc}")
        except Exception as exc:
            self.finished.emit(self.row, False, f"Error: {exc}")


class FormatAnalyzerWorker(QObject):
    finished = Signal(bool, list, str) # success, formats, error_msg

    def __init__(self, url: str, cookie_file: str = ""):
        super().__init__()
        self.url = url
        self.cookie_file = cookie_file

    def run(self) -> None:
        try:
            ydl_opts = {
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
            }
            if self.cookie_file and Path(self.cookie_file).exists():
                ydl_opts["cookiefile"] = self.cookie_file

            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
                formats_list = info.get("formats", [])
                
                parsed_formats = []
                seen_heights = set()
                
                for f in formats_list:
                    height = f.get("height")
                    ext = f.get("ext", "")
                    if height and height >= 144:
                        key = (height, ext)
                        if key not in seen_heights:
                            seen_heights.add(key)
                            filesize = f.get("filesize") or f.get("filesize_approx")
                            size_str = f" (~{filesize / 1024 / 1024:.1f} MB)" if filesize else ""
                            parsed_formats.append({
                                "name": f"{height}p ({ext.upper()}){size_str}",
                                "format_id": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
                                "merge_output_format": ext if ext in ("mp4", "mkv") else "mp4"
                            })

                # Extract audio
                audio_formats = [f for f in formats_list if f.get("vcodec") == "none" and f.get("acodec") != "none"]
                if audio_formats:
                    best_audio = max(audio_formats, key=lambda x: x.get("abr") or x.get("tbr") or 0)
                    filesize = best_audio.get("filesize") or best_audio.get("filesize_approx")
                    size_str = f" (~{filesize / 1024 / 1024:.1f} MB)" if filesize else ""
                    parsed_formats.append({
                        "name": f"Audio Only (MP3/M4A){size_str}",
                        "format_id": "bestaudio/best",
                        "postprocessors": [
                            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
                        ]
                    })
                
                parsed_formats.sort(key=lambda x: 0 if "Audio" in x["name"] else int(x["name"].split("p")[0]), reverse=True)
                
                if not parsed_formats:
                    parsed_formats = [{"name": name, "format_id": val["format"]} for name, val in QUALITY_PRESETS.items()]

                self.finished.emit(True, parsed_formats, "")
        except Exception as e:
            self.finished.emit(False, [], str(e))


class SpeedChartWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = [0.0] * 30
        self.setMinimumHeight(120)

    def add_sample(self, speed_bytes: float) -> None:
        speed_mb = speed_bytes / 1024 / 1024
        self.history.pop(0)
        self.history.append(speed_mb)
        self.update()

    def clear_history(self) -> None:
        self.history = [0.0] * 30
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        width = self.width()
        height = self.height()

        painter.fillRect(0, 0, width, height, QColor(0, 0, 0, 0))

        if not self.history:
            return

        max_val = max(self.history)
        if max_val < 1.0:
            max_val = 1.0

        points = []
        step_x = width / (len(self.history) - 1)
        
        for i, val in enumerate(self.history):
            x = i * step_x
            y = height - (val / max_val) * (height - 20) - 10
            points.append(QPointF(x, y))

        grid_pen = QPen(QColor(100, 116, 139, 40), 1, Qt.DashLine)
        painter.setPen(grid_pen)
        for i in range(1, 4):
            y_grid = i * (height / 4)
            painter.drawLine(0, y_grid, width, y_grid)

        text_color = QColor(148, 163, 184, 180)
        painter.setPen(text_color)
        painter.setFont(self.font())
        painter.drawText(8, 18, f"Max: {max_val:.1f} MB/s")

        path = QPainterPath()
        path.moveTo(0, height)
        for pt in points:
            path.lineTo(pt)
        path.lineTo(width, height)
        path.closeSubpath()

        grad = QLinearGradient(0, 0, 0, height)
        grad.setColorAt(0, QColor(99, 102, 241, 100))
        grad.setColorAt(1, QColor(99, 102, 241, 0))
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.NoPen)
        painter.drawPath(path)

        curve_pen = QPen(QColor(99, 102, 241), 2)
        painter.setPen(curve_pen)
        painter.setBrush(Qt.NoBrush)
        
        line_path = QPainterPath()
        if points:
            line_path.moveTo(points[0])
            for pt in points[1:]:
                line_path.lineTo(pt)
        painter.drawPath(line_path)


class MainWindow(QMainWindow):
    COL_URL = 0
    COL_QUALITY = 1
    COL_STATUS = 2
    COL_PROGRESS = 3

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Universal Video Downloader")
        self.resize(980, 720)
        self.setAcceptDrops(True)

        # Force custom taskbar icon on Windows
        try:
            import ctypes
            myappid = "starfalleso.videodownloader.pyside.1"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

        # Set Window Icon
        icon_path = Path("icon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        else:
            self.setWindowIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))

        # Concurrent Queue State
        self.active_downloads = {}  # maps row index -> {"thread": QThread, "worker": DownloadWorker}
        self.active_speeds = {}     # maps row -> float (bytes/sec)
        self.max_concurrent = 2
        self.queue_running = False
        self.stop_queue_requested = False
        self.dark_mode = True
        self._intro_animation: QPropertyAnimation | None = None
        self._intro_played = False

        # Format analyzer thread & worker
        self.analyzer_thread = None
        self.analyzer_worker = None

        self.url_input = QLineEdit()
        self.url_input.setObjectName("urlInput")
        self.url_input.setClearButtonEnabled(True)
        self.url_input.setPlaceholderText(
            "Paste TikTok / YouTube / Instagram / Twitter(X) video URL..."
        )

        self.paste_button = QPushButton("📋 Paste")
        self.paste_button.setObjectName("secondaryButton")
        self.paste_button.setFixedWidth(70)
        self.paste_button.clicked.connect(self.paste_clipboard)

        self.analyze_button = QPushButton("🔍 Analyze")
        self.analyze_button.setObjectName("secondaryButton")
        self.analyze_button.setFixedWidth(85)
        self.analyze_button.clicked.connect(self.analyze_url)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(list(QUALITY_PRESETS.keys()))
        self.quality_combo.setCurrentText("Best (Video + Audio)")

        self.output_input = QLineEdit(str(Path.home() / "Downloads"))
        self.output_input.setClearButtonEnabled(True)
        self.output_input.setPlaceholderText("Select output folder")

        browse_button = QPushButton("Browse")
        browse_button.setObjectName("secondaryButton")
        browse_button.clicked.connect(self.choose_folder)

        self.cookies_input = QLineEdit()
        self.cookies_input.setClearButtonEnabled(True)
        self.cookies_input.setPlaceholderText("Optional: path to cookies.txt")
        cookies_button = QPushButton("Cookies")
        cookies_button.setObjectName("secondaryButton")
        cookies_button.clicked.connect(self.choose_cookies)

        self.enqueue_button = QPushButton("Add To Queue")
        self.enqueue_button.setObjectName("secondaryButton")
        self.enqueue_button.clicked.connect(self.enqueue_urls)

        self.start_button = QPushButton("Start Queue")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_queue)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_download)

        self.remove_button = QPushButton("Remove Selected")
        self.remove_button.setObjectName("secondaryButton")
        self.remove_button.clicked.connect(self.remove_selected_items)

        self.clear_finished_button = QPushButton("Clear Finished")
        self.clear_finished_button.setObjectName("secondaryButton")
        self.clear_finished_button.clicked.connect(self.clear_finished_items)

        self.theme_button = QPushButton("☀️ Light Mode")
        self.theme_button.setObjectName("secondaryButton")
        self.theme_button.setCheckable(True)
        self.theme_button.setChecked(True)
        self.theme_button.clicked.connect(self.toggle_theme)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusPill")
        self.status_label.setProperty("state", "idle")
        self.status_label.setAlignment(Qt.AlignCenter)

        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        self.log_box.setPlaceholderText("Download events will appear here...")

        self.queue_table = QTableWidget(0, 4)
        self.queue_table.setHorizontalHeaderLabels(["URL", "Quality", "Status", "Progress"])
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.queue_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.queue_table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.queue_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.queue_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.queue_table.setWordWrap(False)
        self.queue_table.setTextElideMode(Qt.ElideMiddle)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.verticalHeader().setVisible(False)
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(self.COL_URL, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_QUALITY, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_PROGRESS, QHeaderView.ResizeToContents)
        self.queue_table.itemSelectionChanged.connect(self._update_queue_buttons)

        # System Tray Icon Setup
        self.tray_icon = QSystemTrayIcon(self)
        icon_path = Path("icon.ico")
        if icon_path.exists():
            self.tray_icon.setIcon(QIcon(str(icon_path)))
        else:
            self.tray_icon.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        
        tray_menu = QMenu()
        restore_action = tray_menu.addAction("Restore")
        restore_action.triggered.connect(self.showNormal)
        restore_action.triggered.connect(self.activateWindow)
        exit_action = tray_menu.addAction("Exit")
        exit_action.triggered.connect(QApplication.instance().quit)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

        central = QWidget(objectName="root")
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(14)

        header_card = QFrame(objectName="headerCard")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(18, 14, 18, 14)
        header_layout.setSpacing(6)

        title_label = QLabel("Universal Video Downloader", objectName="titleLabel")
        subtitle_label = QLabel(
            "Queue multiple links and pick quality for TikTok, YouTube, Instagram, and Twitter/X.",
            objectName="subtitleLabel",
        )
        subtitle_label.setWordWrap(True)

        chips_layout = QHBoxLayout()
        chips_layout.setContentsMargins(0, 0, 0, 0)
        chips_layout.setSpacing(8)
        for platform in ("TikTok", "YouTube", "Instagram", "Twitter/X"):
            chips_layout.addWidget(self._make_chip(platform))
        chips_layout.addStretch()

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(title_label)
        title_row.addStretch()
        title_row.addWidget(self.theme_button)

        header_layout.addLayout(title_row)
        header_layout.addWidget(subtitle_label)
        header_layout.addLayout(chips_layout)

        input_card = QFrame(objectName="card")
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(16, 16, 16, 16)
        input_layout.setSpacing(10)

        url_layout = QHBoxLayout()
        url_layout.setSpacing(6)
        url_layout.addWidget(self.url_input, 1)
        url_layout.addWidget(self.paste_button)
        url_layout.addWidget(self.analyze_button)

        input_layout.addWidget(QLabel("Video URL", objectName="fieldLabel"))
        input_layout.addLayout(url_layout)
        input_layout.addWidget(QLabel("Quality / Format", objectName="fieldLabel"))
        input_layout.addWidget(self.quality_combo)
        input_layout.addWidget(QLabel("Save To", objectName="fieldLabel"))

        folder_layout = QHBoxLayout()
        folder_layout.setSpacing(8)
        folder_layout.addWidget(self.output_input)
        folder_layout.addWidget(browse_button)
        input_layout.addLayout(folder_layout)

        input_layout.addWidget(QLabel("Cookies File (Optional)", objectName="fieldLabel"))
        cookies_layout = QHBoxLayout()
        cookies_layout.setSpacing(8)
        cookies_layout.addWidget(self.cookies_input)
        cookies_layout.addWidget(cookies_button)
        input_layout.addLayout(cookies_layout)

        queue_card = QFrame(objectName="card")
        queue_layout = QVBoxLayout(queue_card)
        queue_layout.setContentsMargins(16, 14, 16, 16)
        queue_layout.setSpacing(8)

        queue_topbar = QHBoxLayout()
        queue_topbar.setSpacing(8)
        queue_topbar.addWidget(QLabel("Queue", objectName="fieldLabel"))
        queue_topbar.addStretch()
        queue_topbar.addWidget(self.remove_button)
        queue_topbar.addWidget(self.clear_finished_button)

        queue_layout.addLayout(queue_topbar)
        queue_layout.addWidget(self.queue_table)

        controls_card = QFrame(objectName="card")
        controls_layout = QVBoxLayout(controls_card)
        controls_layout.setContentsMargins(16, 16, 16, 16)
        controls_layout.setSpacing(10)

        concurrency_layout = QHBoxLayout()
        concurrency_layout.setSpacing(10)
        concurrency_layout.addWidget(QLabel("Max Parallel Downloads:", objectName="fieldLabel"))
        
        self.concurrency_slider = QSlider(Qt.Horizontal)
        self.concurrency_slider.setRange(1, 5)
        self.concurrency_slider.setValue(2)
        self.concurrency_slider.setTickPosition(QSlider.TicksBelow)
        self.concurrency_slider.setTickInterval(1)
        self.concurrency_slider.setFixedHeight(22)
        
        self.concurrency_label = QLabel("2", objectName="fieldLabel")
        self.concurrency_label.setFixedWidth(20)
        self.concurrency_label.setAlignment(Qt.AlignCenter)
        self.concurrency_slider.valueChanged.connect(self.update_concurrency_value)

        concurrency_layout.addWidget(self.concurrency_slider, 1)
        concurrency_layout.addWidget(self.concurrency_label)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.addWidget(self.enqueue_button, 1)
        button_layout.addWidget(self.start_button, 1)
        button_layout.addWidget(self.cancel_button, 1)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        status_row.addWidget(self.status_label, 0)
        status_row.addWidget(self.progress_bar, 1)

        controls_layout.addLayout(concurrency_layout)
        controls_layout.addLayout(button_layout)
        controls_layout.addLayout(status_row)

        log_card = QFrame(objectName="card")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(16, 14, 16, 16)
        log_layout.setSpacing(8)
        log_layout.addWidget(QLabel("Activity Log", objectName="fieldLabel"))
        log_layout.addWidget(self.log_box)
        self.log_box.setMinimumHeight(140)

        chart_card = QFrame(objectName="card")
        chart_layout = QVBoxLayout(chart_card)
        chart_layout.setContentsMargins(16, 12, 16, 12)
        chart_layout.setSpacing(6)
        chart_layout.addWidget(QLabel("Download Speed Chart", objectName="fieldLabel"))

        self.speed_chart = SpeedChartWidget()
        chart_layout.addWidget(self.speed_chart)

        # Build dashboard split layout
        body_layout = QHBoxLayout()
        body_layout.setSpacing(14)

        left_column = QVBoxLayout()
        left_column.setSpacing(14)
        left_column.addWidget(input_card)
        left_column.addWidget(controls_card)
        left_column.addWidget(chart_card)
        left_column.addStretch()

        right_column = QVBoxLayout()
        right_column.setSpacing(14)
        right_column.addWidget(queue_card, 3)
        right_column.addWidget(log_card, 2)

        body_layout.addLayout(left_column, 2)
        body_layout.addLayout(right_column, 3)

        main_layout.addWidget(header_card)
        main_layout.addLayout(body_layout)

        self.apply_styles()
        self.setCentralWidget(central)
        self._update_queue_buttons()

    def _make_chip(self, text: str) -> QLabel:
        chip = QLabel(text)
        chip.setObjectName(f"chip_{text.replace('/', '_').lower()}")
        chip.setAlignment(Qt.AlignCenter)
        return chip

    def apply_styles(self) -> None:
        base_styles = """
            QWidget#root {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #f8fafc,
                    stop: 0.6 #f1f5f9,
                    stop: 1 #e2e8f0
                );
            }
            QFrame#headerCard, QFrame#card {
                background-color: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
            }
            QLabel {
                color: #0f172a;
                font-family: "Segoe UI", Arial, sans-serif;
            }
            QLabel#titleLabel {
                font-family: "Segoe UI", Arial, sans-serif;
                font-weight: 700;
                color: #0f172a;
                font-size: 24px;
                letter-spacing: -0.5px;
            }
            QLabel#subtitleLabel {
                color: #475569;
                font-size: 13px;
            }
            QLabel#fieldLabel {
                color: #64748b;
                font-size: 11px;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.8px;
            }
            
            /* Platform Chips Light Mode */
            QLabel#chip_tiktok {
                border: 1px solid #99f6e4;
                background-color: #f0fdfa;
                color: #0d9488;
                border-radius: 10px;
                padding: 3px 10px;
                font-size: 11px;
                font-weight: 600;
            }
            QLabel#chip_youtube {
                border: 1px solid #fecaca;
                background-color: #fef2f2;
                color: #dc2626;
                border-radius: 10px;
                padding: 3px 10px;
                font-size: 11px;
                font-weight: 600;
            }
            QLabel#chip_instagram {
                border: 1px solid #fed7aa;
                background-color: #fff7ed;
                color: #ea580c;
                border-radius: 10px;
                padding: 3px 10px;
                font-size: 11px;
                font-weight: 600;
            }
            QLabel#chip_twitter_x {
                border: 1px solid #e2e8f0;
                background-color: #f8fafc;
                color: #475569;
                border-radius: 10px;
                padding: 3px 10px;
                font-size: 11px;
                font-weight: 600;
            }

            QLineEdit {
                background: #f8fafc;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 8px 12px;
                color: #0f172a;
                font-size: 13px;
                font-family: "Segoe UI", Arial, sans-serif;
                selection-background-color: #a5b4fc;
            }
            QLineEdit:focus {
                border: 1px solid #6366f1;
                background: #ffffff;
            }
            QComboBox {
                background: #f8fafc;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 8px 12px;
                color: #0f172a;
                font-size: 13px;
                font-family: "Segoe UI", Arial, sans-serif;
            }
            QComboBox::drop-down {
                border: none;
                width: 26px;
            }
            QComboBox::down-arrow {
                width: 0px;
                height: 0px;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 6px solid #64748b;
                margin-right: 8px;
            }
            QComboBox QAbstractItemView {
                border: 1px solid #e2e8f0;
                background: #ffffff;
                color: #0f172a;
                selection-background-color: #e0e7ff;
                selection-color: #3730a3;
                outline: 0;
            }
            QComboBox QAbstractItemView::item {
                background: #ffffff;
                color: #0f172a;
                min-height: 22px;
                padding: 5px 8px;
            }
            QComboBox QAbstractItemView::item:selected {
                background: #e0e7ff;
                color: #3730a3;
            }
            QPushButton {
                border-radius: 8px;
                padding: 8px 14px;
                font-family: "Segoe UI", Arial, sans-serif;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton#primaryButton {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #4f46e5, stop: 1 #7c3aed
                );
                color: #ffffff;
                border: none;
            }
            QPushButton#primaryButton:hover {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #4338ca, stop: 1 #6d28d9
                );
            }
            QPushButton#secondaryButton {
                background-color: #ffffff;
                color: #475569;
                border: 1px solid #cbd5e1;
            }
            QPushButton#secondaryButton:hover {
                background-color: #f1f5f9;
                color: #0f172a;
                border-color: #94a3b8;
            }
            QPushButton#secondaryButton:checked {
                background-color: #e0e7ff;
                color: #3730a3;
                border-color: #6366f1;
            }
            QPushButton#dangerButton {
                background-color: #fef2f2;
                color: #991b1b;
                border: 1px solid #fca5a5;
            }
            QPushButton#dangerButton:hover {
                background-color: #fee2e2;
                border-color: #f87171;
            }
            QPushButton:disabled {
                background-color: #f1f5f9;
                color: #94a3b8;
                border: 1px solid #e2e8f0;
            }
            QLabel#statusPill {
                min-width: 150px;
                border-radius: 8px;
                padding: 6px 10px;
                font-size: 12px;
                font-weight: 600;
                color: #475569;
                background-color: #f1f5f9;
                border: 1px solid #cbd5e1;
            }
            QLabel#statusPill[state="active"] {
                color: #3730a3;
                background-color: #e0e7ff;
                border: 1px solid #a5b4fc;
            }
            QLabel#statusPill[state="success"] {
                color: #166534;
                background-color: #dcfce7;
                border: 1px solid #86efac;
            }
            QLabel#statusPill[state="warning"] {
                color: #92400e;
                background-color: #fef3c7;
                border: 1px solid #fcd34d;
            }
            QLabel#statusPill[state="error"] {
                color: #991b1b;
                background-color: #fee2e2;
                border: 1px solid #fca5a5;
            }
            QProgressBar {
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                height: 18px;
                background: #f1f5f9;
            }
            QProgressBar::chunk {
                border-radius: 7px;
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #6366f1, stop: 1 #3b82f6
                );
            }
            QPlainTextEdit#logBox {
                background-color: #0f172a;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 8px;
                color: #38bdf8;
                font-family: "Consolas", monospace;
                font-size: 11px;
            }
            QTableWidget {
                background-color: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                gridline-color: #f1f5f9;
                alternate-background-color: #f8fafc;
                selection-background-color: #e0e7ff;
                selection-color: #3730a3;
                color: #0f172a;
                font-size: 12px;
            }
            QHeaderView::section {
                background: #f8fafc;
                color: #475569;
                border: none;
                border-right: 1px solid #e2e8f0;
                border-bottom: 1px solid #e2e8f0;
                padding: 6px;
                font-weight: 600;
            }
            
            /* Custom Scrollbars Light Mode */
            QScrollBar:vertical {
                background: #f1f5f9;
                width: 8px;
                border-radius: 4px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #cbd5e1;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #94a3b8;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                background: none;
                height: 0px;
            }
            QScrollBar:horizontal {
                background: #f1f5f9;
                height: 8px;
                border-radius: 4px;
                margin: 0px;
            }
            QScrollBar::handle:horizontal {
                background: #cbd5e1;
                border-radius: 4px;
                min-width: 20px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #94a3b8;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                background: none;
                width: 0px;
            }

            /* Slider Light Mode */
            QSlider::groove:horizontal {
                border: 1px solid #cbd5e1;
                height: 6px;
                background: #f1f5f9;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #6366f1;
                border: none;
                width: 14px;
                height: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
            QSlider::handle:horizontal:hover {
                background: #4f46e5;
            }
            """
        if self.dark_mode:
            dark_overrides = """
            QWidget#root {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #09090b,
                    stop: 0.5 #18181b,
                    stop: 1 #27272a
                );
            }
            QFrame#headerCard, QFrame#card {
                background-color: #18181b;
                border: 1px solid #27272a;
            }
            QLabel {
                color: #f4f4f5;
            }
            QLabel#titleLabel {
                color: #f4f4f5;
            }
            QLabel#subtitleLabel {
                color: #a1a1aa;
            }
            QLabel#fieldLabel {
                color: #71717a;
            }
            
            /* Platform Chips Dark Mode */
            QLabel#chip_tiktok {
                background-color: #113030;
                border: 1px solid #2dd4bf;
                color: #99f6e4;
            }
            QLabel#chip_youtube {
                background-color: #3f1b1b;
                border: 1px solid #f87171;
                color: #fca5a5;
            }
            QLabel#chip_instagram {
                background-color: #382015;
                border: 1px solid #fb923c;
                color: #fed7aa;
            }
            QLabel#chip_twitter_x {
                background-color: #27272a;
                border: 1px solid #52525b;
                color: #e4e4e7;
            }
            
            QLineEdit {
                background: #09090b;
                border: 1px solid #27272a;
                color: #f4f4f5;
                selection-background-color: #4f46e5;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #6366f1;
                background: #09090b;
            }
            QComboBox {
                background: #09090b;
                border: 1px solid #27272a;
                color: #f4f4f5;
            }
            QComboBox::down-arrow {
                border-top: 6px solid #a1a1aa;
            }
            QComboBox QAbstractItemView {
                border: 1px solid #27272a;
                background: #09090b;
                color: #f4f4f5;
                selection-background-color: #312e81;
                selection-color: #e0e7ff;
            }
            QComboBox QAbstractItemView::item {
                background: #09090b;
                color: #f4f4f5;
            }
            QComboBox QAbstractItemView::item:selected {
                background: #312e81;
                color: #e0e7ff;
            }
            QPushButton#primaryButton {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #6366f1, stop: 1 #8b5cf6
                );
                color: #ffffff;
                border: none;
            }
            QPushButton#primaryButton:hover {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #4f46e5, stop: 1 #7c3aed
                );
            }
            QPushButton#secondaryButton {
                background-color: #27272a;
                color: #e4e4e7;
                border: 1px solid #3f3f46;
            }
            QPushButton#secondaryButton:hover {
                background-color: #3f3f46;
                color: #ffffff;
                border-color: #52525b;
            }
            QPushButton#secondaryButton:checked {
                background-color: #312e81;
                color: #e0e7ff;
                border-color: #6366f1;
            }
            QPushButton#dangerButton {
                background-color: #451a1a;
                color: #fca5a5;
                border: 1px solid #7f1d1d;
            }
            QPushButton#dangerButton:hover {
                background-color: #7f1d1d;
                border-color: #b91c1c;
            }
            QPushButton:disabled {
                background-color: #1c1c1e;
                color: #52525b;
                border: 1px solid #27272a;
            }
            QLabel#statusPill {
                color: #e4e4e7;
                background-color: #27272a;
                border: 1px solid #3f3f46;
            }
            QLabel#statusPill[state="active"] {
                color: #c7d2fe;
                background-color: #1e1b4b;
                border: 1px solid #3730a3;
            }
            QLabel#statusPill[state="success"] {
                color: #a7f3d0;
                background-color: #064e3b;
                border: 1px solid #047857;
            }
            QLabel#statusPill[state="warning"] {
                color: #fde68a;
                background-color: #78350f;
                border: 1px solid #b45309;
            }
            QLabel#statusPill[state="error"] {
                color: #fecaca;
                background-color: #7f1d1d;
                border: 1px solid #b91c1c;
            }
            QProgressBar {
                border: 1px solid #27272a;
                background: #09090b;
            }
            QProgressBar::chunk {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #8b5cf6, stop: 1 #3b82f6
                );
            }
            QPlainTextEdit#logBox {
                background-color: #09090b;
                border: 1px solid #27272a;
                color: #34d399;
            }
            QTableWidget {
                background-color: #18181b;
                border: 1px solid #27272a;
                gridline-color: #27272a;
                alternate-background-color: #121214;
                selection-background-color: #312e81;
                selection-color: #e0e7ff;
                color: #e4e4e7;
            }
            QHeaderView::section {
                background: #27272a;
                color: #e4e4e7;
                border-right: 1px solid #3f3f46;
                border-bottom: 1px solid #3f3f46;
            }
            
            /* Custom Scrollbars Dark Mode */
            QScrollBar:vertical {
                background: #09090b;
                width: 8px;
                border-radius: 4px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #27272a;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #3f3f46;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                background: none;
                height: 0px;
            }
            QScrollBar:horizontal {
                background: #09090b;
                height: 8px;
                border-radius: 4px;
                margin: 0px;
            }
            QScrollBar::handle:horizontal {
                background: #27272a;
                border-radius: 4px;
                min-width: 20px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #3f3f46;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                background: none;
                width: 0px;
            }

            /* Slider Dark Mode */
            QSlider::groove:horizontal {
                border: 1px solid #27272a;
                height: 6px;
                background: #09090b;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #8b5cf6;
                border: none;
                width: 14px;
                height: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
            QSlider::handle:horizontal:hover {
                background: #7c3aed;
            }
            """
            self.setStyleSheet(base_styles + dark_overrides)
        else:
            self.setStyleSheet(base_styles)

    def toggle_theme(self, checked: bool) -> None:
        self.dark_mode = checked
        self.theme_button.setText("☀️ Light Mode" if checked else "🌙 Dark Mode")
        self.apply_styles()

    def update_concurrency_value(self, val: int) -> None:
        self.max_concurrent = val
        self.concurrency_label.setText(str(val))

    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.showNormal()
            self.activateWindow()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasText() or event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        urls_to_add = []
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                local_path = url.toLocalFile()
                if local_path and Path(local_path).suffix == ".txt":
                    try:
                        with open(local_path, "r", encoding="utf-8") as f:
                            urls_to_add.extend([line.strip() for line in f if line.strip()])
                    except Exception as e:
                        self.log_box.appendPlainText(f"Error reading dropped file: {e}")
                else:
                    urls_to_add.append(url.toString())
        elif event.mimeData().hasText():
            text = event.mimeData().text().strip()
            urls_to_add.extend([line.strip() for line in text.splitlines() if line.strip()])

        added = 0
        quality = self.quality_combo.currentText()
        for u in urls_to_add:
            if u.startswith("http://") or u.startswith("https://") or "www." in u:
                self._append_queue_row(u, quality)
                added += 1

        if added > 0:
            self.log_box.appendPlainText(f"Dropped and queued {added} item(s).")
            self.set_status(f"Queued {added} dropped link(s).", "idle")
            self._update_queue_buttons()
            event.acceptProposedAction()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            clipboard = QApplication.clipboard()
            text = clipboard.text().strip()
            platforms = ["youtube.com", "youtu.be", "tiktok.com", "instagram.com", "twitter.com", "x.com"]
            if any(p in text.lower() for p in platforms):
                if not self.url_input.text().strip():
                    self.url_input.setText(text)
                    self.set_status("Detected video link in clipboard!", "idle")
        elif event.type() == QEvent.WindowStateChange:
            if self.isMinimized():
                self.hide()
                self.tray_icon.showMessage(
                    "Universal Video Downloader",
                    "Application minimized to system tray.",
                    QSystemTrayIcon.Information,
                    2000
                )
                event.accept()
        super().changeEvent(event)

    def paste_clipboard(self) -> None:
        clipboard = QApplication.clipboard()
        text = clipboard.text().strip()
        if text:
            self.url_input.setText(text)
            self.set_status("Clipboard URL pasted.", "idle")
        else:
            self.set_status("Clipboard is empty.", "warning")

    def analyze_url(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Missing URL", "Please enter a URL to analyze.")
            return

        self.set_status("Analyzing formats...", "active")
        self.log_box.appendPlainText(f"Analyzing formats for {url}...")
        self.analyze_button.setEnabled(False)
        self.url_input.setEnabled(False)

        cookie_file = self.cookies_input.text().strip()
        
        self.analyzer_thread = QThread()
        self.analyzer_worker = FormatAnalyzerWorker(url, cookie_file)
        self.analyzer_worker.moveToThread(self.analyzer_thread)

        self.analyzer_thread.started.connect(self.analyzer_worker.run)
        self.analyzer_worker.finished.connect(self.on_analyzer_finished)
        self.analyzer_worker.finished.connect(self.analyzer_thread.quit)
        self.analyzer_worker.finished.connect(self.analyzer_worker.deleteLater)
        self.analyzer_thread.finished.connect(self.analyzer_thread.deleteLater)
        self.analyzer_thread.finished.connect(self._clear_analyzer_references)

        self.analyzer_thread.start()

    def _clear_analyzer_references(self) -> None:
        self.analyzer_thread = None
        self.analyzer_worker = None

    def on_analyzer_finished(self, success: bool, formats: list, error_msg: str) -> None:
        self.analyze_button.setEnabled(True)
        self.url_input.setEnabled(True)

        if not success:
            self.set_status("Analysis failed.", "error")
            self.log_box.appendPlainText(f"Format analysis failed: {error_msg}")
            QMessageBox.critical(self, "Analysis Failed", f"Failed to extract formats:\n{error_msg}")
            return

        self.quality_combo.clear()
        for fmt in formats:
            self.quality_combo.addItem(fmt["name"], fmt)

        self.set_status("Format analysis complete.", "success")
        self.log_box.appendPlainText(f"Found {len(formats)} format option(s).")

    def set_status(self, message: str, state: str) -> None:
        if self.status_label.text() != message:
            self.status_label.setText(message)
        if self.status_label.property("state") != state:
            self.status_label.setProperty("state", state)
            self.style().unpolish(self.status_label)
            self.style().polish(self.status_label)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._intro_played:
            return
        self._intro_played = True
        self.setWindowOpacity(0.0)
        self._intro_animation = QPropertyAnimation(self, b"windowOpacity")
        self._intro_animation.setDuration(450)
        self._intro_animation.setStartValue(0.0)
        self._intro_animation.setEndValue(1.0)
        self._intro_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._intro_animation.start()

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self.output_input.setText(folder)

    def choose_cookies(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select cookies.txt",
            "",
            "Text files (*.txt);;All files (*.*)",
        )
        if file_path:
            self.cookies_input.setText(file_path)

    def enqueue_urls(self) -> None:
        raw_text = self.url_input.text().strip()
        if not raw_text:
            QMessageBox.warning(self, "Missing URL", "Please enter at least one video URL.")
            return

        quality = self.quality_combo.currentText()
        urls = [line.strip() for line in raw_text.splitlines() if line.strip()]

        for url in urls:
            self._append_queue_row(url, quality)

        self.url_input.clear()
        self.log_box.appendPlainText(f"Queued {len(urls)} item(s) with quality: {quality}")
        self.set_status(f"Added {len(urls)} item(s) to queue.", "idle")
        self._update_queue_buttons()

    def _append_queue_row(self, url: str, quality: str) -> None:
        row = self.queue_table.rowCount()
        self.queue_table.insertRow(row)
        self._set_row_text(row, self.COL_URL, url)
        quality_combo = self._build_row_quality_combo(quality)
        self.queue_table.setCellWidget(row, self.COL_QUALITY, quality_combo)
        self._set_row_text(row, self.COL_STATUS, "Queued")
        self._set_row_text(row, self.COL_PROGRESS, "0%")

    def _build_row_quality_combo(self, quality: str) -> QComboBox:
        combo = QComboBox()
        # If the main quality combo has custom items (with dict user data), copy them!
        if self.quality_combo.itemData(0) is not None:
            for idx in range(self.quality_combo.count()):
                text = self.quality_combo.itemText(idx)
                data = self.quality_combo.itemData(idx)
                combo.addItem(text, data)
            combo.setCurrentText(self.quality_combo.currentText())
        else:
            for name in QUALITY_PRESETS.keys():
                combo.addItem(name)
            combo.setCurrentText(quality if quality in QUALITY_PRESETS else "Best (Video + Audio)")
        
        combo.setProperty("tableQuality", True)
        return combo

    def _quality_for_row(self, row: int):
        widget = self.queue_table.cellWidget(row, self.COL_QUALITY)
        if isinstance(widget, QComboBox):
            idx = widget.currentIndex()
            data = widget.itemData(idx)
            if data:
                return data
            return widget.currentText()

        quality_item = self.queue_table.item(row, self.COL_QUALITY)
        if quality_item and quality_item.text() in QUALITY_PRESETS:
            return quality_item.text()
        return "Best (Video + Audio)"

    def _set_quality_editable(self, enabled: bool) -> None:
        self.quality_combo.setEnabled(enabled)
        for row in range(self.queue_table.rowCount()):
            widget = self.queue_table.cellWidget(row, self.COL_QUALITY)
            if isinstance(widget, QComboBox):
                widget.setEnabled(enabled)

    def _set_row_text(self, row: int, column: int, text: str) -> None:
        item = self.queue_table.item(row, column)
        if item is None:
            item = QTableWidgetItem(text)
            self.queue_table.setItem(row, column, item)
        else:
            item.setText(text)

        if column == self.COL_URL:
            item.setToolTip(text)

        if column in (self.COL_QUALITY, self.COL_STATUS, self.COL_PROGRESS):
            item.setTextAlignment(Qt.AlignCenter)

    def _set_row_status(self, row: int, status: str) -> None:
        self._set_row_text(row, self.COL_STATUS, status)

    def _set_row_progress(self, row: int, text: str) -> None:
        self._set_row_text(row, self.COL_PROGRESS, text)

    def _next_queued_row(self) -> int | None:
        for row in range(self.queue_table.rowCount()):
            item = self.queue_table.item(row, self.COL_STATUS)
            if item and item.text() == "Queued":
                # Ensure it is not currently downloading in active pool
                if row not in self.active_downloads:
                    return row
        return None

    def start_queue(self) -> None:
        if self.queue_running:
            return

        if not self.output_input.text().strip():
            QMessageBox.warning(self, "Missing Folder", "Please select an output folder.")
            return

        if self._next_queued_row() is None:
            QMessageBox.information(self, "Queue Empty", "Add URLs to the queue first.")
            return

        self.log_box.appendPlainText("Starting queue...")
        self.queue_running = True
        self.stop_queue_requested = False
        self.progress_bar.setValue(0)
        self.speed_chart.clear_history()
        self.active_speeds.clear()
        self._set_quality_editable(False)
        self._update_queue_buttons()
        self._start_next_items()

    def _start_next_items(self) -> None:
        if self.stop_queue_requested:
            if not self.active_downloads:
                self._finish_queue("Queue stopped.", "warning")
            return

        while len(self.active_downloads) < self.max_concurrent:
            next_row = self._next_queued_row()
            if next_row is None:
                break
            self._start_download_item(next_row)

        if not self.active_downloads and self._next_queued_row() is None:
            self._finish_queue("Queue completed.", "success")
            self.tray_icon.showMessage(
                "Universal Video Downloader",
                "All downloads in the queue have completed!",
                QSystemTrayIcon.Information,
                3000
            )

    def _start_download_item(self, row: int) -> None:
        output_dir = self.output_input.text().strip()
        cookie_file = self.cookies_input.text().strip()
        url_item = self.queue_table.item(row, self.COL_URL)
        if url_item is None:
            self._set_row_status(row, "Failed")
            self._set_row_progress(row, "0%")
            self.log_box.appendPlainText(f"[Item {row + 1}] Invalid queue entry.")
            QTimer.singleShot(50, self._start_next_items)
            return

        url = url_item.text()
        quality = self._quality_for_row(row)

        self._set_row_status(row, "Downloading")
        self._set_row_progress(row, "0%")
        self._update_overall_progress()

        thread = QThread()
        worker = DownloadWorker(row, url, output_dir, cookie_file, quality)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        
        worker.progress.connect(self.on_item_progress)
        worker.speed_updated.connect(self.on_item_speed_updated)
        worker.log.connect(self.on_item_log)
        worker.finished.connect(self.on_item_finished)
        
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self.on_thread_finished)

        self.active_downloads[row] = {"thread": thread, "worker": worker}
        self.cancel_button.setEnabled(True)
        thread.start()

    def _finish_queue(self, message: str, state: str) -> None:
        self.queue_running = False
        self.stop_queue_requested = False
        self.active_downloads.clear()
        self.active_speeds.clear()
        self.cancel_button.setEnabled(False)
        self.progress_bar.setValue(100 if state == "success" else 0)
        self.set_status(message, state)
        self.log_box.appendPlainText(message)
        self._set_quality_editable(True)
        self._update_queue_buttons()

    def cancel_download(self) -> None:
        self.stop_queue_requested = True
        self.set_status("Stopping queue...", "warning")
        self.log_box.appendPlainText("Cancel requested...")
        
        for info in list(self.active_downloads.values()):
            worker = info.get("worker")
            if worker:
                worker.cancel()
        
        self.cancel_button.setEnabled(False)

    def on_item_progress(self, row: int, percent: float, message: str) -> None:
        self._set_row_progress(row, f"{percent:.1f}%")
        self.set_status(f"Downloading {len(self.active_downloads)} item(s) concurrently...", "active")
        self._update_overall_progress()

    def on_item_speed_updated(self, row: int, speed: float) -> None:
        self.active_speeds[row] = speed
        total_speed = sum(self.active_speeds.values())
        self.speed_chart.add_sample(total_speed)

    def on_item_log(self, row: int, message: str) -> None:
        self.log_box.appendPlainText(f"[Item {row + 1}] {message}")

    def on_item_finished(self, row: int, success: bool, message: str) -> None:
        lowered = message.lower()
        if success:
            row_status = "Done"
            state = "success"
            progress_text = "100%"
        elif "cancel" in lowered:
            row_status = "Canceled"
            state = "warning"
            current_progress = self.queue_table.item(row, self.COL_PROGRESS)
            progress_text = current_progress.text() if current_progress else "0%"
        else:
            row_status = "Failed"
            state = "error"
            progress_text = "0%"

        self._set_row_status(row, row_status)
        self._set_row_progress(row, progress_text)
        self.log_box.appendPlainText(f"[Item {row + 1}] Finished: {message}")
        self._update_queue_buttons()
        self._update_overall_progress()

    def on_thread_finished(self) -> None:
        sender_thread = self.sender()
        row = None
        for r, info in self.active_downloads.items():
            if info.get("thread") == sender_thread:
                row = r
                break

        if row is not None:
            if row in self.active_downloads:
                del self.active_downloads[row]
            if row in self.active_speeds:
                del self.active_speeds[row]

        if not self.active_downloads:
            self.cancel_button.setEnabled(False)

        if self.queue_running:
            self._start_next_items()

    def _update_overall_progress(self) -> None:
        total_rows = self.queue_table.rowCount()
        if total_rows == 0:
            self.progress_bar.setValue(0)
            return
            
        total_pct = 0.0
        for r in range(total_rows):
            status_item = self.queue_table.item(r, self.COL_STATUS)
            if status_item:
                status = status_item.text()
                if status == "Done":
                    total_pct += 100.0
                elif status in ("Downloading", "Queued", "Canceled", "Failed"):
                    prog_item = self.queue_table.item(r, self.COL_PROGRESS)
                    if prog_item:
                        try:
                            pct_str = prog_item.text().replace("%", "")
                            total_pct += float(pct_str)
                        except ValueError:
                            pass
        avg_pct = total_pct / total_rows
        self.progress_bar.setValue(int(avg_pct))

    def remove_selected_items(self) -> None:
        if self.queue_running:
            QMessageBox.information(self, "Queue Running", "Stop the queue before removing items.")
            return

        selected_rows = sorted({index.row() for index in self.queue_table.selectedIndexes()}, reverse=True)
        if not selected_rows:
            return

        for row in selected_rows:
            self.queue_table.removeRow(row)

        self.set_status(f"Removed {len(selected_rows)} item(s).", "idle")
        self._update_queue_buttons()
        self._update_overall_progress()

    def clear_finished_items(self) -> None:
        if self.queue_running:
            QMessageBox.information(self, "Queue Running", "Stop the queue before clearing items.")
            return

        removable = []
        for row in range(self.queue_table.rowCount()):
            status_item = self.queue_table.item(row, self.COL_STATUS)
            if status_item and status_item.text() in {"Done", "Failed", "Canceled"}:
                removable.append(row)

        for row in reversed(removable):
            self.queue_table.removeRow(row)

        if removable:
            self.set_status(f"Cleared {len(removable)} finished item(s).", "idle")

        self._update_queue_buttons()
        self._update_overall_progress()

    def _update_queue_buttons(self) -> None:
        has_rows = self.queue_table.rowCount() > 0
        has_queued = self._next_queued_row() is not None
        has_selection = bool(self.queue_table.selectedIndexes())

        self.start_button.setEnabled(has_queued and not self.queue_running)
        self.cancel_button.setEnabled(self.queue_running and len(self.active_downloads) > 0)
        self.remove_button.setEnabled(has_selection and not self.queue_running)
        self.clear_finished_button.setEnabled(has_rows and not self.queue_running)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.stop_queue_requested = True
        
        for info in list(self.active_downloads.values()):
            worker = info.get("worker")
            if worker:
                worker.cancel()
        
        for info in list(self.active_downloads.values()):
            thread = info.get("thread")
            if thread and thread.isRunning():
                thread.quit()
                thread.wait(1000)
                
        if self.analyzer_thread and self.analyzer_thread.isRunning():
            self.analyzer_thread.quit()
            self.analyzer_thread.wait(1000)
            
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
