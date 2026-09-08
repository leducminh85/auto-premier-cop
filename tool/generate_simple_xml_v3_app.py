#!/usr/bin/env python3
"""PySide6 desktop interface for the Premiere XML generator."""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QUrl, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QColor,
    QCloseEvent,
    QDesktopServices,
    QFontDatabase,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from generate_simple_xml import (
    SimpleGenerationSummary,
    generate_simple_project_xml,
    tokens_from_transcript,
)
from generate_simple_xml_v3 import (
    ArticleComponent,
    DEFAULT_CONTENT_NAME,
    GeneratorError,
    discover_video_media,
    find_named_child,
    inspect_docx_components,
    resolve_default_project,
)
from generate_xml import AUDIO_EXTENSIONS, hidden_subprocess_options


@dataclass(frozen=True)
class Theme:
    background: str = "#EEF5FC"
    gradient_start: str = "#E7F3FA"
    gradient_end: str = "#ECEBFA"
    card: str = "#FFFFFF"
    text: str = "#1D2747"
    secondary: str = "#63718A"
    tertiary: str = "#8B98AC"
    accent: str = "#39A9EA"
    accent_soft: str = "#E2F3FF"
    navy: str = "#1E294D"
    navy_hover: str = "#2A3763"
    border: str = "#DCE5F0"
    success: str = "#247A5A"
    success_soft: str = "#E6F5EF"
    warning: str = "#956515"
    warning_soft: str = "#FFF4D8"
    error: str = "#BA4545"
    error_soft: str = "#FCEAEA"


THEME = Theme()
IS_MACOS = sys.platform == "darwin"
FILE_MANAGER = (
    "Finder"
    if IS_MACOS
    else "File Explorer"
    if os.name == "nt"
    else "trình quản lý tệp"
)


def apply_light_palette(application: QApplication) -> None:
    """Lock Qt and native window chrome to the app's light appearance."""
    application.styleHints().setColorScheme(Qt.ColorScheme.Light)
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: THEME.background,
        QPalette.ColorRole.WindowText: THEME.text,
        QPalette.ColorRole.Base: THEME.card,
        QPalette.ColorRole.AlternateBase: "#F7FAFD",
        QPalette.ColorRole.ToolTipBase: THEME.navy,
        QPalette.ColorRole.ToolTipText: "#FFFFFF",
        QPalette.ColorRole.Text: THEME.text,
        QPalette.ColorRole.Button: THEME.card,
        QPalette.ColorRole.ButtonText: THEME.text,
        QPalette.ColorRole.BrightText: "#FFFFFF",
        QPalette.ColorRole.Link: THEME.accent,
        QPalette.ColorRole.Highlight: THEME.accent,
        QPalette.ColorRole.HighlightedText: "#FFFFFF",
        QPalette.ColorRole.PlaceholderText: THEME.tertiary,
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.WindowText,
        QColor("#A9B4C4"),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor("#A9B4C4"),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor("#A9B4C4"),
    )
    application.setPalette(palette)


class Stage:
    WELCOME = "welcome"
    SETUP = "setup"
    REVIEW = "review"
    READY = "ready"
    SUCCESS = "success"


STEP_TITLES = (
    "Chọn project",
    "Chuẩn bị dữ liệu",
    "Kiểm tra cấu trúc",
    "Xuất XML",
)


def bundled_asset(name: str) -> Path | None:
    bundle_root = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if bundle_root:
        candidates.append(Path(bundle_root) / name)
    candidates.append(Path(__file__).resolve().parent / "assets" / name)
    return next((path for path in candidates if path.is_file()), None)


def normalized_path(path: Path | None) -> str:
    return unicodedata.normalize("NFC", os.path.normcase(str(path))) if path else ""


def component_is_ready(component: ArticleComponent) -> bool:
    if not component.present or component.text_block_count == 0:
        return False
    return component.key == "outro" or component.video_cut_count > 0


def audio_file_count(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(
        path.is_file()
        and not path.name.startswith(".")
        and path.suffix.casefold() in AUDIO_EXTENSIONS
        for path in directory.iterdir()
    )


def discover_audio_directory(project_dir: Path) -> Path:
    candidates = {
        path.parent.resolve()
        for path in project_dir.rglob("*")
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.casefold() in AUDIO_EXTENSIONS
        and "Adobe Premiere Pro Auto-Save" not in path.parts
    }
    if not candidates:
        raise GeneratorError("Không tự tìm thấy thư mục voice-over trong project")
    return min(
        candidates,
        key=lambda path: (
            -audio_file_count(path),
            len(path.relative_to(project_dir).parts),
            str(path).casefold(),
        ),
    )


def discover_project_sources(project_dir: Path) -> dict[str, object]:
    """Discover each source independently so incomplete projects remain editable."""
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise GeneratorError(f"Không tìm thấy thư mục project: {project_dir}")

    content = find_named_child(project_dir, DEFAULT_CONTENT_NAME)
    if content is None:
        docx_files = sorted(
            project_dir.glob("*.docx"), key=lambda path: path.name.casefold()
        )
        content = docx_files[0] if docx_files else None

    errors: dict[str, str] = {}
    video_dir: Path | None = None
    video_count = 0
    try:
        video_dir, videos = discover_video_media(project_dir, None)
        video_count = len(videos)
    except GeneratorError as exc:
        errors["video"] = str(exc)

    audio_dir: Path | None = None
    voiceover_count = 0
    try:
        audio_dir = discover_audio_directory(project_dir)
        voiceover_count = audio_file_count(audio_dir)
    except GeneratorError as exc:
        errors["audio"] = str(exc)

    if content is None:
        errors["content"] = "Không tìm thấy file DOCX trong project"

    return {
        "project": project_dir.resolve(),
        "content": content.resolve() if content else None,
        "video": video_dir,
        "audio": audio_dir,
        "video_count": video_count,
        "voiceover_count": voiceover_count,
        "errors": errors,
    }


def add_shadow(widget: QWidget, blur: int = 30, y_offset: int = 8) -> None:
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, y_offset)
    shadow.setColor(QColor(82, 112, 145, 35))
    widget.setGraphicsEffect(shadow)


def repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def make_icon(kind: str, color: str = THEME.text, size: int = 22) -> QIcon:
    """Draw one consistent outline icon family without text glyphs."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(
        QColor(color),
        max(1.5, size / 13),
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    s = float(size)

    if kind == "folder":
        path = QPainterPath()
        path.moveTo(s * 0.13, s * 0.32)
        path.lineTo(s * 0.4, s * 0.32)
        path.lineTo(s * 0.48, s * 0.42)
        path.lineTo(s * 0.87, s * 0.42)
        path.lineTo(s * 0.8, s * 0.78)
        path.lineTo(s * 0.16, s * 0.78)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "file":
        painter.drawRoundedRect(
            int(s * 0.24), int(s * 0.12), int(s * 0.52), int(s * 0.76), 2, 2
        )
        painter.drawLine(
            int(s * 0.34), int(s * 0.52), int(s * 0.66), int(s * 0.52)
        )
        painter.drawLine(
            int(s * 0.34), int(s * 0.66), int(s * 0.58), int(s * 0.66)
        )
    elif kind == "video":
        painter.drawRoundedRect(
            int(s * 0.12), int(s * 0.24), int(s * 0.59), int(s * 0.52), 4, 4
        )
        path = QPainterPath()
        path.moveTo(s * 0.71, s * 0.4)
        path.lineTo(s * 0.9, s * 0.3)
        path.lineTo(s * 0.9, s * 0.7)
        path.lineTo(s * 0.71, s * 0.6)
        painter.drawPath(path)
    elif kind == "audio":
        for x, top, bottom in (
            (0.29, 0.36, 0.64),
            (0.45, 0.24, 0.76),
            (0.61, 0.32, 0.68),
            (0.77, 0.43, 0.57),
        ):
            painter.drawLine(int(s * x), int(s * top), int(s * x), int(s * bottom))
    elif kind == "grid":
        for row in range(2):
            for column in range(2):
                painter.drawRoundedRect(
                    int(s * (0.16 + column * 0.38)),
                    int(s * (0.16 + row * 0.38)),
                    int(s * 0.27),
                    int(s * 0.27),
                    3,
                    3,
                )
    elif kind == "review":
        painter.drawRoundedRect(
            int(s * 0.18), int(s * 0.13), int(s * 0.64), int(s * 0.74), 4, 4
        )
        painter.drawLine(
            int(s * 0.31), int(s * 0.38), int(s * 0.42), int(s * 0.49)
        )
        painter.drawLine(
            int(s * 0.42), int(s * 0.49), int(s * 0.65), int(s * 0.27)
        )
        painter.drawLine(
            int(s * 0.31), int(s * 0.66), int(s * 0.68), int(s * 0.66)
        )
    elif kind == "export":
        painter.drawRoundedRect(
            int(s * 0.19), int(s * 0.18), int(s * 0.62), int(s * 0.67), 4, 4
        )
        painter.drawLine(int(s * 0.5), int(s * 0.63), int(s * 0.5), int(s * 0.08))
        painter.drawLine(int(s * 0.35), int(s * 0.24), int(s * 0.5), int(s * 0.08))
        painter.drawLine(int(s * 0.65), int(s * 0.24), int(s * 0.5), int(s * 0.08))
    elif kind == "check":
        painter.drawEllipse(
            int(s * 0.12), int(s * 0.12), int(s * 0.76), int(s * 0.76)
        )
        painter.drawLine(int(s * 0.29), int(s * 0.51), int(s * 0.43), int(s * 0.65))
        painter.drawLine(int(s * 0.43), int(s * 0.65), int(s * 0.72), int(s * 0.35))
    elif kind == "log":
        for y in (0.28, 0.5, 0.72):
            painter.drawEllipse(int(s * 0.15), int(s * y - 1), 2, 2)
            painter.drawLine(int(s * 0.29), int(s * y), int(s * 0.82), int(s * y))
    elif kind == "copy":
        painter.drawRoundedRect(
            int(s * 0.28), int(s * 0.17), int(s * 0.5), int(s * 0.55), 3, 3
        )
        painter.drawRoundedRect(
            int(s * 0.17), int(s * 0.3), int(s * 0.5), int(s * 0.55), 3, 3
        )
    elif kind == "open":
        painter.drawRoundedRect(
            int(s * 0.15), int(s * 0.28), int(s * 0.56), int(s * 0.56), 3, 3
        )
        painter.drawLine(int(s * 0.44), int(s * 0.56), int(s * 0.86), int(s * 0.14))
        painter.drawLine(int(s * 0.6), int(s * 0.14), int(s * 0.86), int(s * 0.14))
        painter.drawLine(int(s * 0.86), int(s * 0.14), int(s * 0.86), int(s * 0.4))
    else:
        painter.drawEllipse(int(s * 0.2), int(s * 0.2), int(s * 0.6), int(s * 0.6))

    painter.end()
    return QIcon(pixmap)


APP_STYLE = f"""
QWidget {{ color: {THEME.text}; font-size: 14px; }}
QFrame[card="true"] {{
    background: {THEME.card};
    border: 1px solid rgba(220, 229, 240, 0.75);
    border-radius: 24px;
}}
QLabel[role="title"] {{ font-size: 29px; font-weight: 500; color: {THEME.text}; }}
QLabel[role="cardTitle"] {{ font-size: 18px; font-weight: 500; color: {THEME.text}; }}
QLabel[role="sectionTitle"] {{ font-size: 16px; font-weight: 500; color: {THEME.text}; }}
QLabel[role="body"] {{ font-size: 14px; color: {THEME.secondary}; }}
QLabel[role="caption"] {{ font-size: 12px; color: {THEME.secondary}; }}
QLabel[role="eyebrow"] {{ font-size: 11px; font-weight: 600; color: {THEME.accent}; }}
QLabel[role="path"] {{ font-size: 12px; color: {THEME.tertiary}; }}
QLabel[role="error"] {{ font-size: 12px; color: {THEME.error}; }}
QLabel[badge="ready"] {{ background: {THEME.success_soft}; color: {THEME.success}; border-radius: 10px; padding: 4px 9px; font-size: 11px; font-weight: 600; }}
QLabel[badge="warning"] {{ background: {THEME.warning_soft}; color: {THEME.warning}; border-radius: 10px; padding: 4px 9px; font-size: 11px; font-weight: 600; }}
QLabel[badge="error"] {{ background: {THEME.error_soft}; color: {THEME.error}; border-radius: 10px; padding: 4px 9px; font-size: 11px; font-weight: 600; }}
QLabel[badge="neutral"] {{ background: #F1F5F9; color: {THEME.secondary}; border-radius: 10px; padding: 4px 9px; font-size: 11px; font-weight: 600; }}
QPushButton {{
    min-height: 34px; border-radius: 12px; padding: 0 14px;
    font-size: 13px; font-weight: 500; border: 1px solid {THEME.border};
    background: white; color: {THEME.text};
}}
QPushButton:hover {{ background: #F5F9FD; border-color: #C8D6E7; }}
QPushButton:pressed {{ background: #EAF1F8; }}
QPushButton:focus {{ border: 2px solid {THEME.accent}; }}
QPushButton:disabled {{ color: #A9B4C4; background: #F3F6F9; border-color: #E7EDF4; }}
QPushButton[variant="primary"] {{ background: {THEME.navy}; color: white; border: none; min-height: 40px; padding: 0 20px; }}
QPushButton[variant="primary"]:hover {{ background: {THEME.navy_hover}; }}
QPushButton[variant="primary"]:pressed {{ background: #151E3A; }}
QPushButton[variant="primary"]:disabled {{ background: #CBD3DF; color: #F5F7FA; }}
QPushButton[variant="soft"] {{ background: {THEME.accent_soft}; color: #1678B3; border: none; }}
QPushButton[variant="soft"]:hover {{ background: #D4EDFC; }}
QPushButton[variant="icon"] {{ min-width: 34px; max-width: 34px; padding: 0; background: transparent; border: none; }}
QPushButton[variant="icon"]:hover {{ background: {THEME.accent_soft}; }}
QLineEdit {{ background: #F7FAFD; border: 1px solid {THEME.border}; border-radius: 12px; min-height: 40px; padding: 0 12px; selection-background-color: {THEME.accent}; }}
QLineEdit:focus {{ border: 2px solid {THEME.accent}; background: white; }}
QPlainTextEdit {{ background: #F7FAFD; border: 1px solid {THEME.border}; border-radius: 14px; padding: 10px; color: {THEME.secondary}; font-size: 12px; }}
QTreeWidget {{ background: white; border: none; outline: none; alternate-background-color: #F8FBFE; }}
QTreeWidget::item {{ min-height: 48px; border-bottom: 1px solid #EDF2F7; }}
QTreeWidget::item:hover {{ background: #F3F8FD; }}
QTreeWidget::item:selected {{ background: {THEME.accent_soft}; color: {THEME.text}; }}
QHeaderView::section {{ background: #F7FAFD; color: {THEME.secondary}; border: none; border-bottom: 1px solid {THEME.border}; padding: 10px 8px; font-size: 12px; font-weight: 500; }}
QCheckBox {{ spacing: 8px; color: {THEME.secondary}; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid #BCC9D8; background: white; }}
QCheckBox::indicator:checked {{ background: {THEME.navy}; border-color: {THEME.navy}; }}
QProgressBar {{ border: none; background: #DFE8F2; border-radius: 3px; max-height: 5px; min-height: 5px; }}
QProgressBar::chunk {{ background: {THEME.accent}; border-radius: 3px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #C7D4E3; min-height: 40px; border-radius: 4px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QToolTip {{ background: {THEME.navy}; color: white; border: none; padding: 6px; }}
"""


class GradientSurface(QWidget):
    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        painter = QPainter(self)
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0, QColor(THEME.gradient_start))
        gradient.setColorAt(1, QColor(THEME.gradient_end))
        painter.fillRect(self.rect(), gradient)


class ElideLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def setFullText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._update_elision()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self) -> None:
        self.setText(
            self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideMiddle,
                max(40, self.width() - 4),
            )
        )


class IconTile(QFrame):
    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("IconTile")
        self.setFixedSize(46, 46)
        self.setStyleSheet(
            f"QFrame#IconTile {{ background: {THEME.accent_soft}; "
            "border-radius: 14px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 11, 11, 11)
        label = QLabel()
        label.setPixmap(make_icon(kind, THEME.accent, 24).pixmap(24, 24))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)


class HeaderBlock(QWidget):
    def __init__(
        self,
        eyebrow: str,
        title: str,
        subtitle: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.eyebrow = QLabel(eyebrow.upper())
        self.eyebrow.setProperty("role", "eyebrow")
        self.title = QLabel(title)
        self.title.setProperty("role", "title")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setProperty("role", "body")
        self.subtitle.setWordWrap(True)
        self.subtitle.setVisible(bool(subtitle))
        layout.addWidget(self.eyebrow)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)

    def setCopy(self, eyebrow: str, title: str, subtitle: str) -> None:
        self.eyebrow.setText(eyebrow.upper())
        self.title.setText(title)
        self.subtitle.setText(subtitle)
        self.subtitle.setVisible(bool(subtitle))


class SourceCard(QFrame):
    chooseRequested = Signal(str)
    clearRequested = Signal(str)
    copyRequested = Signal(str)
    openRequested = Signal(str)

    def __init__(
        self,
        key: str,
        icon: str,
        title: str,
        description: str,
        optional: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self.optional = optional
        self.current_path = ""
        self.setProperty("card", True)
        self.setMinimumHeight(178)
        add_shadow(self, 24, 6)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(12)
        heading = QHBoxLayout()
        heading.setSpacing(13)
        heading.addWidget(IconTile(icon))
        copy = QVBoxLayout()
        copy.setSpacing(3)
        title_label = QLabel(title)
        title_label.setProperty("role", "cardTitle")
        description_label = QLabel(description)
        description_label.setProperty("role", "caption")
        description_label.setWordWrap(True)
        description_label.setVisible(bool(description))
        copy.addWidget(title_label)
        copy.addWidget(description_label)
        heading.addLayout(copy, 1)
        self.badge = QLabel("Chưa chọn")
        self.badge.setProperty("badge", "neutral")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(heading)

        self.name_label = ElideLabel("Chưa chọn nguồn")
        self.name_label.setProperty("role", "sectionTitle")
        self.path_label = ElideLabel("")
        self.path_label.setProperty("role", "path")
        outer.addWidget(self.name_label)
        outer.addWidget(self.path_label)

        self.error_label = QLabel()
        self.error_label.setProperty("role", "error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        outer.addWidget(self.error_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.choose_button = QPushButton("Chọn nguồn")
        self.choose_button.setProperty("variant", "soft")
        self.choose_button.setIcon(make_icon("folder", "#1678B3", 18))
        self.choose_button.clicked.connect(
            lambda: self.chooseRequested.emit(self.key)
        )
        self.clear_button = QPushButton("Bỏ chọn")
        self.clear_button.clicked.connect(
            lambda: self.clearRequested.emit(self.key)
        )
        self.clear_button.hide()
        self.copy_button = QPushButton("Copy")
        self.copy_button.setIcon(make_icon("copy", THEME.secondary, 17))
        self.copy_button.clicked.connect(
            lambda: self.copyRequested.emit(self.current_path)
        )
        self.open_button = QPushButton(f"Mở {FILE_MANAGER}")
        self.open_button.setIcon(make_icon("open", THEME.secondary, 17))
        self.open_button.clicked.connect(
            lambda: self.openRequested.emit(self.current_path)
        )
        actions.addWidget(self.choose_button)
        actions.addWidget(self.clear_button)
        actions.addStretch(1)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.open_button)
        outer.addLayout(actions)

    def setPathState(self, path: Path | None, state: str, message: str) -> None:
        self.current_path = str(path) if path else ""
        if path:
            self.name_label.setFullText(path.name)
            self.path_label.setFullText(str(path))
            self.choose_button.setText("Thay đổi")
        else:
            self.name_label.setFullText(
                "Không sử dụng" if self.optional else "Chưa chọn nguồn"
            )
            self.path_label.setFullText(
                ""
                if self.optional
                else "Chọn thủ công hoặc đổi project để tự phát hiện."
            )
            self.choose_button.setText("Chọn file" if self.optional else "Chọn nguồn")

        badge_text, badge_state = {
            "ready": ("Sẵn sàng", "ready"),
            "error": ("Cần xử lý", "error"),
            "warning": ("Chưa chọn", "warning"),
            "optional": ("Tùy chọn", "neutral"),
        }.get(state, ("Chưa chọn", "neutral"))
        self.badge.setText(badge_text)
        self.badge.setProperty("badge", badge_state)
        repolish(self.badge)
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message) and state == "error")
        self.copy_button.setEnabled(bool(path))
        self.open_button.setEnabled(bool(path))
        self.clear_button.setVisible(self.optional and bool(path))

    def setBusy(self, busy: bool) -> None:
        self.choose_button.setDisabled(busy)
        self.copy_button.setDisabled(busy or not self.current_path)
        self.open_button.setDisabled(busy or not self.current_path)
        self.clear_button.setDisabled(busy or not self.current_path)

    def setCompact(self, compact: bool) -> None:
        if compact:
            self.copy_button.setText("")
            self.copy_button.setFixedWidth(36)
            self.copy_button.setToolTip("Copy đường dẫn")
            self.open_button.setText("")
            self.open_button.setFixedWidth(36)
            self.open_button.setToolTip(f"Mở trong {FILE_MANAGER}")
        else:
            self.copy_button.setText("Copy")
            self.copy_button.setMaximumWidth(16777215)
            self.copy_button.setMinimumWidth(0)
            self.open_button.setText(f"Mở {FILE_MANAGER}")
            self.open_button.setMaximumWidth(16777215)
            self.open_button.setMinimumWidth(0)

    def contextMenuEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self.current_path:
            return
        menu = QMenu(self)
        copy_action = menu.addAction(
            make_icon("copy", THEME.secondary, 17), "Copy đường dẫn"
        )
        open_action = menu.addAction(
            make_icon("open", THEME.secondary, 17), f"Mở trong {FILE_MANAGER}"
        )
        selected = menu.exec(event.globalPos())
        if selected == copy_action:
            self.copyRequested.emit(self.current_path)
        elif selected == open_action:
            self.openRequested.emit(self.current_path)


class ProjectSummaryCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        add_shadow(self, 24, 6)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        title = QLabel("Tóm tắt project")
        title.setProperty("role", "cardTitle")
        self.project_name = ElideLabel("Chưa có project")
        self.project_name.setProperty("role", "sectionTitle")
        self.project_path = ElideLabel("")
        self.project_path.setProperty("role", "path")
        layout.addWidget(title)
        layout.addWidget(self.project_name)
        layout.addWidget(self.project_path)
        self.rows: dict[str, QLabel] = {}
        for key, label in (
            ("content", "Content DOCX"),
            ("video", "Video nguồn"),
            ("audio", "File voice-over"),
            ("transcript", "Transcript TXT"),
        ):
            row = QHBoxLayout()
            name = QLabel(label)
            name.setProperty("role", "body")
            value = QLabel("Chưa có")
            value.setProperty("role", "caption")
            row.addWidget(name)
            row.addStretch(1)
            row.addWidget(value)
            layout.addLayout(row)
            self.rows[key] = value
        layout.addStretch(1)

    def updateSummary(
        self,
        project: Path | None,
        states: dict[str, tuple[str, str]],
        video_count: int,
        voiceover_count: int,
    ) -> None:
        self.project_name.setFullText(project.name if project else "Chưa có project")
        self.project_path.setFullText(str(project) if project else "")
        for key in ("content", "video", "audio", "transcript"):
            state = states[key][0]
            if state == "ready":
                if key == "video" and video_count:
                    text = f"{video_count} video"
                elif key == "audio" and voiceover_count:
                    text = f"{voiceover_count} file"
                else:
                    text = "Đã tìm thấy"
                color = THEME.success
            elif state == "optional":
                text, color = "Không dùng", THEME.tertiary
            elif state == "error":
                text, color = "Có lỗi", THEME.error
            else:
                text, color = "Chưa có", THEME.warning
            self.rows[key].setText(text)
            self.rows[key].setStyleSheet(f"color: {color}; font-size: 12px;")


class ExportSettingsCard(QFrame):
    sequenceChanged = Signal(str)
    chooseOutputRequested = Signal()
    copyOutputRequested = Signal(str)
    openOutputRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        add_shadow(self, 24, 6)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(10)
        title = QLabel("Cấu hình xuất")
        title.setProperty("role", "cardTitle")
        layout.addWidget(title)
        sequence_label = QLabel("Tên sequence")
        sequence_label.setProperty("role", "caption")
        self.sequence_edit = QLineEdit()
        self.sequence_edit.setPlaceholderText("Tên sequence trong Premiere")
        self.sequence_edit.textChanged.connect(self.sequenceChanged)
        layout.addWidget(sequence_label)
        layout.addWidget(self.sequence_edit)
        output_label = QLabel("XML đầu ra")
        output_label.setProperty("role", "caption")
        self.output_name = ElideLabel("Chưa chọn vị trí lưu")
        self.output_name.setProperty("role", "sectionTitle")
        self.output_path = ElideLabel("")
        self.output_path.setProperty("role", "path")
        layout.addWidget(output_label)
        layout.addWidget(self.output_name)
        layout.addWidget(self.output_path)
        actions = QHBoxLayout()
        self.choose_button = QPushButton("Chọn vị trí lưu")
        self.choose_button.setProperty("variant", "soft")
        self.choose_button.clicked.connect(self.chooseOutputRequested)
        self.copy_button = QPushButton()
        self.copy_button.setProperty("variant", "icon")
        self.copy_button.setIcon(make_icon("copy", THEME.secondary, 18))
        self.copy_button.setToolTip("Copy đường dẫn XML")
        self.copy_button.clicked.connect(
            lambda: self.copyOutputRequested.emit(self.output_path.toolTip())
        )
        self.open_button = QPushButton()
        self.open_button.setProperty("variant", "icon")
        self.open_button.setIcon(make_icon("open", THEME.secondary, 18))
        self.open_button.setToolTip(f"Mở trong {FILE_MANAGER}")
        self.open_button.clicked.connect(
            lambda: self.openOutputRequested.emit(self.output_path.toolTip())
        )
        actions.addWidget(self.choose_button)
        actions.addStretch(1)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.open_button)
        layout.addLayout(actions)

    def setValues(self, sequence: str, output: Path | None) -> None:
        self.sequence_edit.blockSignals(True)
        self.sequence_edit.setText(sequence)
        self.sequence_edit.blockSignals(False)
        self.output_name.setFullText(
            output.name if output else "Chưa chọn vị trí lưu"
        )
        self.output_path.setFullText(str(output) if output else "")
        self.copy_button.setEnabled(output is not None)
        self.open_button.setEnabled(output is not None)

    def setBusy(self, busy: bool) -> None:
        self.sequence_edit.setDisabled(busy)
        self.choose_button.setDisabled(busy)
        self.copy_button.setDisabled(busy or not self.output_path.toolTip())
        self.open_button.setDisabled(busy or not self.output_path.toolTip())


class WelcomePage(QWidget):
    chooseRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 28, 28, 28)
        outer.addStretch(1)
        card = QFrame()
        card.setProperty("card", True)
        card.setMinimumSize(560, 320)
        card.setMaximumWidth(680)
        add_shadow(card, 36, 10)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(38, 36, 38, 36)
        card_layout.setSpacing(14)
        icon_tile = IconTile("folder")
        icon_tile.setFixedSize(58, 58)
        card_layout.addWidget(icon_tile, 0, Qt.AlignmentFlag.AlignHCenter)
        title = QLabel("Bắt đầu với một project")
        title.setProperty("role", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setMinimumHeight(42)
        body = QLabel(
            "Chọn thư mục chứa content DOCX, video và voice-over. "
            "XML Cut Room sẽ tự tìm các nguồn hiện có."
        )
        body.setProperty("role", "body")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setMinimumHeight(48)
        self.notice = QLabel("Chưa chọn project")
        self.notice.setProperty("role", "caption")
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.choose_button = QPushButton("Chọn project")
        self.choose_button.setProperty("variant", "primary")
        self.choose_button.setIcon(make_icon("folder", "white", 18))
        self.choose_button.clicked.connect(self.chooseRequested)
        self.choose_button.setMaximumWidth(180)
        card_layout.addWidget(title)
        card_layout.addWidget(body)
        card_layout.addWidget(self.notice)
        card_layout.addWidget(
            self.choose_button, 0, Qt.AlignmentFlag.AlignHCenter
        )
        outer.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)

    def setNotice(self, text: str, error: bool = False) -> None:
        self.notice.setText(text)
        self.notice.setStyleSheet(
            f"color: {THEME.error if error else THEME.secondary}; font-size: 12px;"
        )


class SetupPage(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.setWidget(self.content)
        outer = QVBoxLayout(self.content)
        outer.setContentsMargins(28, 26, 28, 28)
        outer.setSpacing(20)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self.header = HeaderBlock(
            "Bước 2",
            "Chuẩn bị dữ liệu",
            "",
        )
        outer.addWidget(self.header)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setProperty("role", "error")
        self.notice.setStyleSheet(
            f"background: {THEME.error_soft}; border-radius: 12px; "
            f"padding: 10px 13px; color: {THEME.error};"
        )
        self.notice.hide()
        outer.addWidget(self.notice)
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(18)
        self.source_cards = {
            "content": SourceCard(
                "content",
                "file",
                "Content DOCX",
                "",
            ),
            "video": SourceCard(
                "video",
                "video",
                "Thư mục video",
                "",
            ),
            "audio": SourceCard(
                "audio",
                "audio",
                "Thư mục voice-over",
                "",
            ),
            "transcript": SourceCard(
                "transcript",
                "file",
                "Transcript TXT",
                "",
                optional=True,
            ),
        }
        self.summary = ProjectSummaryCard()
        self.export = ExportSettingsCard()
        for card in self.source_cards.values():
            card.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
        outer.addLayout(self.grid)
        outer.addStretch(1)
        self._compact: bool | None = None
        self._reflow(False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow(event.size().width() < 900)

    def _reflow(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().setParent(self.content)
        cards = list(self.source_cards.values())
        for card in cards:
            card.setCompact(compact)
        if compact:
            for row, card in enumerate(cards):
                self.grid.addWidget(card, row, 0)
            self.grid.addWidget(self.summary, len(cards), 0)
            self.grid.addWidget(self.export, len(cards) + 1, 0)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 0)
        else:
            for row, card in enumerate(cards):
                self.grid.addWidget(card, row, 0)
            self.grid.addWidget(self.summary, 0, 1)
            self.grid.addWidget(self.export, 1, 1, len(cards) - 1, 1)
            self.grid.setColumnStretch(0, 2)
            self.grid.setColumnStretch(1, 1)

    def showNotice(self, text: str = "") -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))


class StructureReview(QFrame):
    selectionChanged = Signal()
    openDocumentRequested = Signal()
    copyDocumentRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        add_shadow(self, 24, 6)
        self.components: list[ArticleComponent] = []
        self._syncing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 16, 20, 14)
        title_copy = QVBoxLayout()
        title_copy.setSpacing(3)
        title = QLabel("Thành phần bài")
        title.setProperty("role", "cardTitle")
        self.document_label = ElideLabel()
        self.document_label.setProperty("role", "caption")
        title_copy.addWidget(title)
        title_copy.addWidget(self.document_label)
        header_layout.addLayout(title_copy, 1)
        self.open_button = QPushButton("Mở DOCX")
        self.open_button.setIcon(make_icon("open", THEME.secondary, 17))
        self.open_button.clicked.connect(self.openDocumentRequested)
        self.copy_button = QPushButton("Copy đường dẫn")
        self.copy_button.setIcon(make_icon("copy", THEME.secondary, 17))
        self.copy_button.clicked.connect(self.copyDocumentRequested)
        header_layout.addWidget(self.open_button)
        header_layout.addWidget(self.copy_button)
        layout.addWidget(header)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(
            (
                "",
                "Thành phần",
                "Nội dung",
                "Video cut",
                "Text block",
                "Trạng thái",
            )
        )
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header_view = self.tree.header()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header_view.resizeSection(0, 42)
        header_view.setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column in (3, 4, 5):
            header_view.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDoubleClicked.connect(
            lambda item, _column: item.setExpanded(not item.isExpanded())
        )
        layout.addWidget(self.tree, 1)

    def load(self, content: Path, components: list[ArticleComponent]) -> None:
        self._syncing = True
        self.tree.clear()
        self.components = components
        self.document_label.setFullText(str(content))
        for index, component in enumerate(components):
            ready = component_is_ready(component)
            preview = ", ".join(component.timecodes[:2])
            if len(component.timecodes) > 2:
                preview += f" và {len(component.timecodes) - 2} đoạn khác"
            if not preview:
                preview = f"{component.text_block_count} khối lời đọc"
            item = QTreeWidgetItem(
                (
                    "",
                    component.title,
                    preview,
                    str(component.video_cut_count),
                    str(component.text_block_count),
                    "Hợp lệ" if ready else "Cần kiểm tra",
                )
            )
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            flags = item.flags()
            if ready:
                item.setFlags(flags | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
                item.setForeground(5, QColor(THEME.success))
            else:
                item.setFlags(flags & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setForeground(5, QColor(THEME.error))
            if component.timecodes:
                cuts = QTreeWidgetItem(
                    (
                        "",
                        "Chi tiết video",
                        "   •   ".join(component.timecodes),
                        f"{component.video_cut_count} đoạn",
                        "",
                        "",
                    )
                )
                cuts.setForeground(2, QColor(THEME.secondary))
                item.addChild(cuts)
            text_detail = QTreeWidgetItem(
                (
                    "",
                    "Chi tiết lời đọc",
                    f"Đã phát hiện {component.text_block_count} text block trong DOCX",
                    "",
                    str(component.text_block_count),
                    "",
                )
            )
            text_detail.setForeground(2, QColor(THEME.secondary))
            item.addChild(text_detail)
            self.tree.addTopLevelItem(item)
        self._syncing = False
        self.selectionChanged.emit()

    def _item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._syncing or item.parent() is not None:
            return
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(index, int) and not component_is_ready(self.components[index]):
            self._syncing = True
            item.setCheckState(0, Qt.CheckState.Unchecked)
            self._syncing = False
        self.selectionChanged.emit()

    def setAllValid(self, checked: bool) -> None:
        self._syncing = True
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if component_is_ready(self.components[index]):
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if checked
                    else Qt.CheckState.Unchecked,
                )
        self._syncing = False
        self.selectionChanged.emit()

    def selectionStats(self) -> tuple[int, int, bool]:
        total = self.tree.topLevelItemCount()
        selected = sum(
            self.tree.topLevelItem(index).checkState(0) == Qt.CheckState.Checked
            for index in range(total)
        )
        all_ready = bool(self.components) and all(
            component_is_ready(component) for component in self.components
        )
        return selected, total, all_ready

    def collapseAllDetails(self) -> bool:
        expanded = any(
            self.tree.topLevelItem(index).isExpanded()
            for index in range(self.tree.topLevelItemCount())
        )
        if expanded:
            self.tree.collapseAll()
        return expanded


class ReviewPage(QWidget):
    selectAllChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(16)
        self.header = HeaderBlock(
            "Bước 3",
            "Kiểm tra cấu trúc",
            "",
        )
        layout.addWidget(self.header)
        summary = QFrame()
        summary.setProperty("card", True)
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(18, 12, 18, 12)
        summary_layout.setSpacing(18)
        self.component_metric = QLabel()
        self.cut_metric = QLabel()
        self.text_metric = QLabel()
        self.voice_metric = QLabel()
        for metric in (
            self.component_metric,
            self.cut_metric,
            self.text_metric,
            self.voice_metric,
        ):
            metric.setProperty("role", "body")
            summary_layout.addWidget(metric)
        summary_layout.addStretch(1)
        self.selection_label = QLabel()
        self.selection_label.setProperty("role", "caption")
        self.select_all = QCheckBox("Chọn tất cả phần hợp lệ")
        self.select_all.toggled.connect(self.selectAllChanged)
        summary_layout.addWidget(self.selection_label)
        summary_layout.addWidget(self.select_all)
        layout.addWidget(summary)
        self.structure = StructureReview()
        layout.addWidget(self.structure, 1)

    def updateSummary(
        self,
        components: list[ArticleComponent],
        voiceover_files: int,
        selected: int,
    ) -> None:
        total = len(components)
        cuts = sum(component.video_cut_count for component in components)
        text_blocks = sum(component.text_block_count for component in components)
        self.component_metric.setText(f"{total} thành phần")
        self.cut_metric.setText(f"{cuts} video cut")
        self.text_metric.setText(f"{text_blocks} text block")
        self.voice_metric.setText(f"{voiceover_files} file voice-over")
        self.selection_label.setText(f"Đã chọn {selected}/{total}")
        self.select_all.blockSignals(True)
        self.select_all.setChecked(bool(total and selected == total))
        self.select_all.blockSignals(False)


class ResultPage(QWidget):
    openRequested = Signal()
    copyRequested = Signal()
    backRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(20)
        self.header = HeaderBlock(
            "Hoàn tất",
            "XML đã sẵn sàng",
            "Timeline đã được tạo và kiểm tra bằng dữ liệu thực tế.",
        )
        layout.addWidget(self.header)
        card = QFrame()
        card.setProperty("card", True)
        add_shadow(card, 30, 8)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 26, 28, 26)
        card_layout.setSpacing(15)
        heading = QHBoxLayout()
        heading.addWidget(IconTile("check"))
        copy = QVBoxLayout()
        title = QLabel("Xuất XML thành công")
        title.setProperty("role", "cardTitle")
        self.summary_label = QLabel()
        self.summary_label.setProperty("role", "body")
        self.summary_label.setWordWrap(True)
        copy.addWidget(title)
        copy.addWidget(self.summary_label)
        heading.addLayout(copy, 1)
        card_layout.addLayout(heading)
        self.output_name = ElideLabel()
        self.output_name.setProperty("role", "sectionTitle")
        self.output_path = ElideLabel()
        self.output_path.setProperty("role", "path")
        card_layout.addWidget(self.output_name)
        card_layout.addWidget(self.output_path)
        actions = QHBoxLayout()
        open_button = QPushButton(f"Mở trong {FILE_MANAGER}")
        open_button.setProperty("variant", "primary")
        open_button.setIcon(make_icon("open", "white", 18))
        open_button.clicked.connect(self.openRequested)
        copy_button = QPushButton("Copy đường dẫn")
        copy_button.setIcon(make_icon("copy", THEME.secondary, 18))
        copy_button.clicked.connect(self.copyRequested)
        back_button = QPushButton("Quay lại cấu hình")
        back_button.clicked.connect(self.backRequested)
        actions.addWidget(open_button)
        actions.addWidget(copy_button)
        actions.addStretch(1)
        actions.addWidget(back_button)
        card_layout.addLayout(actions)
        layout.addWidget(card)
        layout.addStretch(1)

    def setSummary(self, summary: SimpleGenerationSummary) -> None:
        self.summary_label.setText(
            f"{summary.video_cut_count} video cut · "
            f"{summary.narration_count} voice-over · "
            f"{summary.duration_seconds:.2f} giây · {summary.fps} fps"
        )
        self.output_name.setFullText(summary.output.name)
        self.output_path.setFullText(str(summary.output))


class Sidebar(QFrame):
    changeProjectRequested = Signal()
    logRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(220)
        self.setStyleSheet(
            "QFrame#Sidebar { background: rgba(255, 255, 255, 0.68); "
            "border-right: 1px solid rgba(220, 229, 240, 0.8); }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(16)
        brand = QHBoxLayout()
        logo_label = QLabel()
        logo_path = bundled_asset("xmlcutroom-logo.png")
        if logo_path:
            logo_label.setPixmap(
                QPixmap(str(logo_path)).scaled(
                    38,
                    38,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            logo_label.setPixmap(make_icon("grid", THEME.accent, 30).pixmap(38, 38))
        brand_name = QLabel("XML Cut Room")
        brand_name.setStyleSheet(
            f"font-size: 16px; font-weight: 600; color: {THEME.text};"
        )
        brand.addWidget(logo_label)
        brand.addWidget(brand_name)
        brand.addStretch(1)
        layout.addLayout(brand)

        self.project_card = QFrame()
        self.project_card.setObjectName("ProjectCard")
        self.project_card.setStyleSheet(
            f"QFrame#ProjectCard {{ background: white; "
            f"border: 1px solid {THEME.border}; border-radius: 16px; }}"
        )
        project_layout = QVBoxLayout(self.project_card)
        project_layout.setContentsMargins(13, 12, 13, 12)
        project_layout.setSpacing(5)
        project_caption = QLabel("Project")
        project_caption.setProperty("role", "eyebrow")
        self.project_name = ElideLabel("Chưa chọn")
        self.project_name.setStyleSheet(
            f"font-size: 13px; font-weight: 500; color: {THEME.text};"
        )
        self.project_status = QLabel("Cần chọn project")
        self.project_status.setProperty("role", "caption")
        self.change_button = QPushButton("Đổi project")
        self.change_button.setProperty("variant", "soft")
        self.change_button.clicked.connect(self.changeProjectRequested)
        project_layout.addWidget(project_caption)
        project_layout.addWidget(self.project_name)
        project_layout.addWidget(self.project_status)
        project_layout.addWidget(self.change_button)
        layout.addWidget(self.project_card)

        steps_label = QLabel("QUY TRÌNH")
        steps_label.setProperty("role", "caption")
        layout.addWidget(steps_label)
        self.step_rows: list[tuple[QFrame, QLabel, QLabel]] = []
        icons = ("folder", "grid", "review", "export")
        for index, (title, icon_name) in enumerate(
            zip(STEP_TITLES, icons, strict=True)
        ):
            row = QFrame()
            row.setObjectName("StepRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(11, 9, 11, 9)
            row_layout.setSpacing(10)
            icon = QLabel()
            icon.setFixedSize(22, 22)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label = QLabel(f"{index + 1}. {title}")
            row_layout.addWidget(icon)
            row_layout.addWidget(label, 1)
            layout.addWidget(row)
            self.step_rows.append((row, icon, label))

        layout.addStretch(1)
        self.log_button = QPushButton("Nhật ký")
        self.log_button.setIcon(make_icon("log", THEME.secondary, 18))
        self.log_button.clicked.connect(self.logRequested)
        layout.addWidget(self.log_button)
        version = QLabel("Phiên bản 3.0 · PySide6")
        version.setProperty("role", "caption")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version)

    def updateState(
        self,
        project: Path | None,
        project_status: str,
        states: list[str],
    ) -> None:
        self.project_name.setFullText(project.name if project else "Chưa chọn")
        self.project_status.setText(project_status)
        self.project_status.setVisible(bool(project_status))
        self.change_button.setText("Đổi project" if project else "Chọn project")
        icons = ("folder", "grid", "review", "export")
        for index, (state, parts) in enumerate(
            zip(states, self.step_rows, strict=True)
        ):
            row, icon, label = parts
            if state == "current":
                background, foreground = THEME.navy, "white"
                icon_name = icons[index]
            elif state == "complete":
                background, foreground = "transparent", THEME.success
                icon_name = "check"
            elif state == "error":
                background, foreground = THEME.error_soft, THEME.error
                icon_name = icons[index]
            else:
                background, foreground = "transparent", THEME.tertiary
                icon_name = icons[index]
            row.setStyleSheet(
                f"QFrame#StepRow {{ background: {background}; "
                "border-radius: 13px; }"
            )
            icon.setPixmap(make_icon(icon_name, foreground, 20).pixmap(20, 20))
            label.setStyleSheet(
                f"font-size: 13px; color: {foreground}; "
                f"font-weight: {'500' if state == 'current' else '400'};"
            )


class LogPanel(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        title = QLabel("Nhật ký")
        title.setProperty("role", "sectionTitle")
        copy_button = QPushButton("Copy")
        copy_button.setIcon(make_icon("copy", THEME.secondary, 17))
        copy_button.clicked.connect(self.copy)
        clear_button = QPushButton("Xóa")
        clear_button.clicked.connect(self.clear)
        heading.addWidget(title)
        heading.addStretch(1)
        heading.addWidget(copy_button)
        heading.addWidget(clear_button)
        layout.addLayout(heading)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumHeight(150)
        layout.addWidget(self.text)

    def append(self, message: str, level: str = "info") -> None:
        prefix = {
            "success": "✓",
            "warning": "!",
            "error": "!",
            "info": "·",
        }.get(level, "·")
        self.text.appendPlainText(f"{prefix}  {message}")
        scrollbar = self.text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def copy(self) -> None:
        QApplication.clipboard().setText(self.text.toPlainText())

    def clear(self) -> None:
        self.text.clear()


class ActionBar(QFrame):
    primaryRequested = Signal()
    secondaryRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ActionBar")
        self.setStyleSheet(
            "QFrame#ActionBar { background: rgba(255, 255, 255, 0.82); "
            f"border-top: 1px solid {THEME.border}; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 12, 24, 12)
        layout.setSpacing(10)
        status_area = QVBoxLayout()
        status_area.setSpacing(5)
        self.status = QLabel()
        self.status.setProperty("role", "caption")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(250)
        self.progress.hide()
        status_area.addWidget(self.status)
        status_area.addWidget(self.progress)
        layout.addLayout(status_area, 1)
        self.secondary = QPushButton()
        self.secondary.clicked.connect(self.secondaryRequested)
        self.primary = QPushButton()
        self.primary.setProperty("variant", "primary")
        self.primary.clicked.connect(self.primaryRequested)
        layout.addWidget(self.secondary)
        layout.addWidget(self.primary)

    def configure(
        self,
        status: str,
        primary: str,
        secondary: str = "",
        enabled: bool = True,
        busy: bool = False,
    ) -> None:
        self.status.setText(status)
        self.primary.setText(primary)
        self.primary.setEnabled(enabled and not busy)
        self.secondary.setText(secondary)
        self.secondary.setVisible(bool(secondary))
        self.secondary.setDisabled(busy)
        self.progress.setVisible(busy)


class WorkerSignals(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)


class Worker(QRunnable):
    def __init__(self, job_id: int, function: Callable[[], object]) -> None:
        super().__init__()
        self.job_id = job_id
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function()
        except Exception as exc:
            traceback.print_exc()
            self.signals.failed.emit(
                self.job_id, str(exc) or exc.__class__.__name__
            )
        else:
            self.signals.succeeded.emit(self.job_id, result)


class XmlGeneratorApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("XML Cut Room")
        self.resize(1280, 820)
        self.setMinimumSize(1024, 700)
        icon_path = bundled_asset("xmlcutroom-logo.png")
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))

        self.stage = Stage.WELCOME
        self.busy_kind: str | None = None
        self.close_when_idle = False
        self.job_counter = 0
        self.active_job_id = 0
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(2)
        self.workers: dict[int, Worker] = {}

        self.project_path: Path | None = None
        self.content_path: Path | None = None
        self.video_path: Path | None = None
        self.audio_path: Path | None = None
        self.transcript_path: Path | None = None
        self.output_path: Path | None = None
        self.sequence_name = ""
        self.discovery_errors: dict[str, str] = {}
        self.video_count = 0
        self.voiceover_count = 0
        self.verified_signature: tuple[str, str, str, str] | None = None
        self.review_components: list[ArticleComponent] = []
        self.review_content: Path | None = None
        self.last_summary: SimpleGenerationSummary | None = None
        self.last_error = ""

        self._build_ui()
        self._bind_shortcuts()
        self._refresh_ui()
        self._start_default_project_lookup()

    def _build_ui(self) -> None:
        surface = GradientSurface()
        surface.setStyleSheet(APP_STYLE)
        self.setCentralWidget(surface)
        shell = QHBoxLayout(surface)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.changeProjectRequested.connect(self._browse_project)
        self.sidebar.logRequested.connect(self._toggle_log)
        shell.addWidget(self.sidebar)

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        shell.addWidget(workspace, 1)

        self.pages = QStackedWidget()
        self.welcome_page = WelcomePage()
        self.welcome_page.chooseRequested.connect(self._browse_project)
        self.setup_page = SetupPage()
        for card in self.setup_page.source_cards.values():
            card.chooseRequested.connect(self._browse_source)
            card.clearRequested.connect(self._clear_source)
            card.copyRequested.connect(self._copy_text)
            card.openRequested.connect(self._reveal_path)
        export = self.setup_page.export
        export.sequenceChanged.connect(self._set_sequence_name)
        export.chooseOutputRequested.connect(self._browse_output)
        export.copyOutputRequested.connect(self._copy_text)
        export.openOutputRequested.connect(self._reveal_path)
        self.review_page = ReviewPage()
        self.review_page.selectAllChanged.connect(
            self.review_page.structure.setAllValid
        )
        self.review_page.structure.selectionChanged.connect(
            self._review_selection_changed
        )
        self.review_page.structure.openDocumentRequested.connect(
            self._open_review_document
        )
        self.review_page.structure.copyDocumentRequested.connect(
            self._copy_review_document
        )
        self.result_page = ResultPage()
        self.result_page.openRequested.connect(self._open_result)
        self.result_page.copyRequested.connect(self._copy_result)
        self.result_page.backRequested.connect(self._return_to_ready)
        for page in (
            self.welcome_page,
            self.setup_page,
            self.review_page,
            self.result_page,
        ):
            self.pages.addWidget(page)
        workspace_layout.addWidget(self.pages, 1)

        self.log_panel = LogPanel()
        self.log_panel.setContentsMargins(20, 0, 20, 12)
        self.log_panel.hide()
        workspace_layout.addWidget(self.log_panel)
        self.action_bar = ActionBar()
        self.action_bar.primaryRequested.connect(self._primary_action)
        self.action_bar.secondaryRequested.connect(self._secondary_action)
        workspace_layout.addWidget(self.action_bar)

    def _bind_shortcuts(self) -> None:
        open_action = QAction(self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._browse_project)
        self.addAction(open_action)
        log_action = QAction(self)
        log_action.setShortcut(
            QKeySequence("Meta+L" if IS_MACOS else "Ctrl+L")
        )
        log_action.triggered.connect(self._toggle_log)
        self.addAction(log_action)
        primary_action = QAction(self)
        primary_action.setShortcut(
            QKeySequence("Meta+Return" if IS_MACOS else "Ctrl+Return")
        )
        primary_action.triggered.connect(self._primary_action)
        self.addAction(primary_action)
        escape_action = QAction(self)
        escape_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        escape_action.triggered.connect(self._escape)
        self.addAction(escape_action)

    def _next_job_id(self) -> int:
        self.job_counter += 1
        self.active_job_id = self.job_counter
        return self.job_counter

    def _run_worker(
        self,
        kind: str,
        function: Callable[[], object],
        success: Callable[[object], None],
    ) -> None:
        if self.busy_kind:
            return
        job_id = self._next_job_id()
        self.busy_kind = kind
        worker = Worker(job_id, function)

        def succeeded(returned_id: int, result: object) -> None:
            if returned_id != self.active_job_id:
                self.workers.pop(returned_id, None)
                return
            self.workers.pop(returned_id, None)
            self.busy_kind = None
            success(result)
            self._finish_worker()

        def failed(returned_id: int, message: str) -> None:
            if returned_id != self.active_job_id:
                self.workers.pop(returned_id, None)
                return
            self.workers.pop(returned_id, None)
            self.busy_kind = None
            self._worker_failed(kind, message)
            self._finish_worker()

        worker.signals.succeeded.connect(succeeded)
        worker.signals.failed.connect(failed)
        self.workers[job_id] = worker
        self.thread_pool.start(worker)
        self._refresh_ui()

    def _finish_worker(self) -> None:
        if self.close_when_idle:
            QApplication.quit()
            return
        self._refresh_ui()

    def _worker_failed(self, kind: str, message: str) -> None:
        self.last_error = message
        self._log(f"LỖI: {message}", "error")
        if kind == "default_project":
            self.welcome_page.setNotice(
                "Chưa tìm thấy project mặc định. Hãy chọn thủ công."
            )
            return
        if kind == "project":
            self.stage = Stage.WELCOME
            self.welcome_page.setNotice(message, error=True)
        elif kind == "review":
            self.stage = Stage.SETUP
            self.setup_page.showNotice(message)
            self._show_error_dialog("Không thể kiểm tra cấu trúc", message)
        elif kind == "generation":
            self.stage = Stage.READY
            self.setup_page.showNotice(f"Xuất XML thất bại: {message}")
            self._show_error_dialog("Không thể xuất XML", message)

    def _start_default_project_lookup(self) -> None:
        def lookup() -> object:
            return discover_project_sources(resolve_default_project())

        self._run_worker("default_project", lookup, self._project_discovered)

    def _browse_project(self) -> None:
        if self.busy_kind:
            return
        selected = QFileDialog.getExistingDirectory(
            self,
            "Chọn thư mục project",
            str(self.project_path or Path.cwd()),
        )
        if selected:
            self._start_project_discovery(Path(selected))

    def _start_project_discovery(self, project: Path) -> None:
        self._invalidate_review()
        self.last_error = ""
        self._log(f"Đang quét project: {project}")
        self._run_worker(
            "project",
            lambda: discover_project_sources(project),
            self._project_discovered,
        )

    def _project_discovered(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        self.project_path = (
            result.get("project")
            if isinstance(result.get("project"), Path)
            else None
        )
        self.content_path = (
            result.get("content")
            if isinstance(result.get("content"), Path)
            else None
        )
        self.video_path = (
            result.get("video")
            if isinstance(result.get("video"), Path)
            else None
        )
        self.audio_path = (
            result.get("audio")
            if isinstance(result.get("audio"), Path)
            else None
        )
        self.transcript_path = None
        self.video_count = int(result.get("video_count", 0))
        self.voiceover_count = int(result.get("voiceover_count", 0))
        errors = result.get("errors", {})
        self.discovery_errors = dict(errors) if isinstance(errors, dict) else {}
        if self.project_path:
            self.output_path = self.project_path / "output_simple.xml"
            self.sequence_name = f"{self.project_path.name} - Simple Cut"
            self.stage = Stage.SETUP
            self.setup_page.showNotice("")
            self.welcome_page.setNotice("")
            self._log(f"Đã nạp project: {self.project_path}", "success")

    def _browse_source(self, key: str) -> None:
        if self.busy_kind:
            return
        initial = str(self.project_path or Path.cwd())
        selected = ""
        if key == "content":
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "Chọn content DOCX",
                initial,
                "Word document (*.docx);;Tất cả file (*)",
            )
        elif key == "transcript":
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "Chọn transcript TXT",
                initial,
                "Transcript (*.txt);;Tất cả file (*)",
            )
        else:
            title = (
                "Chọn thư mục video"
                if key == "video"
                else "Chọn thư mục voice-over"
            )
            selected = QFileDialog.getExistingDirectory(self, title, initial)
        if not selected:
            return
        path = Path(selected).resolve()
        self._invalidate_review()
        self.discovery_errors.pop(key, None)
        if key == "content":
            self.content_path = path
        elif key == "video":
            self.video_path = path
            self.video_count = 0
        elif key == "audio":
            self.audio_path = path
            self.voiceover_count = audio_file_count(path)
        else:
            self.transcript_path = path
        self.last_error = ""
        self.setup_page.showNotice("")
        self._refresh_ui()

    def _clear_source(self, key: str) -> None:
        if self.busy_kind or key != "transcript":
            return
        self._invalidate_review()
        self.transcript_path = None
        self.discovery_errors.pop("transcript", None)
        self.last_error = ""
        self.setup_page.showNotice("")
        self._refresh_ui()

    def _browse_output(self) -> None:
        if self.busy_kind:
            return
        fallback = (
            self.project_path / "output_simple.xml"
            if self.project_path
            else Path.cwd() / "output_simple.xml"
        )
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Chọn XML đầu ra",
            str(self.output_path or fallback),
            "Premiere XML (*.xml);;Tất cả file (*)",
        )
        if selected:
            path = Path(selected).resolve()
            self.output_path = (
                path if path.suffix.casefold() == ".xml" else path.with_suffix(".xml")
            )
            self._refresh_ui()

    def _set_sequence_name(self, value: str) -> None:
        self.sequence_name = value
        self._refresh_action_bar()

    def _input_signature(self) -> tuple[str, str, str, str] | None:
        if not (self.content_path and self.video_path and self.audio_path):
            return None
        return (
            normalized_path(self.content_path),
            normalized_path(self.video_path),
            normalized_path(self.audio_path),
            normalized_path(self.transcript_path),
        )

    def _invalidate_review(self) -> None:
        self._next_job_id()
        self.verified_signature = None
        self.review_components = []
        self.review_content = None
        self.last_summary = None
        if self.project_path:
            self.stage = Stage.SETUP

    def _field_states(self) -> dict[str, tuple[str, str]]:
        states: dict[str, tuple[str, str]] = {}
        specifications = {
            "content": (self.content_path, "file", ".docx"),
            "video": (self.video_path, "directory", ""),
            "audio": (self.audio_path, "directory", ""),
            "transcript": (self.transcript_path, "file", ".txt"),
        }
        for key, (path, kind, suffix) in specifications.items():
            if path is None:
                if key == "transcript":
                    states[key] = ("optional", "")
                    continue
                error = self.discovery_errors.get(key, "")
                states[key] = ("error" if error else "warning", error)
            elif suffix and path.suffix.casefold() != suffix:
                states[key] = ("error", f"File phải có đuôi {suffix}")
            elif kind == "file" and not path.is_file():
                states[key] = ("error", f"Không tìm thấy file: {path}")
            elif kind == "directory" and not path.is_dir():
                states[key] = ("error", f"Không tìm thấy thư mục: {path}")
            else:
                states[key] = ("ready", "")
        return states

    def _all_sources_ready(self) -> bool:
        states = self._field_states()
        return bool(
            self.project_path
            and self.project_path.is_dir()
            and all(
                states[key][0] == "ready"
                for key in ("content", "video", "audio")
            )
            and states["transcript"][0] in {"ready", "optional"}
        )

    def _output_ready(self) -> bool:
        return bool(
            self.output_path
            and self.output_path.parent.is_dir()
            and self.sequence_name.strip()
        )

    def _start_review(self) -> None:
        if not self._all_sources_ready() or not self.content_path or not self.audio_path:
            return
        content_snapshot = self.content_path
        audio_snapshot = self.audio_path
        transcript_snapshot = self.transcript_path
        signature = self._input_signature()
        self._log(f"Đang kiểm tra cấu trúc: {content_snapshot}")

        def review() -> object:
            voiceover_count = audio_file_count(audio_snapshot)
            transcript_count = 0
            transcript_confidence = 0.0
            if transcript_snapshot is not None:
                _tokens, placements = tokens_from_transcript(
                    content_snapshot,
                    transcript_snapshot,
                    voiceover_count,
                    60,
                )
                transcript_count = len(placements)
                transcript_confidence = min(
                    placement.confidence for placement in placements
                )
            return {
                "signature": signature,
                "content": content_snapshot,
                "components": inspect_docx_components(content_snapshot),
                "voiceovers": voiceover_count,
                "transcripts": transcript_count,
                "transcript_confidence": transcript_confidence,
            }

        self._run_worker("review", review, self._review_loaded)

    def _review_loaded(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        if result.get("signature") != self._input_signature():
            self._log("Đã bỏ qua kết quả review thuộc nguồn cũ.", "warning")
            return
        components = result.get("components", [])
        self.review_components = (
            list(components) if isinstance(components, list) else []
        )
        self.review_content = (
            result.get("content")
            if isinstance(result.get("content"), Path)
            else None
        )
        self.voiceover_count = int(result.get("voiceovers", 0))
        if self.review_content:
            self.review_page.structure.load(
                self.review_content, self.review_components
            )
        self.stage = Stage.REVIEW
        self._log(
            f"Đã đọc {len(self.review_components)} thành phần từ DOCX.",
            "success",
        )
        transcript_count = int(result.get("transcripts", 0))
        if transcript_count:
            confidence = float(result.get("transcript_confidence", 0.0))
            self._log(
                f"Đã căn chỉnh {transcript_count} block transcript; "
                f"độ khớp thấp nhất {confidence:.0%}.",
                "success",
            )

    def _review_selection_changed(self) -> None:
        selected, _total, _all_ready = (
            self.review_page.structure.selectionStats()
        )
        self.review_page.updateSummary(
            self.review_components, self.voiceover_count, selected
        )
        self._refresh_action_bar()

    def _confirm_review(self) -> None:
        selected, total, all_ready = self.review_page.structure.selectionStats()
        if not total or not all_ready or selected != total:
            self._show_error_dialog(
                "Chưa thể xác nhận",
                "Hãy chọn đủ tất cả thành phần hợp lệ trước khi tiếp tục.",
            )
            return
        self.verified_signature = self._input_signature()
        self.stage = Stage.READY
        self._log(f"Đã xác nhận {total} thành phần.", "success")
        self.setup_page.showNotice("")
        self._refresh_ui()

    def _cancel_review(self) -> None:
        self.verified_signature = None
        self.stage = Stage.SETUP
        self._log(
            "Đã quay lại chỉnh nguồn; cấu trúc chưa được xác nhận.", "warning"
        )
        self._refresh_ui()

    def _start_generation(self) -> None:
        if (
            self.verified_signature != self._input_signature()
            or not self._all_sources_ready()
            or not self._output_ready()
        ):
            return
        assert self.project_path
        assert self.content_path
        assert self.video_path
        assert self.audio_path
        assert self.output_path
        snapshot = {
            "project_dir": self.project_path.resolve(),
            "content": self.content_path.resolve(),
            "video_dir": self.video_path.resolve(),
            "audio_dir": self.audio_path.resolve(),
            "transcript": (
                self.transcript_path.resolve() if self.transcript_path else None
            ),
            "output": self.output_path.resolve(),
            "sequence_name": self.sequence_name.strip(),
        }
        signature = self._input_signature()
        self.last_error = ""
        self.setup_page.showNotice("")
        self._log("Bắt đầu kiểm tra media và tạo XML...")

        def generate() -> object:
            return {
                "signature": signature,
                "summary": generate_simple_project_xml(**snapshot),
            }

        self._run_worker("generation", generate, self._generation_succeeded)

    def _generation_succeeded(self, payload: object) -> None:
        result = payload if isinstance(payload, dict) else {}
        if result.get("signature") != self._input_signature():
            self._log("Đã bỏ qua kết quả export thuộc nguồn cũ.", "warning")
            return
        summary = result.get("summary")
        if not isinstance(summary, SimpleGenerationSummary):
            self._worker_failed(
                "generation", "Backend không trả về kết quả hợp lệ"
            )
            return
        self.last_summary = summary
        self.stage = Stage.SUCCESS
        self.result_page.setSummary(summary)
        self._log(
            f"Hoàn tất: {summary.video_cut_count} video cut, "
            f"{summary.narration_count} voice-over, "
            f"{summary.duration_seconds:.2f}s",
            "success",
        )
        self._log(f"Output: {summary.output}", "success")

    def _return_to_ready(self) -> None:
        self.stage = (
            Stage.READY
            if self.verified_signature == self._input_signature()
            else Stage.SETUP
        )
        self._refresh_ui()

    def _primary_action(self) -> None:
        if self.busy_kind:
            return
        if self.stage == Stage.SETUP:
            self._start_review()
        elif self.stage == Stage.REVIEW:
            self._confirm_review()
        elif self.stage == Stage.READY:
            self._start_generation()

    def _secondary_action(self) -> None:
        if self.busy_kind:
            return
        if self.stage == Stage.REVIEW:
            self._cancel_review()
        elif self.stage == Stage.READY:
            self.verified_signature = None
            self._start_review()

    def _refresh_ui(self) -> None:
        states = self._field_states()
        paths = {
            "content": self.content_path,
            "video": self.video_path,
            "audio": self.audio_path,
            "transcript": self.transcript_path,
        }
        for key, card in self.setup_page.source_cards.items():
            state, message = states[key]
            card.setPathState(paths[key], state, message)
            card.setBusy(bool(self.busy_kind))
        self.setup_page.summary.updateSummary(
            self.project_path, states, self.video_count, self.voiceover_count
        )
        self.setup_page.export.setValues(self.sequence_name, self.output_path)
        self.setup_page.export.setBusy(bool(self.busy_kind))

        if self.stage == Stage.WELCOME:
            self.pages.setCurrentWidget(self.welcome_page)
        elif self.stage in {Stage.SETUP, Stage.READY}:
            self.pages.setCurrentWidget(self.setup_page)
            if self.stage == Stage.READY:
                self.setup_page.header.setCopy(
                    "Bước 4",
                    "Sẵn sàng xuất XML",
                    "",
                )
            else:
                self.setup_page.header.setCopy(
                    "Bước 2",
                    "Chuẩn bị dữ liệu",
                    "",
                )
        elif self.stage == Stage.REVIEW:
            self.pages.setCurrentWidget(self.review_page)
        else:
            self.pages.setCurrentWidget(self.result_page)

        self.sidebar.updateState(
            self.project_path,
            self._project_status_text(states),
            self._step_states(states),
        )
        self.sidebar.change_button.setDisabled(bool(self.busy_kind))
        self.welcome_page.choose_button.setDisabled(bool(self.busy_kind))
        self._refresh_action_bar()

    def _project_status_text(
        self, states: dict[str, tuple[str, str]]
    ) -> str:
        if not self.project_path:
            return "Cần chọn project"
        if any(state == "error" for state, _ in states.values()):
            return "Nguồn cần xử lý"
        if not all(
            states[key][0] == "ready" for key in ("content", "video", "audio")
        ):
            return "Còn thiếu nguồn"
        if self.stage == Stage.SUCCESS:
            return "Đã xuất XML"
        if self.verified_signature == self._input_signature():
            return "Sẵn sàng xuất"
        return ""

    def _step_states(
        self, states: dict[str, tuple[str, str]]
    ) -> list[str]:
        if not self.project_path:
            return ["current", "pending", "pending", "pending"]
        source_error = any(state == "error" for state, _ in states.values())
        source_ready = all(
            states[key][0] == "ready" for key in ("content", "video", "audio")
        ) and states["transcript"][0] in {"ready", "optional"}
        result = ["complete", "pending", "pending", "pending"]
        if not source_ready:
            result[1] = "error" if source_error else "current"
            return result
        if self.stage == Stage.SETUP:
            result[1] = "current"
        elif self.stage == Stage.REVIEW:
            result[1] = "complete"
            result[2] = "current"
        elif (
            self.verified_signature == self._input_signature()
            or self.stage == Stage.SUCCESS
        ):
            result[1] = "complete"
            result[2] = "complete"
            result[3] = (
                "complete" if self.stage == Stage.SUCCESS else "current"
            )
        else:
            result[2] = "current"
        return result

    def _refresh_action_bar(self) -> None:
        busy = bool(self.busy_kind)
        self.action_bar.setVisible(
            self.stage not in {Stage.WELCOME, Stage.SUCCESS}
        )
        if self.stage == Stage.SETUP:
            enabled = self._all_sources_ready()
            status = (
                self._busy_text()
                if busy
                else "Sẵn sàng kiểm tra cấu trúc."
                if enabled
                else self._blocked_reason()
            )
            self.action_bar.configure(
                status, "Kiểm tra cấu trúc", enabled=enabled, busy=busy
            )
        elif self.stage == Stage.REVIEW:
            selected, total, all_ready = (
                self.review_page.structure.selectionStats()
            )
            enabled = bool(total and all_ready and selected == total)
            status = (
                self._busy_text()
                if busy
                else f"Đã chọn {selected}/{total} thành phần."
                if enabled
                else "Chọn đủ các thành phần hợp lệ để xác nhận."
            )
            self.action_bar.configure(
                status,
                "Xác nhận cấu trúc",
                "Quay lại",
                enabled,
                busy,
            )
        elif self.stage == Stage.READY:
            enabled = (
                self.verified_signature == self._input_signature()
                and self._all_sources_ready()
                and self._output_ready()
            )
            status = (
                self._busy_text()
                if busy
                else "Sẵn sàng tạo XML."
                if enabled
                else "Nhập tên sequence và chọn vị trí lưu hợp lệ."
            )
            self.action_bar.configure(
                status, "Xuất XML", "Kiểm tra lại", enabled, busy
            )

    def _busy_text(self) -> str:
        return {
            "default_project": "Đang tìm project gần đây...",
            "project": "Đang quét project và tìm nguồn...",
            "review": "Đang đọc cấu trúc DOCX...",
            "generation": "Đang kiểm tra media và tạo XML...",
        }.get(self.busy_kind or "", "Đang xử lý...")

    def _blocked_reason(self) -> str:
        if not self.project_path:
            return "Chọn project để bắt đầu."
        for key, (state, message) in self._field_states().items():
            if state == "error":
                return message or "Có nguồn dữ liệu chưa hợp lệ."
            if key != "transcript" and state != "ready":
                return "Chọn đủ content, video và voice-over."
        return "Cần kiểm tra cấu trúc DOCX."

    def _toggle_log(self) -> None:
        visible = not self.log_panel.isVisible()
        self.log_panel.setVisible(visible)
        self.sidebar.log_button.setText(
            "Ẩn nhật ký" if visible else "Nhật ký"
        )

    def _log(self, message: str, level: str = "info") -> None:
        self.log_panel.append(message, level)

    def _copy_text(self, value: str) -> None:
        if value:
            QApplication.clipboard().setText(value)
            self._log("Đã copy đường dẫn vào clipboard.", "success")

    def _reveal_path(self, raw: str) -> None:
        if not raw:
            return
        path = Path(raw).expanduser()
        target = path if path.exists() else path.parent
        if not target.exists():
            self._show_error_dialog("Không tìm thấy đường dẫn", str(path))
            return
        try:
            if IS_MACOS:
                command = (
                    ["open", "-R", str(path)]
                    if path.is_file()
                    else ["open", str(target)]
                )
                subprocess.Popen(command)
            elif os.name == "nt":
                if path.is_file():
                    # Explorer expects the select switch and a quoted path as
                    # separate command-line arguments. Combining them causes
                    # paths containing spaces to be quoted as a whole
                    # ("/select,C:\\Some Folder\\file.xml"), which Explorer
                    # does not parse reliably.
                    subprocess.Popen(
                        [
                            "explorer.exe",
                            "/select,",
                            os.path.normpath(str(path.resolve())),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        **hidden_subprocess_options(),
                    )
                else:
                    os.startfile(  # type: ignore[attr-defined]
                        os.path.normpath(str(target.resolve()))
                    )
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            self._show_error_dialog(
                f"Không thể mở {FILE_MANAGER}", str(exc)
            )

    def _open_review_document(self) -> None:
        if self.review_content:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.review_content))
            )

    def _copy_review_document(self) -> None:
        if self.review_content:
            self._copy_text(str(self.review_content))

    def _open_result(self) -> None:
        if self.last_summary:
            self._reveal_path(str(self.last_summary.output))

    def _copy_result(self) -> None:
        if self.last_summary:
            self._copy_text(str(self.last_summary.output))

    def _show_error_dialog(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def _escape(self) -> None:
        if self.log_panel.isVisible():
            self._toggle_log()
        elif self.stage == Stage.REVIEW:
            self.review_page.structure.collapseAllDetails()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.busy_kind and not self.close_when_idle:
            answer = QMessageBox.question(
                self,
                "Đang xử lý",
                "Tác vụ đang chạy và không thể dừng an toàn giữa chừng. "
                "Đóng cửa sổ và thoát ngay khi tác vụ hoàn tất?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.close_when_idle = True
                self.hide()
            event.ignore()
            return
        event.accept()


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("XML Cut Room")
    application.setOrganizationName("XML Cut Room")
    application.setStyle("Fusion")
    apply_light_palette(application)
    system_font = QFontDatabase.systemFont(
        QFontDatabase.SystemFont.GeneralFont
    )
    system_font.setPointSize(14)
    application.setFont(system_font)
    icon_path = bundled_asset("xmlcutroom-logo.png")
    if icon_path:
        application.setWindowIcon(QIcon(str(icon_path)))
    window = XmlGeneratorApp()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
