import os
import re
import sys
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, QThread, Qt, Signal
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
    progress = Signal(float, str)
    log = Signal(str)
    finished = Signal(bool, str)

    def __init__(
        self,
        url: str,
        output_dir: str,
        cookie_file: str = "",
        quality_preset: str = "Best (Video + Audio)",
    ):
        super().__init__()
        self.url = url
        self.output_dir = output_dir
        self.cookie_file = cookie_file
        self.quality_preset = quality_preset
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _progress_hook(self, data: dict) -> None:
        if self._cancelled:
            raise DownloadError("Download canceled by user.")

        status = data.get("status", "")
        if status == "downloading":
            downloaded = data.get("downloaded_bytes", 0)
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            if total > 0:
                percent = (downloaded / total) * 100
                speed = data.get("speed")
                eta = data.get("eta")
                speed_text = (
                    f"{speed / 1024 / 1024:.2f} MB/s" if isinstance(speed, (int, float)) else "N/A"
                )
                eta_text = f"{eta}s" if isinstance(eta, int) else "N/A"
                self.progress.emit(percent, f"{percent:.1f}% | {speed_text} | ETA: {eta_text}")
            else:
                self.progress.emit(0, "Downloading...")
        elif status == "finished":
            self.progress.emit(100, "Download complete, processing file...")

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

            preset = QUALITY_PRESETS.get(
                self.quality_preset, QUALITY_PRESETS["Best (Video + Audio)"]
            )
            ydl_opts["format"] = preset["format"]
            if "merge_output_format" in preset:
                ydl_opts["merge_output_format"] = preset["merge_output_format"]
            if "postprocessors" in preset:
                ydl_opts["postprocessors"] = list(preset["postprocessors"])

            self.log.emit(f"Quality: {self.quality_preset}")
            if self.cookie_file and Path(self.cookie_file).exists():
                ydl_opts["cookiefile"] = self.cookie_file
                self.log.emit("Using cookies file for authenticated download.")

            self.log.emit("Fetching video info...")
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
                title = sanitize_filename(info.get("title", "video"))
                self.log.emit(f"Title: {title}")
                self.log.emit("Starting download...")
                ydl.download([self.url])

            if self._cancelled:
                self.finished.emit(False, "Download canceled.")
                return

            self.finished.emit(True, "Download completed successfully.")
        except DownloadError as exc:
            if self._cancelled:
                self.finished.emit(False, "Download canceled.")
            else:
                self.finished.emit(False, f"Download failed: {exc}")
        except Exception as exc:
            self.finished.emit(False, f"Error: {exc}")


