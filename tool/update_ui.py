"""Non-blocking update prompts, kept separate from project generation jobs."""
from pathlib import Path
from queue import Empty, Queue
import os
import sys
from threading import Thread

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout,
)

from app_update import UpdateError, check_release, download_release, launch_helper, load_config, cleanup_updates_after_startup


class UpdateDialog(QDialog):
    """Compact confirmation card using the desktop app's existing styles."""
    def __init__(self, parent, title, text, action, *, notes="", ready=False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(480)
        self.setStyleSheet(parent.centralWidget().styleSheet() + """
            QFrame#UpdateCard { background: white; border: 1px solid #DCE5F0; border-radius: 24px; }
            QLabel#UpdateTitle { font-size: 23px; font-weight: 600; color: #1D2747; }
            QLabel#UpdateIcon { background: #E2F3FF; border-radius: 14px; }
            QLabel#UpdateNotes { background: #F7FAFD; padding: 12px; border-radius: 12px; color: #63718A; }
            QPushButton[variant="primary"]:focus { border: 2px solid #39A9EA; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("UpdateCard")
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(16)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setObjectName("UpdateIcon")
        icon.setFixedSize(48, 48)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(26, 26)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#39A9EA'), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if ready:
            painter.drawLine(5, 13, 11, 19)
            painter.drawLine(11, 19, 22, 7)
        else:
            painter.drawLine(13, 3, 13, 17)
            painter.drawLine(7, 11, 13, 17)
            painter.drawLine(19, 11, 13, 17)
            painter.drawLine(4, 18, 4, 23)
            painter.drawLine(4, 23, 22, 23)
            painter.drawLine(22, 23, 22, 18)
        painter.end()
        icon.setPixmap(pixmap)
        header.addWidget(icon)
        header.addStretch()
        close = QPushButton("×")
        close.setProperty("variant", "icon")
        close.setAccessibleName("Đóng thông báo cập nhật")
        close.setToolTip("Để sau")
        close.setAutoDefault(False)
        close.clicked.connect(self.reject)
        header.addWidget(close, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        heading = QLabel(title)
        heading.setObjectName("UpdateTitle")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        body = QLabel(text)
        body.setProperty("role", "body")
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        layout.addWidget(body)
        if notes.strip():
            note_label = QLabel(notes.strip())
            note_label.setObjectName("UpdateNotes")
            note_label.setTextFormat(Qt.TextFormat.PlainText)
            note_label.setWordWrap(True)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(note_label)
            scroll.setFixedHeight(min(136, max(64, note_label.heightForWidth(400) + 8)))
            layout.addWidget(scroll)
        layout.addSpacing(4)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()
        self.later_button = QPushButton("Để sau")
        self.later_button.setMinimumHeight(40)
        self.later_button.clicked.connect(self.reject)
        self.accept_button = QPushButton(action)
        self.accept_button.setProperty("variant", "primary")
        self.accept_button.setAutoDefault(False)
        self.accept_button.clicked.connect(self.accept)
        actions.addWidget(self.later_button)
        actions.addWidget(self.accept_button)
        layout.addLayout(actions)
        self.later_button.setDefault(True)
        self.later_button.setFocus()


class UpdateController(QObject):
    def __init__(self, window, button):
        super().__init__(window)
        self.window = window
        self.button = button
        self.config = load_config()
        self.events = Queue()
        self.running = False
        self.installing = False
        self.pending = None
        self.manual = False
        self.job_path = None
        button.clicked.connect(lambda: self.check(True))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        QTimer.singleShot(0, lambda: self.check(False))
        Thread(target=cleanup_updates_after_startup, daemon=True).start()

    def background(self, kind, function):
        self.running = True
        self.button.setEnabled(False)

        def work():
            try:
                result = function()
            except Exception as exc:
                self.events.put(("error", exc))
            else:
                self.events.put((kind, result))
        Thread(target=work, daemon=True).start()

    def message(self, title, text):
        box = QMessageBox(self.window)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(text)
        box.addButton("Đóng", QMessageBox.ButtonRole.AcceptRole)
        box.exec()

    def ask(self, title, text, action, **options):
        box = UpdateDialog(self.window, title, text, action, **options)
        accepted = box.exec() == QDialog.DialogCode.Accepted
        box.deleteLater()
        return accepted

    def check(self, manual=False):
        if self.running or self.installing:
            return
        self.manual = manual
        if self.job_path:
            self.pending = ("download", self.job_path)
            return
        if not self.config.get("manifest_url"):
            if manual:
                self.message("Cập nhật ứng dụng", "Bản này chưa có địa chỉ cập nhật. Hãy liên hệ người cài app.")
            return
        if os.name != "nt" or not getattr(sys, "frozen", False):
            if manual:
                self.message("Cập nhật ứng dụng", "Tự cập nhật chỉ dùng được trong bản .exe trên Windows.")
            return
        self.button.setText("Đang kiểm tra...")
        self.background("check", lambda: check_release(self.config["manifest_url"], self.config["version"]))

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    self.button.setText(f"Đang tải {value}%")
                    continue
                self.running = False
                self.button.setEnabled(True)
                self.button.setText("Kiểm tra cập nhật")
                self.pending = (kind, value)
        except Empty:
            pass
        if not self.pending or self.window.busy_kind or self.window.close_when_idle:
            return
        kind, value = self.pending
        self.pending = None
        if kind == "error":
            text = str(value) if isinstance(value, UpdateError) else "Chưa kết nối hoặc tải được bản cập nhật. Hãy kiểm tra mạng rồi thử lại."
            self.window._log(text, "warning")
            if self.manual:
                self.message("Chưa cập nhật được", text)
        elif kind == "check":
            if value is None:
                if self.manual:
                    self.message("Cập nhật ứng dụng", "Bạn đang dùng phiên bản mới nhất.")
            elif self.ask("Có bản mới", f"Phiên bản {value['version']} đã sẵn sàng.", "Tải bản mới", notes=value.get('notes', '')):
                self.manual = True
                self.button.setText("Đang tải...")
                self.background("download", lambda: download_release(
                    value, Path(sys.executable),
                    lambda percent: self.events.put(("progress", percent)),
                ))
        elif kind == "download":
            self.job_path = value
            self.button.setText("Cài bản đã tải")
            if self.ask("Đã tải xong", "App sẽ mở lại để cập nhật.\nFile vẫn được giữ, bạn cần chọn lại dự án.", "Cập nhật ngay", ready=True):
                try:
                    launch_helper(value)
                except Exception as exc:
                    self.message("Chưa cập nhật được", str(exc))
                else:
                    self.installing = True
                    self.window.close()