class MainWindow(QMainWindow):
    COL_URL = 0
    COL_QUALITY = 1
    COL_STATUS = 2
    COL_PROGRESS = 3

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Universal Video Downloader")
        self.resize(980, 720)

        self.thread: QThread | None = None
        self.worker: DownloadWorker | None = None
        self.current_row: int | None = None
        self.queue_running = False
        self.stop_queue_requested = False
        self.dark_mode = False
        self._intro_animation: QPropertyAnimation | None = None
        self._intro_played = False

        self.url_input = QLineEdit()
        self.url_input.setObjectName("urlInput")
        self.url_input.setClearButtonEnabled(True)
        self.url_input.setPlaceholderText(
            "Paste TikTok / YouTube / Instagram / Twitter(X) video URL..."
        )

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

        self.theme_button = QPushButton("Dark Theme")
        self.theme_button.setObjectName("secondaryButton")
        self.theme_button.setCheckable(True)
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
        header.setSectionResizeMode(self.COL_URL, QHeaderView.Interactive)
        header.setSectionResizeMode(self.COL_QUALITY, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_PROGRESS, QHeaderView.ResizeToContents)
        self.queue_table.setColumnWidth(self.COL_URL, 540)
        self.queue_table.itemSelectionChanged.connect(self._update_queue_buttons)

        central = QWidget(objectName="root")
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(26, 24, 26, 24)
        main_layout.setSpacing(14)

        header_card = QFrame(objectName="headerCard")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(20, 18, 20, 18)
        header_layout.setSpacing(8)

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
        input_layout.addWidget(QLabel("Video URL", objectName="fieldLabel"))
        input_layout.addWidget(self.url_input)
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

        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.addWidget(self.enqueue_button, 1)
        button_layout.addWidget(self.start_button, 1)
        button_layout.addWidget(self.cancel_button, 1)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        status_row.addWidget(self.status_label, 0)
        status_row.addWidget(self.progress_bar, 1)

        controls_layout.addLayout(button_layout)
        controls_layout.addLayout(status_row)

        log_card = QFrame(objectName="card")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(16, 14, 16, 16)
        log_layout.setSpacing(8)
        log_layout.addWidget(QLabel("Activity Log", objectName="fieldLabel"))
        log_layout.addWidget(self.log_box)
        self.log_box.setMinimumHeight(220)

        main_layout.addWidget(header_card)
        main_layout.addWidget(input_card)
        main_layout.addWidget(queue_card, 1)
        main_layout.addWidget(controls_card)
        main_layout.addWidget(log_card)

        self.apply_styles()
        self.setCentralWidget(central)
        self._update_queue_buttons()

    def _make_chip(self, text: str) -> QLabel:
        chip = QLabel(text)
        chip.setObjectName("platformChip")
        chip.setAlignment(Qt.AlignCenter)
        return chip

    def apply_styles(self) -> None:
        base_styles = """
            QWidget#root {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #f4f9ff,
                    stop: 0.55 #fbfcfe,
                    stop: 1 #fff7ed
                );
            }
            QFrame#headerCard, QFrame#card {
                background-color: rgba(255, 255, 255, 230);
                border: 1px solid #d9e3f0;
                border-radius: 16px;
            }
            QLabel {
                color: #233042;
                font-family: "Trebuchet MS";
            }
            QLabel#titleLabel {
                font-family: "Bahnschrift SemiBold";
                color: #18283b;
                font-size: 28px;
                letter-spacing: 0.4px;
            }
            QLabel#subtitleLabel {
                color: #4f637c;
                font-size: 14px;
            }
            QLabel#fieldLabel {
                color: #344861;
                font-size: 12px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.8px;
            }
            QLabel#platformChip {
                border: 1px solid #bfd2ea;
                background-color: #eff6ff;
                border-radius: 11px;
                padding: 3px 10px;
                color: #214a7a;
                font-size: 11px;
                font-weight: 600;
            }
            QLineEdit {
                background: #ffffff;
                border: 1px solid #c4d4e9;
                border-radius: 10px;
                padding: 10px 12px;
                color: #1f2f45;
                font-size: 13px;
                selection-background-color: #5ca7ff;
            }
            QLineEdit:focus {
                border: 2px solid #4c8fe3;
            }
            QComboBox {
                background: #ffffff;
                border: 1px solid #c4d4e9;
                border-radius: 10px;
                padding: 9px 12px;
                color: #1f2f45;
                font-size: 13px;
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
                border-top: 7px solid #4f6f94;
                margin-right: 8px;
            }
            QComboBox QAbstractItemView {
                border: 1px solid #c4d4e9;
                background: #ffffff;
                color: #1f2f45;
                selection-background-color: #d9ecff;
                selection-color: #1f2f45;
                outline: 0;
            }
            QComboBox QAbstractItemView::item {
                background: #ffffff;
                color: #1f2f45;
                min-height: 22px;
                padding: 5px 8px;
            }
            QComboBox QAbstractItemView::item:selected {
                background: #d9ecff;
                color: #1f2f45;
            }
            QPushButton {
                border-radius: 10px;
                padding: 9px 14px;
                font-family: "Trebuchet MS";
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#primaryButton {
                background-color: #1877f2;
                color: #ffffff;
                border: 1px solid #156bda;
            }
            QPushButton#primaryButton:hover {
                background-color: #1168d9;
            }
            QPushButton#secondaryButton {
                background-color: #ffffff;
                color: #27507f;
                border: 1px solid #b9cce5;
            }
            QPushButton#secondaryButton:hover {
                background-color: #edf4ff;
            }
            QPushButton#dangerButton {
                background-color: #fff4f2;
                color: #b53a2f;
                border: 1px solid #f1c3be;
            }
            QPushButton#dangerButton:hover {
                background-color: #ffe7e2;
            }
            QPushButton:disabled {
                background-color: #eef2f7;
                color: #8ea1b9;
                border: 1px solid #dde4ed;
            }
            QLabel#statusPill {
                min-width: 170px;
                border-radius: 13px;
                padding: 6px 10px;
                font-size: 12px;
                font-weight: 700;
                color: #1f3f66;
                background-color: #e6f1ff;
                border: 1px solid #b8d0ec;
            }
            QLabel#statusPill[state="active"] {
                color: #1f3f66;
                background-color: #dff0ff;
                border: 1px solid #9fc7ee;
            }
            QLabel#statusPill[state="success"] {
                color: #1f6a3b;
                background-color: #e8f9ee;
                border: 1px solid #a6ddb6;
            }
            QLabel#statusPill[state="warning"] {
                color: #8a5a12;
                background-color: #fff5e5;
                border: 1px solid #f5d49b;
            }
            QLabel#statusPill[state="error"] {
                color: #8f2720;
                background-color: #ffe8e5;
                border: 1px solid #efc0bb;
            }
            QProgressBar {
                border: 1px solid #c7d7eb;
                border-radius: 10px;
                height: 22px;
                background: #edf3fa;
            }
            QProgressBar::chunk {
                border-radius: 9px;
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #35a6ff, stop: 1 #1570ef
                );
            }
            QPlainTextEdit#logBox {
                background-color: #f9fbff;
                border: 1px solid #d2deee;
                border-radius: 10px;
                padding: 8px;
                color: #233042;
                font-family: "Consolas";
                font-size: 12px;
            }
            QTableWidget {
                background-color: #ffffff;
                border: 1px solid #d2deee;
                border-radius: 10px;
                gridline-color: #e2eaf5;
                alternate-background-color: #f8fbff;
                selection-background-color: #d9ecff;
                selection-color: #1f2f45;
                color: #233042;
                font-size: 12px;
            }
            QHeaderView::section {
                background: #eef4fc;
                color: #365070;
                border: none;
                border-right: 1px solid #d7e2f0;
                border-bottom: 1px solid #d7e2f0;
                padding: 8px;
                font-weight: 700;
            }
            QTableWidget QScrollBar:vertical {
                background: #eef4fc;
                width: 12px;
                border-radius: 6px;
                margin: 4px;
            }
            QTableWidget QScrollBar::handle:vertical {
                background: #b8cbe4;
                border-radius: 6px;
                min-height: 24px;
            }
            QTableWidget QScrollBar:horizontal {
                background: #eef4fc;
                height: 12px;
                border-radius: 6px;
                margin: 4px;
            }
            QTableWidget QScrollBar::handle:horizontal {
                background: #b8cbe4;
                border-radius: 6px;
                min-width: 24px;
            }
            QTableWidget QScrollBar::add-line,
            QTableWidget QScrollBar::sub-line {
                width: 0px;
                height: 0px;
                border: none;
                background: transparent;
            }
            """
        if self.dark_mode:
            dark_overrides = """
            QWidget#root {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #131a25,
                    stop: 0.55 #161f2d,
                    stop: 1 #1c2434
                );
            }
            QFrame#headerCard, QFrame#card {
                background-color: rgba(25, 34, 49, 232);
                border: 1px solid #33445f;
            }
            QLabel {
                color: #d4e3f8;
            }
            QLabel#titleLabel {
                color: #ecf3ff;
            }
            QLabel#subtitleLabel {
                color: #9eb3d3;
            }
            QLabel#fieldLabel {
                color: #95abcf;
            }
            QLabel#platformChip {
                border: 1px solid #3e5580;
                background-color: #22314b;
                color: #c5d9f7;
            }
            QLineEdit {
                background: #1a2537;
                border: 1px solid #3a4d70;
                color: #d7e5fb;
                selection-background-color: #2f71c8;
            }
            QLineEdit:focus {
                border: 2px solid #4b8be1;
            }
            QComboBox {
                background: #1a2537;
                border: 1px solid #3a4d70;
                color: #d7e5fb;
            }
            QComboBox::down-arrow {
                border-top: 7px solid #9ab7de;
            }
            QComboBox QAbstractItemView {
                border: 1px solid #3a4d70;
                background: #1a2537;
                color: #d7e5fb;
                selection-background-color: #2b4872;
                selection-color: #eaf3ff;
            }
            QComboBox QAbstractItemView::item {
                background: #1a2537;
                color: #d7e5fb;
            }
            QComboBox QAbstractItemView::item:selected {
                background: #2b4872;
                color: #eaf3ff;
            }
            QPushButton#primaryButton {
                background-color: #2f6fd8;
                color: #edf4ff;
                border: 1px solid #245ebd;
            }
            QPushButton#primaryButton:hover {
                background-color: #2866cb;
            }
            QPushButton#secondaryButton {
                background-color: #1f2a3d;
                color: #c6daf8;
                border: 1px solid #3d5479;
            }
            QPushButton#secondaryButton:hover {
                background-color: #24334a;
            }
            QPushButton#secondaryButton:checked {
                background-color: #2a3f63;
                color: #e7f0ff;
                border: 1px solid #5781c2;
            }
            QPushButton#dangerButton {
                background-color: #3a2227;
                color: #ffb8b2;
                border: 1px solid #774047;
            }
            QPushButton#dangerButton:hover {
                background-color: #46282e;
            }
            QPushButton:disabled {
                background-color: #1a2434;
                color: #70839f;
                border: 1px solid #2f3f57;
            }
            QLabel#statusPill {
                color: #cde1ff;
                background-color: #1f3553;
                border: 1px solid #42638d;
            }
            QLabel#statusPill[state="active"] {
                color: #cde1ff;
                background-color: #22405f;
                border: 1px solid #4d78a9;
            }
            QLabel#statusPill[state="success"] {
                color: #c8f4d8;
                background-color: #1e4733;
                border: 1px solid #3e7a5e;
            }
            QLabel#statusPill[state="warning"] {
                color: #ffe0b4;
                background-color: #4d3a1f;
                border: 1px solid #816637;
            }
            QLabel#statusPill[state="error"] {
                color: #ffc8c2;
                background-color: #54272b;
                border: 1px solid #87454c;
            }
            QProgressBar {
                border: 1px solid #415474;
                background: #182234;
            }
            QProgressBar::chunk {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #3ea8ff, stop: 1 #2f73db
                );
            }
            QPlainTextEdit#logBox {
                background-color: #172132;
                border: 1px solid #374a68;
                color: #d7e5fb;
            }
            QTableWidget {
                background-color: #162131;
                border: 1px solid #374a68;
                gridline-color: #2e3f59;
                alternate-background-color: #1a2739;
                selection-background-color: #2b4872;
                selection-color: #eaf3ff;
                color: #d7e5fb;
            }
            QHeaderView::section {
                background: #25344b;
                color: #cde1ff;
                border-right: 1px solid #374a68;
                border-bottom: 1px solid #374a68;
            }
            QTableWidget QScrollBar:vertical {
                background: #1d2b40;
            }
            QTableWidget QScrollBar::handle:vertical {
                background: #4c658e;
            }
            QTableWidget QScrollBar:horizontal {
                background: #1d2b40;
            }
            QTableWidget QScrollBar::handle:horizontal {
                background: #4c658e;
            }
            """
            self.setStyleSheet(base_styles + dark_overrides)
        else:
            self.setStyleSheet(base_styles)

    def toggle_theme(self, checked: bool) -> None:
        self.dark_mode = checked
        self.theme_button.setText("Light Theme" if checked else "Dark Theme")
        self.apply_styles()

    def set_status(self, message: str, state: str) -> None:
        self.status_label.setText(message)
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
        combo.addItems(list(QUALITY_PRESETS.keys()))
        combo.setCurrentText(quality if quality in QUALITY_PRESETS else "Best (Video + Audio)")
        combo.setProperty("tableQuality", True)
        return combo

    def _quality_for_row(self, row: int) -> str:
        widget = self.queue_table.cellWidget(row, self.COL_QUALITY)
        if isinstance(widget, QComboBox):
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
        self._set_quality_editable(False)
        self._update_queue_buttons()
        self._start_next_item()

    def _start_next_item(self) -> None:
        if self.stop_queue_requested:
            self._finish_queue("Queue stopped.", "warning")
            return

        next_row = self._next_queued_row()
        if next_row is None:
            self._finish_queue("Queue completed.", "success")
            return

        output_dir = self.output_input.text().strip()
        cookie_file = self.cookies_input.text().strip()
        url_item = self.queue_table.item(next_row, self.COL_URL)
        if url_item is None:
            self._set_row_status(next_row, "Failed")
            self._set_row_progress(next_row, "0%")
            self.log_box.appendPlainText(f"[Item {next_row + 1}] Invalid queue entry.")
            self._start_next_item()
            return

        url = url_item.text()
        quality = self._quality_for_row(next_row)
        self.current_row = next_row

        self._set_row_status(next_row, "Downloading")
        self._set_row_progress(next_row, "0%")
        self.progress_bar.setValue(0)
        self.set_status(
            f"Downloading item {next_row + 1}/{self.queue_table.rowCount()} ({quality})",
            "active",
        )

        self.thread = QThread()
        self.worker = DownloadWorker(url, output_dir, cookie_file, quality)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.log.connect(self.on_log)
        self.worker.finished.connect(self.on_item_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.on_thread_finished)
        self.thread.finished.connect(self.thread.deleteLater)

        self.cancel_button.setEnabled(True)
        self.thread.start()

    def _finish_queue(self, message: str, state: str) -> None:
        self.queue_running = False
        self.stop_queue_requested = False
        self.current_row = None
        self.cancel_button.setEnabled(False)
        self.progress_bar.setValue(100 if state == "success" else 0)
        self.set_status(message, state)
        self.log_box.appendPlainText(message)
        self._set_quality_editable(True)
        self._update_queue_buttons()

    def cancel_download(self) -> None:
        if self.worker:
            self.stop_queue_requested = True
            self.worker.cancel()
            self.set_status("Cancelling current item...", "warning")
            self.log_box.appendPlainText("Cancel requested...")
            self.cancel_button.setEnabled(False)
        elif self.queue_running:
            self.stop_queue_requested = True
            self.set_status("Stopping queue...", "warning")

    def on_progress(self, percent: float, message: str) -> None:
        row = self.current_row
        self.progress_bar.setValue(max(0, min(100, int(percent))))
        if row is not None:
            self._set_row_progress(row, f"{percent:.1f}%")
            self.set_status(f"Item {row + 1}: {message}", "active")
        else:
            self.set_status(message, "active")

    def on_log(self, message: str) -> None:
        row = self.current_row
        prefix = f"[Item {row + 1}] " if row is not None else ""
        self.log_box.appendPlainText(f"{prefix}{message}")

    def on_item_finished(self, success: bool, message: str) -> None:
        row = self.current_row
        if row is None:
            self.log_box.appendPlainText(message)
            return

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
            self.stop_queue_requested = True
        else:
            row_status = "Failed"
            state = "error"
            progress_text = "0%"

        self._set_row_status(row, row_status)
        self._set_row_progress(row, progress_text)
        self.set_status(message, state)
        self.log_box.appendPlainText(f"[Item {row + 1}] {message}")
        self._update_queue_buttons()

    def on_thread_finished(self) -> None:
        self.worker = None
        self.thread = None
        self.current_row = None
        self.cancel_button.setEnabled(False)
        if self.queue_running:
            self._start_next_item()

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

    def _update_queue_buttons(self) -> None:
        has_rows = self.queue_table.rowCount() > 0
        has_queued = self._next_queued_row() is not None
        has_selection = bool(self.queue_table.selectedIndexes())

        self.start_button.setEnabled(has_queued and not self.queue_running)
        self.cancel_button.setEnabled(self.queue_running and self.worker is not None)
        self.remove_button.setEnabled(has_selection and not self.queue_running)
        self.clear_finished_button.setEnabled(has_rows and not self.queue_running)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.worker:
            self.stop_queue_requested = True
            self.worker.cancel()
        if self.thread and self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(2000)
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
