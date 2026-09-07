#!/usr/bin/env python3
"""Native macOS-style desktop interface for the Premiere XML generator."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from generate_simple_xml_v3 import (
    ArticleComponent,
    DEFAULT_CONTENT_NAME,
    DEFAULT_XML_NAME,
    GeneratorError,
    GenerationSummary,
    discover_audio_media,
    discover_video_media,
    find_named_child,
    generate_project_xml,
    inspect_docx_components,
    load_blueprint,
    resolve_default_project,
)


@dataclass(frozen=True)
class ColorTokens:
    window: str
    sidebar: str
    content: str
    elevated: str
    input: str
    hover: str
    selection: str
    selection_soft: str
    separator: str
    text: str
    secondary: str
    tertiary: str
    disabled: str
    accent: str
    success: str
    warning: str
    error: str


DARK_COLORS = ColorTokens(
    window="#1E1E1E",
    sidebar="#252526",
    content="#1C1C1E",
    elevated="#2C2C2E",
    input="#242426",
    hover="#323235",
    selection="#0A84FF",
    selection_soft="#174A72",
    separator="#38383A",
    text="#EBEBF0",
    secondary="#A1A1A6",
    tertiary="#7C7C80",
    disabled="#8A8A8F",
    accent="#0A84FF",
    success="#30D158",
    warning="#FF9F0A",
    error="#FF453A",
)

LIGHT_COLORS = ColorTokens(
    window="#F2F2F7",
    sidebar="#E9E9ED",
    content="#FFFFFF",
    elevated="#F2F2F7",
    input="#FFFFFF",
    hover="#E5E5EA",
    selection="#007AFF",
    selection_soft="#D9ECFF",
    separator="#D1D1D6",
    text="#1C1C1E",
    secondary="#636366",
    tertiary="#8E8E93",
    disabled="#6E6E73",
    accent="#007AFF",
    success="#1F7A35",
    warning="#C93400",
    error="#D70015",
)


class LayoutTokens:
    SIDEBAR_WIDTH = 220
    TOOLBAR_HEIGHT = 44
    ACTION_HEIGHT = 64
    CONTENT_PAD_X = 24
    CONTENT_PAD_Y = 20
    ROW_HEIGHT = 58
    SUMMARY_HEIGHT = 48
    CONTROL_HEIGHT = 32
    MIN_WIDTH = 960
    MIN_HEIGHT = 640
    LOG_HEIGHT = 170


class TypeTokens:
    UI = "SF Pro Text"
    DISPLAY = "SF Pro Display"
    MONO = "SF Mono"
    TITLE = (DISPLAY, 24, "bold")
    SECTION = (UI, 15, "bold")
    ROW = (UI, 13, "bold")
    BODY = (UI, 13)
    SECONDARY = (UI, 11)
    BUTTON = (UI, 13, "bold")
    MONO_SMALL = (MONO, 11)


WORKFLOW_STEPS = (
    "Chọn project",
    "Chuẩn bị dữ liệu",
    "Kiểm tra cấu trúc",
    "Xuất XML",
)


def detect_dark_mode() -> bool:
    override = os.environ.get("XML_CUT_ROOM_THEME", "").strip().casefold()
    if override in {"dark", "light"}:
        return override == "dark"
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )
            return result.stdout.strip().casefold() == "dark"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


def path_text(path: Path | str) -> str:
    return str(Path(path).expanduser())


def normalized_path(path: Path | None) -> str:
    return unicodedata.normalize("NFC", os.path.normcase(str(path))) if path else ""


def shorten_middle(value: str, length: int = 74) -> str:
    if len(value) <= length:
        return value
    left = max(12, (length - 3) // 2)
    return value[:left] + "..." + value[-(length - left - 3) :]


def component_is_ready(component: ArticleComponent) -> bool:
    if not component.present or component.text_block_count == 0:
        return False
    return component.key == "outro" or component.video_cut_count > 0


class Tooltip:
    def __init__(
        self,
        widget: tk.Misc,
        text: str | Callable[[], str],
        colors: ColorTokens,
    ) -> None:
        self.widget = widget
        self.text = text
        self.colors = colors
        self.tip: tk.Toplevel | None = None
        self.after_id: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._hide()
        self.after_id = self.widget.after(450, self._show)

    def _show(self) -> None:
        value = self.text() if callable(self.text) else self.text
        if not value or not self.widget.winfo_exists():
            return
        tip = tk.Toplevel(self.widget)
        self.tip = tip
        tip.wm_overrideredirect(True)
        tip.configure(bg=self.colors.separator)
        x = self.widget.winfo_rootx() + 4
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 7
        tip.geometry(f"+{x}+{y}")
        tk.Label(
            tip,
            text=value,
            bg=self.colors.elevated,
            fg=self.colors.text,
            font=TypeTokens.SECONDARY,
            padx=10,
            pady=7,
            justify="left",
            wraplength=520,
        ).pack(padx=1, pady=1)

    def _hide(self, _event: tk.Event | None = None) -> None:
        if self.after_id:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class Toolbar(tk.Frame):
    def __init__(self, parent: tk.Misc, app: "XmlGeneratorApp") -> None:
        super().__init__(
            parent,
            bg=app.colors.window,
            height=LayoutTokens.TOOLBAR_HEIGHT,
            highlightthickness=0,
        )
        self.app = app
        self.grid_propagate(False)
        self.grid_columnconfigure(2, weight=1)

        self.back_button = ttk.Button(
            self,
            text="‹",
            command=app._cancel_review,
            style="Icon.TButton",
            width=3,
            takefocus=True,
        )
        self.back_button.grid(row=0, column=0, padx=(12, 4), pady=6)
        Tooltip(self.back_button, "Quay lại", app.colors)

        ttk.Label(
            self,
            textvariable=app.project_name_var,
            style="ToolbarTitle.TLabel",
            anchor="w",
        ).grid(row=0, column=1, sticky="w", padx=(4, 8))

        open_button = ttk.Button(
            self,
            text="⌘O",
            command=app._browse_project,
            style="ToolbarIcon.TButton",
            width=4,
            takefocus=True,
        )
        open_button.grid(row=0, column=3, padx=(4, 2), pady=6)
        Tooltip(open_button, "Mở project (Command+O)", app.colors)

        log_button = ttk.Button(
            self,
            text="≡",
            command=app._toggle_log,
            style="ToolbarIcon.TButton",
            width=3,
            takefocus=True,
        )
        log_button.grid(row=0, column=4, padx=(2, 12), pady=6)
        Tooltip(log_button, "Hiện hoặc ẩn nhật ký (Command+L)", app.colors)

        tk.Frame(self, bg=app.colors.separator, height=1).place(
            relx=0, rely=1, relwidth=1, anchor="sw"
        )

    def set_can_go_back(self, enabled: bool) -> None:
        self.back_button.configure(state="normal" if enabled else "disabled")


class Sidebar(tk.Frame):
    def __init__(self, parent: tk.Misc, app: "XmlGeneratorApp") -> None:
        super().__init__(
            parent,
            bg=app.colors.sidebar,
            width=LayoutTokens.SIDEBAR_WIDTH,
            highlightthickness=0,
        )
        self.app = app
        self.grid_propagate(False)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)
        self.step_widgets: list[tuple[tk.Frame, tk.Label, tk.Label]] = []

        project_group = tk.Frame(self, bg=app.colors.sidebar)
        project_group.grid(row=0, column=0, sticky="ew", padx=12, pady=(18, 10))
        tk.Label(
            project_group,
            text="PROJECT",
            bg=app.colors.sidebar,
            fg=app.colors.tertiary,
            font=(TypeTokens.UI, 10, "bold"),
            anchor="w",
        ).pack(fill="x", padx=8, pady=(0, 5))
        project_row = tk.Frame(project_group, bg=app.colors.hover, padx=9, pady=7)
        project_row.pack(fill="x")
        tk.Label(
            project_row,
            text="▤",
            bg=app.colors.hover,
            fg=app.colors.accent,
            font=TypeTokens.BODY,
            width=2,
        ).pack(side="left")
        project_copy = tk.Frame(project_row, bg=app.colors.hover)
        project_copy.pack(side="left", fill="x", expand=True, padx=(5, 0))
        tk.Label(
            project_copy,
            textvariable=app.project_name_var,
            bg=app.colors.hover,
            fg=app.colors.text,
            font=TypeTokens.ROW,
            anchor="w",
        ).pack(fill="x")
        self.project_status = tk.Label(
            project_copy,
            textvariable=app.project_badge_var,
            bg=app.colors.hover,
            fg=app.colors.secondary,
            font=TypeTokens.SECONDARY,
            anchor="w",
        )
        self.project_status.pack(fill="x", pady=(1, 0))

        tk.Frame(self, bg=app.colors.separator, height=1).grid(
            row=1, column=0, sticky="ew", padx=12
        )

        steps = tk.Frame(self, bg=app.colors.sidebar)
        steps.grid(row=2, column=0, sticky="ew", padx=12, pady=(16, 0))
        tk.Label(
            steps,
            text="QUY TRÌNH",
            bg=app.colors.sidebar,
            fg=app.colors.tertiary,
            font=(TypeTokens.UI, 10, "bold"),
            anchor="w",
        ).pack(fill="x", padx=8, pady=(0, 5))
        for title in WORKFLOW_STEPS:
            row = tk.Frame(steps, bg=app.colors.sidebar, padx=8, pady=7)
            row.pack(fill="x", pady=1)
            icon = tk.Label(
                row,
                text="○",
                bg=app.colors.sidebar,
                fg=app.colors.tertiary,
                font=(TypeTokens.UI, 12, "bold"),
                width=2,
            )
            icon.pack(side="left")
            label = tk.Label(
                row,
                text=title,
                bg=app.colors.sidebar,
                fg=app.colors.secondary,
                font=TypeTokens.BODY,
                anchor="w",
            )
            label.pack(side="left", fill="x", expand=True, padx=(6, 0))
            self.step_widgets.append((row, icon, label))

        tk.Label(
            self,
            text="Phiên bản 3.0",
            bg=app.colors.sidebar,
            fg=app.colors.tertiary,
            font=TypeTokens.SECONDARY,
            anchor="w",
        ).grid(row=5, column=0, sticky="sw", padx=20, pady=16)

        tk.Frame(self, bg=app.colors.separator, width=1).place(
            relx=1, rely=0, relheight=1, anchor="ne"
        )

    def update_states(self, states: list[str]) -> None:
        state_colors = {
            "pending": self.app.colors.tertiary,
            "current": self.app.colors.accent,
            "complete": self.app.colors.success,
            "error": self.app.colors.error,
        }
        state_icons = {
            "pending": "○",
            "current": "●",
            "complete": "✓",
            "error": "!",
        }
        for state, (row, icon, label) in zip(states, self.step_widgets, strict=True):
            background = (
                self.app.colors.selection_soft
                if state == "current"
                else self.app.colors.sidebar
            )
            color = state_colors[state]
            row.configure(bg=background)
            icon.configure(text=state_icons[state], bg=background, fg=color)
            label.configure(
                bg=background,
                fg=self.app.colors.text if state == "current" else color,
                font=(TypeTokens.UI, 13, "bold" if state == "current" else "normal"),
            )


class SummaryBar(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        app: "XmlGeneratorApp",
        components: list[ArticleComponent],
        voiceovers: int,
    ) -> None:
        super().__init__(
            parent,
            bg=app.colors.elevated,
            height=LayoutTokens.SUMMARY_HEIGHT,
            highlightthickness=0,
        )
        self.app = app
        self.pack_propagate(False)
        self.selected_var = tk.StringVar(value="0/8 đã chọn")
        self.select_all_var = tk.BooleanVar(value=False)

        metrics = tk.Frame(self, bg=app.colors.elevated)
        metrics.pack(side="left", fill="y", padx=(14, 0))
        total_video = sum(item.video_cut_count for item in components)
        total_voice = voiceovers or sum(item.text_block_count for item in components)
        values: list[str | tk.StringVar] = [
            f"{len(components)} thành phần",
            f"{total_video} video cut",
            f"{total_voice} voice-over",
            self.selected_var,
        ]
        for index, value in enumerate(values):
            if index:
                tk.Frame(metrics, bg=app.colors.separator, width=1, height=16).pack(
                    side="left", padx=10, pady=16
                )
            tk.Label(
                metrics,
                textvariable=value if isinstance(value, tk.StringVar) else None,
                text="" if isinstance(value, tk.StringVar) else value,
                bg=app.colors.elevated,
                fg=app.colors.text if index == 0 else app.colors.secondary,
                font=TypeTokens.BODY,
            ).pack(side="left", pady=14)

        controls = tk.Frame(self, bg=app.colors.elevated)
        controls.pack(side="right", fill="y", padx=(8, 12))
        valid = all(component_is_ready(item) for item in components)
        tk.Label(
            controls,
            text="✓ Dữ liệu hợp lệ" if valid else "! Cần kiểm tra",
            bg=app.colors.elevated,
            fg=app.colors.success if valid else app.colors.warning,
            font=TypeTokens.SECONDARY,
        ).pack(side="left", padx=(0, 13), pady=14)
        ttk.Checkbutton(
            controls,
            text="Chọn tất cả",
            variable=self.select_all_var,
            command=app._select_all,
            style="Summary.TCheckbutton",
            takefocus=True,
        ).pack(side="left", pady=11)

    def set_selected(self, selected: int, total: int) -> None:
        self.selected_var.set(f"{selected}/{total} đã chọn")
        self.select_all_var.set(bool(total and selected == total))


class StructureList(tk.Frame):
    """Native table/tree with one owned scrollbar and inline detail rows."""

    def __init__(
        self,
        parent: tk.Misc,
        app: "XmlGeneratorApp",
        components: list[ArticleComponent],
    ) -> None:
        super().__init__(parent, bg=app.colors.content, highlightthickness=0)
        self.app = app
        self.components = components
        self.iid_to_index: dict[str, int] = {}
        self.parent_iids: list[str] = []
        self.hover_iid = ""
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            self,
            columns=("check", "name", "preview", "video", "voice", "state"),
            show="headings",
            selectmode="none",
            style="Structure.Treeview",
            takefocus=True,
        )
        self.tree.heading("check", text="")
        self.tree.heading("name", text="Thành phần", anchor="w")
        self.tree.heading("preview", text="Nội dung", anchor="w")
        self.tree.heading("video", text="Video", anchor="e")
        self.tree.heading("voice", text="Voice-over", anchor="e")
        self.tree.heading("state", text="", anchor="center")
        self.tree.column("check", width=42, minwidth=42, stretch=False, anchor="center")
        self.tree.column("name", width=135, minwidth=115, stretch=False, anchor="w")
        self.tree.column("preview", width=250, minwidth=170, stretch=False, anchor="w")
        self.tree.column("video", width=72, minwidth=68, stretch=False, anchor="e")
        self.tree.column("voice", width=98, minwidth=92, stretch=False, anchor="e")
        self.tree.column("state", width=42, minwidth=40, stretch=False, anchor="center")

        scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.tree.yview,
            style="CutRoom.Vertical.TScrollbar",
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.scrollbar = scrollbar

        self.tree.tag_configure("valid", foreground=app.colors.text)
        self.tree.tag_configure("invalid", foreground=app.colors.error)
        self.tree.tag_configure(
            "checked", background=app.colors.selection, foreground="#FFFFFF"
        )
        self.tree.tag_configure("hover", background=app.colors.hover)
        self.tree.tag_configure(
            "detail", background=app.colors.elevated, foreground=app.colors.secondary
        )
        self.tree.tag_configure(
            "detail_heading", background=app.colors.elevated, foreground=app.colors.text
        )

        self._load()
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", self._clear_hover)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Return>", self._on_return)
        self.bind("<Configure>", self._resize_columns)

    def _resize_columns(self, event: tk.Event) -> None:
        fixed_width = 42 + 135 + 72 + 98 + 42 + 16
        self.tree.column("preview", width=max(170, event.width - fixed_width))

    def _load(self) -> None:
        for index, component in enumerate(self.components):
            ready = component_is_ready(component)
            preview_values = list(component.timecodes[:2])
            hidden = component.video_cut_count - len(preview_values)
            if preview_values:
                preview = ", ".join(preview_values)
                if hidden:
                    preview += f" và {hidden} đoạn khác"
            else:
                preview = f"{component.text_block_count} khối lời đọc"
            iid = f"component-{index}"
            self.parent_iids.append(iid)
            self.iid_to_index[iid] = index
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    "☐",
                    component.title,
                    preview,
                    f"{component.video_cut_count} video",
                    f"{component.text_block_count} voice-over",
                    "✓  ›" if ready else "!  ›",
                ),
                tags=("valid" if ready else "invalid",),
                open=False,
            )
            if component.timecodes:
                self.tree.insert(
                    iid,
                    "end",
                    values=(
                        "",
                        "Video cut",
                        f"{component.video_cut_count} đoạn timecode",
                        f"{component.video_cut_count} đoạn",
                        "",
                        "",
                    ),
                    tags=("detail_heading",),
                )
                for offset in range(0, len(component.timecodes), 3):
                    group = component.timecodes[offset : offset + 3]
                    self.tree.insert(
                        iid,
                        "end",
                        values=("", "", "    ".join(group), "", "", ""),
                        tags=("detail",),
                    )
            self.tree.insert(
                iid,
                "end",
                values=(
                    "",
                    "Voice-over",
                    f"Đã phát hiện {component.text_block_count} khối lời đọc",
                    "",
                    f"{component.text_block_count} khối",
                    "",
                ),
                tags=("detail",),
            )

    def _main_iid_at(self, y: int) -> str:
        iid = self.tree.identify_row(y)
        return iid if iid in self.iid_to_index else ""

    def _on_click(self, event: tk.Event) -> str | None:
        iid = self._main_iid_at(event.y)
        if not iid:
            return None
        column = self.tree.identify_column(event.x)
        if column == "#6":
            self.tree.item(iid, open=not bool(self.tree.item(iid, "open")))
            self._refresh_iid(iid)
            return "break"
        self.toggle_iid(iid)
        self.tree.focus(iid)
        return "break"

    def _on_space(self, _event: tk.Event) -> str:
        iid = self.tree.focus()
        if iid in self.iid_to_index:
            self.toggle_iid(iid)
        return "break"

    def _on_return(self, _event: tk.Event) -> str:
        iid = self.tree.focus()
        if iid in self.iid_to_index:
            self.tree.item(iid, open=not bool(self.tree.item(iid, "open")))
        return "break"

    def _on_motion(self, event: tk.Event) -> None:
        iid = self._main_iid_at(event.y)
        if iid == self.hover_iid:
            return
        old = self.hover_iid
        self.hover_iid = iid
        if old:
            self._refresh_iid(old)
        if iid:
            self._refresh_iid(iid)

    def _clear_hover(self, _event: tk.Event | None = None) -> None:
        old = self.hover_iid
        self.hover_iid = ""
        if old:
            self._refresh_iid(old)

    def _refresh_iid(self, iid: str) -> None:
        index = self.iid_to_index[iid]
        component = self.components[index]
        variable = self.app.review_variables[index]
        tags: tuple[str, ...]
        if variable.get():
            tags = ("checked",)
        elif iid == self.hover_iid:
            tags = ("hover",)
        else:
            tags = ("valid" if component_is_ready(component) else "invalid",)
        values = list(self.tree.item(iid, "values"))
        values[0] = "☑" if variable.get() else "☐"
        values[-1] = (
            "✓" if component_is_ready(component) else "!"
        ) + ("  ▾" if self.tree.item(iid, "open") else "  ›")
        self.tree.item(iid, values=values, tags=tags)

    def toggle_iid(self, iid: str) -> None:
        index = self.iid_to_index[iid]
        component = self.components[index]
        if not component_is_ready(component):
            self.app._show_error(f"{component.title} chưa đủ dữ liệu để xác nhận")
            return
        variable = self.app.review_variables[index]
        variable.set(not variable.get())
        self._refresh_iid(iid)
        self.app._update_confirm_state()

    def refresh_all(self) -> None:
        for iid in self.parent_iids:
            self._refresh_iid(iid)

    def collapse_all(self) -> bool:
        changed = False
        for iid in self.parent_iids:
            if self.tree.item(iid, "open"):
                self.tree.item(iid, open=False)
                changed = True
        return changed


class BottomActionBar(tk.Frame):
    def __init__(self, parent: tk.Misc, app: "XmlGeneratorApp") -> None:
        super().__init__(
            parent,
            bg=app.colors.window,
            height=LayoutTokens.ACTION_HEIGHT,
            highlightthickness=0,
        )
        self.app = app
        self.grid_propagate(False)
        self.grid_columnconfigure(0, weight=1)

        tk.Frame(self, bg=app.colors.separator, height=1).place(
            relx=0, rely=0, relwidth=1, anchor="nw"
        )
        left = tk.Frame(self, bg=app.colors.window)
        left.grid(row=0, column=0, sticky="w", padx=16, pady=12)
        self.status_label = ttk.Label(
            left,
            textvariable=app.action_status_var,
            style="ActionStatus.TLabel",
        )
        self.status_label.pack(anchor="w")
        self.progress = ttk.Progressbar(
            left,
            mode="indeterminate",
            style="CutRoom.Horizontal.TProgressbar",
            length=220,
        )

        self.buttons = tk.Frame(self, bg=app.colors.window)
        self.buttons.grid(row=0, column=1, sticky="e", padx=16, pady=15)

    def show_setup(self) -> None:
        for child in self.buttons.winfo_children():
            child.grid_forget()
        self.app.review_button.grid(row=0, column=0, padx=(0, 8))
        self.app.generate_button.grid(row=0, column=1)

    def show_review(self) -> None:
        for child in self.buttons.winfo_children():
            child.grid_forget()
        self.app.back_action_button.grid(row=0, column=0, padx=(0, 8))
        self.app.confirm_button.grid(row=0, column=1)


class LogPanel(tk.Frame):
    def __init__(self, parent: tk.Misc, app: "XmlGeneratorApp") -> None:
        super().__init__(parent, bg=app.colors.elevated, highlightthickness=0)
        self.app = app
        header = tk.Frame(self, bg=app.colors.elevated, height=36)
        header.pack(fill="x")
        header.pack_propagate(False)
        ttk.Label(header, text="Nhật ký", style="LogHeader.TLabel").pack(
            side="left", padx=12, pady=9
        )
        ttk.Button(
            header,
            text="Xóa",
            command=self.clear,
            style="Small.TButton",
            takefocus=True,
        ).pack(side="right", padx=(4, 10), pady=5)
        ttk.Button(
            header,
            text="Copy",
            command=self.copy,
            style="Small.TButton",
            takefocus=True,
        ).pack(side="right", pady=5)
        tk.Frame(self, bg=app.colors.separator, height=1).pack(fill="x")
        self.text = tk.Text(
            self,
            bg=app.colors.input,
            fg=app.colors.secondary,
            insertbackground=app.colors.text,
            selectbackground=app.colors.selection,
            selectforeground="#FFFFFF",
            font=TypeTokens.MONO_SMALL,
            borderwidth=0,
            highlightthickness=0,
            padx=12,
            pady=8,
            wrap="word",
            state="disabled",
        )
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("success", foreground=app.colors.success)
        self.text.tag_configure("warning", foreground=app.colors.warning)
        self.text.tag_configure("error", foreground=app.colors.error)
        self.text.tag_configure("info", foreground=app.colors.secondary)

    def append(self, message: str, level: str) -> None:
        icons = {"success": "✓", "warning": "!", "error": "!", "info": "i"}
        self.text.configure(state="normal")
        self.text.insert("end", f"{icons.get(level, 'i')}  {message}\n", level)
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def copy(self) -> None:
        value = self.text.get("1.0", "end-1c")
        if value:
            self.app._copy_value(value, log_event=False)


@dataclass
class FieldView:
    key: str
    variable: tk.StringVar
    entry: ttk.Entry
    helper_label: tk.Label
    status_label: tk.Label
    default_helper: str
    kind: str


class XmlGeneratorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.dark_mode = detect_dark_mode()
        self.colors = DARK_COLORS if self.dark_mode else LIGHT_COLORS
        self.root.title("XML Cut Room")
        self.root.geometry("1280x800")
        self.root.minsize(LayoutTokens.MIN_WIDTH, LayoutTokens.MIN_HEIGHT)
        self.root.configure(bg=self.colors.window)
        self._configure_native_window()

        self.project_var = tk.StringVar()
        self.content_var = tk.StringVar()
        self.xml_var = tk.StringVar()
        self.video_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.sequence_var = tk.StringVar(value="Bài 0509")
        self.status_var = tk.StringVar(value="Chưa chọn project")
        self.action_status_var = tk.StringVar(value="Chọn project để bắt đầu")
        self.review_footer_var = tk.StringVar(value="Đã chọn 0 trong 8 thành phần")
        self.project_name_var = tk.StringVar(value="Chưa có project")
        self.project_badge_var = tk.StringVar(value="Chưa sẵn sàng")

        self.verified_content: Path | None = None
        self.is_running = False
        self.is_review_loading = False
        self.generation_success = False
        self.generation_error = False
        self.current_page = "setup"
        self.log_expanded = False
        self.field_views: dict[str, FieldView] = {}
        self.review_components: list[ArticleComponent] = []
        self.review_variables: list[tk.BooleanVar] = []
        self.review_path: Path | None = None
        self.review_page: tk.Frame | None = None
        self.structure_list: StructureList | None = None
        self.summary_bar: SummaryBar | None = None
        self._toast: tk.Toplevel | None = None
        self._suspend_trace = False
        self._worker_results: queue.Queue[tuple[str, object]] = queue.Queue()

        self._configure_ttk()
        self._build()
        for variable in (
            self.project_var,
            self.content_var,
            self.xml_var,
            self.video_var,
            self.audio_var,
            self.output_var,
            self.sequence_var,
        ):
            variable.trace_add("write", self._inputs_changed)
        self._bind_shortcuts()
        self._load_initial_project()
        self._update_generate_state()

    def _configure_native_window(self) -> None:
        if sys.platform != "darwin":
            return
        try:
            self.root.tk.call(
                "tk::unsupported::MacWindowStyle",
                "style",
                self.root._w,
                "document",
                "closeBox collapseBox resizable",
            )
        except tk.TclError:
            pass

    def _configure_ttk(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        c = self.colors
        style.configure("TFrame", background=c.content)
        style.configure(
            "TLabel", background=c.content, foreground=c.text, font=TypeTokens.BODY
        )
        style.configure(
            "ToolbarTitle.TLabel",
            background=c.window,
            foreground=c.text,
            font=TypeTokens.ROW,
        )
        style.configure(
            "ActionStatus.TLabel",
            background=c.window,
            foreground=c.secondary,
            font=TypeTokens.SECONDARY,
        )
        style.configure(
            "LogHeader.TLabel",
            background=c.elevated,
            foreground=c.text,
            font=TypeTokens.ROW,
        )
        style.configure(
            "TButton",
            background=c.elevated,
            foreground=c.text,
            bordercolor=c.separator,
            lightcolor=c.elevated,
            darkcolor=c.elevated,
            focuscolor=c.accent,
            focusthickness=1,
            padding=(14, 6),
            font=TypeTokens.BUTTON,
        )
        style.map(
            "TButton",
            background=[("active", c.hover), ("pressed", c.input)],
            foreground=[("disabled", c.disabled)],
            bordercolor=[("focus", c.accent), ("active", c.secondary)],
        )
        style.configure(
            "Primary.TButton",
            background=c.accent,
            foreground="#FFFFFF",
            bordercolor=c.accent,
            lightcolor=c.accent,
            darkcolor=c.accent,
            padding=(16, 6),
            font=TypeTokens.BUTTON,
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", c.elevated),
                ("pressed", "#0068D7"),
                ("active", c.selection),
            ],
            foreground=[("disabled", c.disabled)],
            bordercolor=[("disabled", c.separator), ("focus", c.text)],
            lightcolor=[("disabled", c.elevated)],
            darkcolor=[("disabled", c.elevated)],
        )
        style.configure(
            "Icon.TButton",
            padding=(8, 2),
            font=(TypeTokens.UI, 20),
            background=c.window,
            bordercolor=c.window,
        )
        style.configure(
            "ToolbarIcon.TButton",
            padding=(7, 3),
            font=(TypeTokens.UI, 12, "bold"),
            background=c.window,
            bordercolor=c.window,
        )
        style.configure(
            "Small.TButton",
            padding=(9, 3),
            font=TypeTokens.SECONDARY,
            background=c.elevated,
        )
        style.configure(
            "Field.TEntry",
            fieldbackground=c.input,
            foreground=c.text,
            insertcolor=c.text,
            bordercolor=c.separator,
            lightcolor=c.separator,
            darkcolor=c.separator,
            padding=(9, 7),
            font=TypeTokens.MONO_SMALL,
        )
        style.configure("Valid.Field.TEntry", bordercolor=c.success)
        style.configure("Error.Field.TEntry", bordercolor=c.error)
        for entry_style, border in (
            ("Field.TEntry", c.separator),
            ("Valid.Field.TEntry", c.success),
            ("Error.Field.TEntry", c.error),
        ):
            style.map(
                entry_style,
                bordercolor=[("focus", c.accent), ("active", border)],
                lightcolor=[("focus", c.accent)],
                darkcolor=[("focus", c.accent)],
            )
        style.configure(
            "Summary.TCheckbutton",
            background=c.elevated,
            foreground=c.text,
            font=TypeTokens.BODY,
            padding=(4, 2),
            focuscolor=c.accent,
        )
        style.map(
            "Summary.TCheckbutton",
            background=[("active", c.elevated)],
            foreground=[("disabled", c.disabled)],
        )
        style.configure(
            "Structure.Treeview",
            background=c.content,
            fieldbackground=c.content,
            foreground=c.text,
            bordercolor=c.separator,
            lightcolor=c.separator,
            darkcolor=c.separator,
            relief="flat",
            rowheight=LayoutTokens.ROW_HEIGHT,
            font=TypeTokens.BODY,
        )
        style.configure(
            "Structure.Treeview.Heading",
            background=c.elevated,
            foreground=c.secondary,
            bordercolor=c.separator,
            relief="flat",
            padding=(7, 6),
            font=TypeTokens.SECONDARY,
        )
        style.map(
            "Structure.Treeview.Heading",
            background=[("active", c.hover)],
        )
        style.configure(
            "CutRoom.Vertical.TScrollbar",
            background=c.elevated,
            troughcolor=c.content,
            bordercolor=c.content,
            arrowcolor=c.tertiary,
            width=11,
        )
        style.configure(
            "CutRoom.Horizontal.TProgressbar",
            background=c.accent,
            troughcolor=c.separator,
            bordercolor=c.window,
            lightcolor=c.accent,
            darkcolor=c.accent,
            thickness=3,
        )

    def _build(self) -> None:
        shell = tk.Frame(self.root, bg=self.colors.window)
        shell.pack(fill="both", expand=True)
        shell.grid_rowconfigure(0, weight=1)
        shell.grid_columnconfigure(1, weight=1)
        self.shell = shell

        self.sidebar = Sidebar(shell, self)
        self.sidebar.grid(row=0, column=0, sticky="nsew")

        workspace = tk.Frame(shell, bg=self.colors.content)
        workspace.grid(row=0, column=1, sticky="nsew")
        workspace.grid_columnconfigure(0, weight=1)
        workspace.grid_rowconfigure(1, weight=1)
        self.workspace = workspace

        self.toolbar = Toolbar(workspace, self)
        self.toolbar.grid(row=0, column=0, sticky="ew")

        self.main_pane = tk.PanedWindow(
            workspace,
            orient="vertical",
            bg=self.colors.separator,
            sashwidth=5,
            sashrelief="flat",
            borderwidth=0,
        )
        self.main_pane.grid(row=1, column=0, sticky="nsew")
        self.view_host = tk.Frame(self.main_pane, bg=self.colors.content)
        self.main_pane.add(self.view_host, minsize=320, stretch="always")

        self.setup_page = tk.Frame(self.view_host, bg=self.colors.content)
        self.setup_page.grid(row=0, column=0, sticky="nsew")
        self.view_host.grid_rowconfigure(0, weight=1)
        self.view_host.grid_columnconfigure(0, weight=1)
        self._build_setup_page()

        self.log_panel = LogPanel(self.main_pane, self)
        self.log_text = self.log_panel.text

        self.action_bar = BottomActionBar(workspace, self)
        self.action_bar.grid(row=2, column=0, sticky="ew")
        self.review_button = ttk.Button(
            self.action_bar.buttons,
            text="Kiểm tra cấu trúc",
            command=self._review_content,
            style="TButton",
            takefocus=True,
        )
        self.generate_button = ttk.Button(
            self.action_bar.buttons,
            text="Tạo XML",
            command=self._generate,
            style="Primary.TButton",
            takefocus=True,
        )
        self.back_action_button = ttk.Button(
            self.action_bar.buttons,
            text="Quay lại",
            command=self._cancel_review,
            style="TButton",
            takefocus=True,
        )
        self.confirm_button = ttk.Button(
            self.action_bar.buttons,
            text="Xác nhận",
            command=self._confirm_review,
            style="Primary.TButton",
            takefocus=True,
        )
        Tooltip(self.generate_button, self._blocked_reason, self.colors)
        Tooltip(
            self.confirm_button,
            lambda: "Chọn đủ các thành phần hợp lệ để xác nhận.",
            self.colors,
        )
        self.progress = self.action_bar.progress
        self.action_bar.show_setup()

    def _build_setup_page(self) -> None:
        page = self.setup_page
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(2, weight=1)
        header = tk.Frame(page, bg=self.colors.content)
        header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=LayoutTokens.CONTENT_PAD_X,
            pady=(LayoutTokens.CONTENT_PAD_Y, 13),
        )
        tk.Label(
            header,
            text="BƯỚC 2 TRÊN 4",
            bg=self.colors.content,
            fg=self.colors.secondary,
            font=(TypeTokens.UI, 10, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            header,
            text="Chuẩn bị dữ liệu",
            bg=self.colors.content,
            fg=self.colors.text,
            font=TypeTokens.TITLE,
            anchor="w",
        ).pack(fill="x", pady=(4, 2))
        tk.Label(
            header,
            text="Kiểm tra nguồn đầu vào trước khi tạo timeline Premiere.",
            bg=self.colors.content,
            fg=self.colors.secondary,
            font=TypeTokens.BODY,
            anchor="w",
        ).pack(fill="x")

        tk.Frame(page, bg=self.colors.separator, height=1).grid(
            row=1,
            column=0,
            sticky="ew",
            padx=LayoutTokens.CONTENT_PAD_X,
        )
        form = tk.Frame(page, bg=self.colors.content)
        form.grid(
            row=2,
            column=0,
            sticky="nsew",
            padx=LayoutTokens.CONTENT_PAD_X,
            pady=(5, 8),
        )
        form.grid_columnconfigure(1, weight=1)
        fields = (
            ("content", "Content DOCX", self.content_var, "file", self._browse_content, "Chứa cấu trúc và timecode bài."),
            ("video", "Thư mục video", self.video_var, "folder", self._browse_video, "Chứa đúng video 1-6 hoặc TH1-TH6."),
            ("audio", "Thư mục voice-over", self.audio_var, "folder", self._browse_audio, "Chứa đầy đủ file voice-over theo timeline của project."),
            ("output", "File XML đầu ra", self.output_var, "output", self._browse_output, "Tạo file XML mới, không ghi đè XML gốc."),
            ("sequence", "Tên sequence", self.sequence_var, "text", None, "Tên hiển thị trong Premiere Pro."),
        )
        for row_index, (key, label, variable, kind, command, helper) in enumerate(fields):
            self._build_field_row(
                form,
                row_index,
                key,
                label,
                variable,
                kind,
                command,
                helper,
            )

    def _build_field_row(
        self,
        parent: tk.Frame,
        row_index: int,
        key: str,
        label: str,
        variable: tk.StringVar,
        kind: str,
        command: Callable[[], None] | None,
        helper: str,
    ) -> None:
        row = tk.Frame(parent, bg=self.colors.content, height=66)
        row.grid(row=row_index, column=0, columnspan=2, sticky="ew")
        row.grid_propagate(False)
        row.grid_columnconfigure(1, weight=1)
        tk.Label(
            row,
            text=label,
            bg=self.colors.content,
            fg=self.colors.text,
            font=TypeTokens.ROW,
            width=18,
            anchor="w",
        ).grid(row=0, column=0, rowspan=2, sticky="nw", pady=(10, 0))
        entry = ttk.Entry(
            row,
            textvariable=variable,
            style="Field.TEntry",
            takefocus=True,
        )
        entry.grid(row=0, column=1, sticky="ew", pady=(6, 2))
        status = tk.Label(
            row,
            text="",
            bg=self.colors.content,
            fg=self.colors.secondary,
            font=TypeTokens.SECONDARY,
            width=13,
            anchor="w",
        )
        status.grid(row=0, column=2, padx=(10, 4), pady=(7, 0))
        if command is not None:
            button_label = {
                "file": "Chọn file",
                "folder": "Chọn thư mục",
                "output": "Chọn vị trí lưu",
            }[kind]
            ttk.Button(
                row,
                text=button_label,
                command=command,
                style="TButton",
                takefocus=True,
            ).grid(row=0, column=3, padx=(4, 0), pady=(5, 0))
        helper_label = tk.Label(
            row,
            text=helper,
            bg=self.colors.content,
            fg=self.colors.secondary,
            font=TypeTokens.SECONDARY,
            anchor="w",
        )
        helper_label.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(0, 5))
        tk.Frame(row, bg=self.colors.separator, height=1).place(
            relx=0, rely=1, relwidth=1, anchor="sw"
        )
        view = FieldView(key, variable, entry, helper_label, status, helper, kind)
        self.field_views[key] = view
        if kind != "text":
            Tooltip(entry, variable.get, self.colors)
            entry.bind(
                "<Button-2>",
                lambda event, item=variable: self._show_path_menu(event, item),
            )
            entry.bind(
                "<Control-Button-1>",
                lambda event, item=variable: self._show_path_menu(event, item),
            )

    def _bind_shortcuts(self) -> None:
        self.root.bind_all("<Command-o>", self._shortcut_open)
        self.root.bind_all("<Command-O>", self._shortcut_open)
        self.root.bind_all("<Command-l>", self._shortcut_log)
        self.root.bind_all("<Command-L>", self._shortcut_log)
        self.root.bind_all("<Command-Return>", self._shortcut_confirm)
        self.root.bind_all("<Escape>", self._escape)

    def _shortcut_open(self, _event: tk.Event) -> str:
        self._browse_project()
        return "break"

    def _shortcut_log(self, _event: tk.Event) -> str:
        self._toggle_log()
        return "break"

    def _shortcut_confirm(self, _event: tk.Event) -> str:
        if self.current_page == "review":
            if str(self.confirm_button.cget("state")) != "disabled":
                self._confirm_review()
        elif self.verified_content is not None:
            if str(self.generate_button.cget("state")) != "disabled":
                self._generate()
        elif str(self.review_button.cget("state")) != "disabled":
            self._review_content()
        return "break"

    def _escape(self, _event: tk.Event) -> str | None:
        if self._toast is not None:
            self._dismiss_toast()
            return "break"
        if self.structure_list is not None and self.structure_list.collapse_all():
            return "break"
        return None

    def _inputs_changed(self, *_args: object) -> None:
        if self._suspend_trace:
            return
        if self.verified_content is not None:
            try:
                current = Path(self.content_var.get()).expanduser().resolve()
            except OSError:
                current = None
            if normalized_path(current) != normalized_path(self.verified_content):
                self.verified_content = None
                self.status_var.set("Content đã thay đổi, cần kiểm tra lại")
                self.generation_success = False
        self._update_generate_state()

    def _set_paths(self, values: list[tuple[tk.StringVar, Path | str]]) -> None:
        self._suspend_trace = True
        try:
            for variable, value in values:
                variable.set(path_text(value))
        finally:
            self._suspend_trace = False
        self._update_generate_state()

    def _load_initial_project(self) -> None:
        try:
            project_dir = resolve_default_project()
        except GeneratorError:
            self._update_generate_state()
            return
        self._autofill_project(project_dir, review=False)

    def _autofill_project(self, project_dir: Path, *, review: bool) -> None:
        try:
            content = find_named_child(project_dir, DEFAULT_CONTENT_NAME)
            xml = find_named_child(project_dir, DEFAULT_XML_NAME)
            if content is None or xml is None:
                raise GeneratorError("Project phải có content.docx và Bài 0509.xml")
            blueprint = load_blueprint(xml)
            video_dir, _videos = discover_video_media(project_dir, None)
            audio_dir, _audios = discover_audio_media(
                project_dir, None, len(blueprint.narration_clips)
            )
            output = xml.with_name(xml.stem + "_v3.xml")
        except GeneratorError as exc:
            self._show_error(str(exc))
            return
        self.verified_content = None
        self.generation_success = False
        self.generation_error = False
        self._set_paths(
            [
                (self.project_var, project_dir),
                (self.content_var, content),
                (self.xml_var, xml),
                (self.video_var, video_dir),
                (self.audio_var, audio_dir),
                (self.output_var, output),
            ]
        )
        self.status_var.set("Đã nạp project, cần kiểm tra cấu trúc")
        self._log(f"Đã nạp project: {project_dir}", "success")
        if review:
            self.root.after(80, self._review_content)

    def _browse_project(self) -> None:
        selected = filedialog.askdirectory(
            title="Chọn thư mục project",
            initialdir=self.project_var.get() or str(Path.cwd()),
        )
        if selected:
            self._autofill_project(Path(selected).resolve(), review=True)

    def _browse_content(self) -> None:
        selected = filedialog.askopenfilename(
            title="Chọn content.docx",
            initialdir=self.project_var.get() or str(Path.cwd()),
            filetypes=(("Word document", "*.docx"), ("All files", "*.*")),
        )
        if selected:
            self.content_var.set(path_text(selected))
            self.root.after(50, self._review_content)

    def _browse_video(self) -> None:
        selected = filedialog.askdirectory(
            title="Chọn thư mục video",
            initialdir=self.video_var.get() or self.project_var.get() or str(Path.cwd()),
        )
        if selected:
            self.video_var.set(path_text(selected))

    def _browse_audio(self) -> None:
        selected = filedialog.askdirectory(
            title="Chọn thư mục voice-over",
            initialdir=self.audio_var.get() or self.project_var.get() or str(Path.cwd()),
        )
        if selected:
            self.audio_var.set(path_text(selected))

    def _browse_output(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Chọn vị trí lưu XML",
            initialdir=self.project_var.get() or str(Path.cwd()),
            initialfile=(
                Path(self.output_var.get()).name
                if self.output_var.get()
                else "output_v3.xml"
            ),
            defaultextension=".xml",
            filetypes=(("XML", "*.xml"),),
        )
        if selected:
            self.output_var.set(path_text(selected))

    def _validate_fields(self) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        specs = {
            "content": (self.content_var, "file", ".docx", "Content DOCX"),
            "xml": (self.xml_var, "file", ".xml", "XML timeline tự động"),
            "video": (self.video_var, "directory", "", "Thư mục video"),
            "audio": (self.audio_var, "directory", "", "Thư mục voice-over"),
            "output": (self.output_var, "output", ".xml", "File XML đầu ra"),
        }
        for key, (variable, kind, suffix, label) in specs.items():
            raw = variable.get().strip()
            if not raw:
                if key == "xml":
                    result[key] = (
                        "error",
                        f"Project phải có {DEFAULT_XML_NAME} để đọc timeline.",
                    )
                else:
                    result[key] = ("empty", f"Chưa chọn {label.lower()}.")
                continue
            path = Path(raw).expanduser()
            if suffix and path.suffix.casefold() != suffix:
                result[key] = ("error", f"{label} phải có đuôi {suffix}.")
            elif kind == "file" and not path.is_file():
                result[key] = (
                    "error",
                    f"Project không có {DEFAULT_XML_NAME}: {path}"
                    if key == "xml"
                    else f"Không tìm thấy file: {path}",
                )
            elif kind == "directory" and not path.is_dir():
                result[key] = ("error", f"Không tìm thấy thư mục: {path}")
            elif (
                kind == "output"
                and self.xml_var.get().strip()
                and path.resolve() == Path(self.xml_var.get()).expanduser().resolve()
            ):
                result[key] = (
                    "error",
                    "File đầu ra không được ghi đè XML gốc.",
                )
            else:
                result[key] = (
                    "valid",
                    (
                        "Vị trí lưu hợp lệ."
                        if kind != "output" or path.parent.is_dir()
                        else "Vị trí hợp lệ, thư mục sẽ được tạo khi xuất."
                    )
                    if kind == "output"
                    else "Đã tìm thấy.",
                )
        return result

    def _update_field_states(self) -> None:
        for key, (state, message) in self._validate_fields().items():
            view = self.field_views.get(key)
            if view is None:
                continue
            if state == "valid":
                color, status, style = self.colors.success, "✓ Đã tìm thấy", "Valid.Field.TEntry"
            elif state == "error":
                color, status, style = self.colors.error, "! Có lỗi", "Error.Field.TEntry"
            else:
                color, status, style = self.colors.secondary, "Chưa chọn", "Field.TEntry"
            if key == "output" and state == "valid":
                status = "✓ Hợp lệ"
            view.entry.configure(style=style)
            view.status_label.configure(text=status, fg=color)
            view.helper_label.configure(
                text=(view.default_helper + " " + message) if state == "valid" else message,
                fg=self.colors.error if state == "error" else self.colors.secondary,
            )

    def _all_fields_valid(self) -> bool:
        return all(state == "valid" for state, _ in self._validate_fields().values())

    def _project_valid(self) -> bool:
        raw = self.project_var.get().strip()
        return bool(raw and Path(raw).expanduser().is_dir())

    def _required_paths_ready(self) -> bool:
        return all(
            variable.get().strip()
            for variable in (
                self.project_var,
                self.content_var,
                self.xml_var,
                self.video_var,
                self.audio_var,
                self.output_var,
            )
        )

    def _blocked_reason(self) -> str:
        if self.is_running:
            return "XML đang được tạo. Vui lòng chờ hoàn tất."
        if self.is_review_loading:
            return "Đang kiểm tra cấu trúc content.docx."
        if not self._project_valid():
            return (
                "Không tìm thấy thư mục project đã chọn."
                if self.project_var.get().strip()
                else "Chọn project để bắt đầu."
            )
        invalid = [
            message
            for state, message in self._validate_fields().values()
            if state != "valid"
        ]
        if invalid:
            return invalid[0]
        if self.verified_content is None:
            return "Cần xác nhận đủ 8 thành phần trước khi tạo XML."
        return "Sẵn sàng tạo XML."

    def _workflow_states(self) -> list[str]:
        project_raw = self.project_var.get().strip()
        project_valid = bool(project_raw and Path(project_raw).expanduser().is_dir())
        fields = self._validate_fields()
        data_valid = project_valid and all(
            state == "valid" for state, _ in fields.values()
        )
        project_state = "complete" if project_valid else "error" if project_raw else "current"
        if not project_valid:
            return [project_state, "pending", "pending", "pending"]
        data_state = (
            "complete"
            if data_valid
            else "error"
            if any(state == "error" for state, _ in fields.values())
            else "current"
        )
        if not data_valid:
            return ["complete", data_state, "pending", "pending"]
        if self.current_page != "review" and self.verified_content is None:
            return ["complete", "current", "pending", "pending"]
        review_state = "current" if self.current_page == "review" else "complete"
        if self.generation_success:
            export_state = "complete"
        elif self.generation_error:
            export_state = "error"
        elif self.is_running or self.verified_content is not None:
            export_state = "current"
        else:
            export_state = "pending"
        return ["complete", "complete", review_state, export_state]

    def _update_workflow_ui(self) -> None:
        states = self._workflow_states()
        self.sidebar.update_states(states)
        if self.generation_success:
            color = self.colors.success
        elif self.generation_error:
            color = self.colors.error
        elif self.verified_content is not None:
            color = self.colors.success
        elif self.project_var.get().strip():
            color = self.colors.secondary
        else:
            color = self.colors.tertiary
        self.sidebar.project_status.configure(fg=color)

    def _update_generate_state(self) -> None:
        self._update_field_states()
        enabled = (
            not self.is_running
            and not self.is_review_loading
            and self.verified_content is not None
            and self._required_paths_ready()
            and self._all_fields_valid()
            and self._project_valid()
        )
        self.generate_button.configure(state="normal" if enabled else "disabled")
        content_valid = self._validate_fields().get("content", ("empty", ""))[0] == "valid"
        self.review_button.configure(
            state=(
                "normal"
                if content_valid and not self.is_running and not self.is_review_loading
                else "disabled"
            )
        )
        self.action_status_var.set(self._blocked_reason())
        project_raw = self.project_var.get().strip()
        self.project_name_var.set(Path(project_raw).name if project_raw else "Chưa có project")
        if self.generation_success:
            badge = "Đã xuất XML"
        elif self.verified_content is not None:
            badge = "Sẵn sàng"
        elif self._all_fields_valid():
            badge = "Chờ xác nhận"
        elif project_raw:
            badge = "Thiếu dữ liệu"
        else:
            badge = "Chưa sẵn sàng"
        self.project_badge_var.set(badge)
        self._update_workflow_ui()
        self.toolbar.set_can_go_back(self.current_page == "review")

    def _review_content(self) -> None:
        if self.is_review_loading or self.is_running:
            return
        raw_path = self.content_var.get().strip()
        if not raw_path:
            self._show_error("Hãy chọn content.docx trước")
            return
        content_path = Path(raw_path).expanduser().resolve()
        if not content_path.is_file():
            self._show_error(f"Không tìm thấy content.docx: {content_path}")
            return
        self.is_review_loading = True
        self.verified_content = None
        self.status_var.set("Đang đọc cấu trúc content.docx")
        self.review_button.configure(text="Đang kiểm tra...", state="disabled")
        self._set_processing(True)
        self._log(f"Đang kiểm tra cấu trúc: {content_path}")
        xml_path = Path(self.xml_var.get()).expanduser()
        self.root.after(40, self._poll_worker_results)

        def worker() -> None:
            try:
                components = inspect_docx_components(content_path)
                voiceovers = 0
                if xml_path.is_file():
                    voiceovers = len(load_blueprint(xml_path).narration_clips)
            except Exception as exc:
                self._worker_results.put(("review_error", exc))
                return
            self._worker_results.put(("review_ok", (content_path, components, voiceovers)))

        threading.Thread(target=worker, daemon=True).start()

    def _review_loaded(
        self,
        content_path: Path,
        components: list[ArticleComponent],
        voiceovers: int,
    ) -> None:
        self.is_review_loading = False
        self.review_button.configure(text="Kiểm tra cấu trúc")
        self._set_processing(False)
        self.review_path = content_path
        self.review_components = components
        self.current_page = "review"
        self.status_var.set("Đang kiểm tra 8 thành phần")
        self._show_review_page(content_path, components, voiceovers)
        self._update_generate_state()

    def _review_failed(self, error: Exception) -> None:
        self.is_review_loading = False
        self.review_button.configure(text="Kiểm tra cấu trúc")
        self._set_processing(False)
        self._show_error(str(error))

    def _show_review_page(
        self,
        content_path: Path,
        components: list[ArticleComponent],
        voiceovers: int,
    ) -> None:
        self.setup_page.grid_remove()
        if self.review_page is not None:
            self.review_page.destroy()
        page = tk.Frame(self.view_host, bg=self.colors.content)
        self.review_page = page
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(3, weight=1)

        header = tk.Frame(page, bg=self.colors.content)
        header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=LayoutTokens.CONTENT_PAD_X,
            pady=(LayoutTokens.CONTENT_PAD_Y, 13),
        )
        header.grid_columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="BƯỚC 3 TRÊN 4",
            bg=self.colors.content,
            fg=self.colors.secondary,
            font=(TypeTokens.UI, 10, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            header,
            text="Kiểm tra cấu trúc",
            bg=self.colors.content,
            fg=self.colors.text,
            font=TypeTokens.TITLE,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(4, 2))
        tk.Label(
            header,
            text="Xác nhận các phần trước khi tạo timeline.",
            bg=self.colors.content,
            fg=self.colors.secondary,
            font=TypeTokens.BODY,
            anchor="w",
        ).grid(row=2, column=0, sticky="w")
        document_button = ttk.Button(
            header,
            text=f"▤  {content_path.name}",
            command=lambda: self._show_document_menu(document_button, content_path),
            style="TButton",
            takefocus=True,
        )
        document_button.grid(row=1, column=1, rowspan=2, sticky="e", padx=(14, 0))
        Tooltip(document_button, path_text(content_path), self.colors)
        document_button.bind(
            "<Button-2>",
            lambda event: self._show_path_menu_value(event, content_path),
        )
        document_button.bind(
            "<Control-Button-1>",
            lambda event: self._show_path_menu_value(event, content_path),
        )

        self.summary_bar = SummaryBar(page, self, components, voiceovers)
        self.summary_bar.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=LayoutTokens.CONTENT_PAD_X,
        )
        tk.Label(
            page,
            text="Thành phần bài",
            bg=self.colors.content,
            fg=self.colors.text,
            font=TypeTokens.SECTION,
            anchor="w",
        ).grid(
            row=2,
            column=0,
            sticky="ew",
            padx=LayoutTokens.CONTENT_PAD_X,
            pady=(14, 7),
        )
        self.review_variables = [tk.BooleanVar(value=False) for _ in components]
        self.structure_list = StructureList(page, self, components)
        self.structure_list.grid(
            row=3,
            column=0,
            sticky="nsew",
            padx=LayoutTokens.CONTENT_PAD_X,
            pady=(0, 12),
        )
        self.action_bar.show_review()
        self._update_confirm_state()

    def _show_document_menu(self, widget: tk.Widget, path: Path) -> None:
        menu = self._path_menu(path_text(path))
        menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    def _select_all(self) -> None:
        desired = True
        if self.summary_bar is not None:
            desired = self.summary_bar.select_all_var.get()
        if not desired and not any(variable.get() for variable in self.review_variables):
            desired = True
        for component, variable in zip(
            self.review_components, self.review_variables, strict=True
        ):
            variable.set(desired and component_is_ready(component))
        if self.structure_list is not None:
            self.structure_list.refresh_all()
        self._update_confirm_state()

    def _update_confirm_state(self) -> None:
        total = len(self.review_variables)
        selected = sum(variable.get() for variable in self.review_variables)
        self.review_footer_var.set(f"Đã chọn {selected} trong {total} thành phần")
        if self.summary_bar is not None:
            self.summary_bar.set_selected(selected, total)
        ready = bool(self.review_components) and all(
            component_is_ready(item) for item in self.review_components
        )
        checked = bool(self.review_variables) and all(
            variable.get() for variable in self.review_variables
        )
        self.confirm_button.configure(state="normal" if ready and checked else "disabled")
        if self.current_page == "review":
            self.action_status_var.set(self.review_footer_var.get())

    def _confirm_review(self) -> None:
        if self.review_path is None:
            return
        if not self.review_variables or not all(
            variable.get() for variable in self.review_variables
        ):
            self._show_error("Cần xác nhận đủ 8 thành phần trước khi tiếp tục")
            return
        self.verified_content = self.review_path
        cut_count = sum(
            item.video_cut_count
            for item in self.review_components
            if item.key != "outro"
        )
        self.status_var.set("Cấu trúc đã xác nhận, sẵn sàng tạo XML")
        self._log(
            f"Đã xác nhận {len(self.review_components)} thành phần, {cut_count} video cut",
            "success",
        )
        self._return_to_setup()

    def _cancel_review(self) -> None:
        if self.current_page != "review":
            return
        self.verified_content = None
        self.status_var.set("Cấu trúc chưa được xác nhận")
        self._log("Đã quay lại mà chưa xác nhận cấu trúc.", "warning")
        self._return_to_setup()

    def _return_to_setup(self) -> None:
        self.current_page = "setup"
        if self.review_page is not None:
            self.review_page.grid_remove()
        self.setup_page.grid()
        self.action_bar.show_setup()
        self.structure_list = None
        self.summary_bar = None
        self._update_generate_state()

    def _generate(self) -> None:
        if self.is_running:
            return
        if self.verified_content is None:
            self._show_error("Hãy kiểm tra và xác nhận content.docx trước")
            return
        if not self._all_fields_valid():
            self._show_error(self._blocked_reason())
            return
        snapshot = {
            "project_dir": Path(self.project_var.get()).expanduser().resolve(),
            "content": Path(self.content_var.get()).expanduser().resolve(),
            "reference_xml": Path(self.xml_var.get()).expanduser().resolve(),
            "video_dir": Path(self.video_var.get()).expanduser().resolve(),
            "audio_dir": Path(self.audio_var.get()).expanduser().resolve(),
            "output": Path(self.output_var.get()).expanduser().resolve(),
            "sequence_name": self.sequence_var.get().strip() or None,
        }
        self.is_running = True
        self.generation_error = False
        self.generation_success = False
        self.status_var.set("Đang kiểm tra media và tạo XML")
        self.generate_button.configure(text="Đang tạo...", state="disabled")
        self._set_processing(True)
        self._log("Bắt đầu kiểm tra 6 video và 103 voice-over...")
        self._update_generate_state()
        self.root.after(40, self._poll_worker_results)

        def worker() -> None:
            try:
                summary = generate_project_xml(**snapshot)
            except Exception as exc:
                self._worker_results.put(("generation_error", exc))
                return
            self._worker_results.put(("generation_ok", summary))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_worker_results(self) -> None:
        handled = False
        while True:
            try:
                kind, payload = self._worker_results.get_nowait()
            except queue.Empty:
                break
            handled = True
            if kind == "review_ok":
                content_path, components, voiceovers = payload  # type: ignore[misc]
                self._review_loaded(content_path, components, voiceovers)
            elif kind == "review_error":
                self._review_failed(payload)  # type: ignore[arg-type]
            elif kind == "generation_ok":
                self._generation_succeeded(payload)  # type: ignore[arg-type]
            elif kind == "generation_error":
                self._generation_failed(payload)  # type: ignore[arg-type]
        if not handled and (self.is_running or self.is_review_loading):
            self.root.after(40, self._poll_worker_results)

    def _generation_succeeded(self, summary: GenerationSummary) -> None:
        self.is_running = False
        self.generation_success = True
        self.generation_error = False
        self.generate_button.configure(text="Tạo XML")
        self._set_processing(False)
        self.status_var.set("XML đã tạo và kiểm tra thành công")
        self._log(
            f"Hoàn tất: {summary.video_cut_count} video cut, "
            f"{summary.narration_count} voice-over, {summary.duration_seconds:.2f}s",
            "success",
        )
        self._log(
            f"Đã tự xóa {summary.removed_blur_effect_count} hiệu ứng làm mờ khỏi video.",
            "success",
        )
        self._log(f"Output: {summary.output}", "success")
        self._show_success_toast(summary.output)
        self._update_generate_state()

    def _generation_failed(self, error: Exception) -> None:
        self.is_running = False
        self.generation_success = False
        self.generation_error = True
        self.generate_button.configure(text="Tạo XML")
        self._set_processing(False)
        self.status_var.set("Tạo XML thất bại")
        self._update_generate_state()
        self._show_error(str(error))

    def _set_processing(self, active: bool) -> None:
        if active:
            self.progress.pack(fill="x", pady=(5, 0))
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()

    def _show_success_toast(self, output: Path) -> None:
        self._dismiss_toast()
        toast = tk.Toplevel(self.root)
        self._toast = toast
        toast.title("Tạo XML thành công")
        toast.resizable(False, False)
        toast.transient(self.root)
        toast.configure(bg=self.colors.elevated)
        try:
            if sys.platform == "darwin":
                toast.tk.call(
                    "tk::unsupported::MacWindowStyle",
                    "style",
                    toast._w,
                    "utility",
                    "closeBox",
                )
        except tk.TclError:
            pass
        body = tk.Frame(toast, bg=self.colors.elevated, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="✓  Tạo XML thành công",
            bg=self.colors.elevated,
            fg=self.colors.success,
            font=TypeTokens.SECTION,
            anchor="w",
        ).pack(fill="x")
        path_label = tk.Label(
            body,
            text=shorten_middle(path_text(output), 62),
            bg=self.colors.elevated,
            fg=self.colors.secondary,
            font=TypeTokens.MONO_SMALL,
            anchor="w",
        )
        path_label.pack(fill="x", pady=(8, 13))
        Tooltip(path_label, path_text(output), self.colors)
        actions = tk.Frame(body, bg=self.colors.elevated)
        actions.pack(fill="x")
        ttk.Button(
            actions,
            text="Mở trong Finder",
            command=lambda: self._open_path_value(output),
        ).pack(side="left")
        ttk.Button(
            actions,
            text="Copy đường dẫn",
            command=lambda: self._copy_value(path_text(output)),
        ).pack(side="left", padx=(7, 0))
        toast.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() - toast.winfo_reqwidth() - 24
        y = self.root.winfo_rooty() + 54
        toast.geometry(f"+{max(0, x)}+{max(0, y)}")
        toast.protocol("WM_DELETE_WINDOW", self._dismiss_toast)

    def _dismiss_toast(self) -> None:
        if self._toast is not None:
            try:
                self._toast.destroy()
            except tk.TclError:
                pass
            self._toast = None

    def _show_error(self, message: str) -> None:
        self.status_var.set("Cần xử lý lỗi trước khi tiếp tục")
        self._log(f"LỖI: {message}", "error")
        self._update_generate_state()
        messagebox.showerror(
            "Không thể tiếp tục",
            message + "\n\nHãy kiểm tra trường được đánh dấu hoặc chọn lại nguồn dữ liệu.",
            parent=self.root,
        )

    def _log(self, message: str, level: str = "info") -> None:
        self.log_panel.append(message, level)

    def _toggle_log(self) -> None:
        self.log_expanded = not self.log_expanded
        if self.log_expanded:
            self.main_pane.add(
                self.log_panel,
                minsize=90,
                height=LayoutTokens.LOG_HEIGHT,
                stretch="never",
            )
            self.root.after_idle(self._place_log_sash)
        else:
            self.main_pane.forget(self.log_panel)

    def _place_log_sash(self) -> None:
        if not self.log_expanded:
            return
        try:
            y = max(320, self.main_pane.winfo_height() - LayoutTokens.LOG_HEIGHT)
            self.main_pane.sash_place(0, 0, y)
        except tk.TclError:
            pass

    def _copy_value(self, value: str, *, log_event: bool = True) -> None:
        if not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.root.update_idletasks()
        if log_event:
            self._log("Đã copy đường dẫn vào clipboard.", "success")

    def _copy_path(self, variable: tk.StringVar) -> None:
        self._copy_value(variable.get().strip())

    def _clear_path(self, variable: tk.StringVar) -> None:
        variable.set("")

    def _open_path(self, variable: tk.StringVar) -> None:
        raw = variable.get().strip()
        if raw:
            self._open_path_value(Path(raw).expanduser())

    def _open_path_value(self, path: Path) -> None:
        target = path if path.exists() else path.parent
        if not target.exists():
            self._show_error(f"Không tìm thấy đường dẫn: {path}")
            return
        try:
            if sys.platform == "darwin":
                command = ["open", "-R", str(path)] if path.is_file() else ["open", str(target)]
            elif os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
                return
            else:
                command = ["xdg-open", str(target)]
            subprocess.Popen(command)
        except OSError as exc:
            self._show_error(f"Không thể mở Finder: {exc}")

    def _path_menu(self, value: str) -> tk.Menu:
        menu = tk.Menu(
            self.root,
            tearoff=False,
            bg=self.colors.elevated,
            fg=self.colors.text,
            activebackground=self.colors.selection,
            activeforeground="#FFFFFF",
            font=TypeTokens.BODY,
        )
        menu.add_command(label="Copy Path", command=lambda: self._copy_value(value))
        menu.add_command(
            label="Reveal in Finder",
            command=lambda: self._open_path_value(Path(value).expanduser()),
        )
        return menu

    def _show_path_menu(self, event: tk.Event, variable: tk.StringVar) -> str:
        return self._show_path_menu_value(event, Path(variable.get()).expanduser())

    def _show_path_menu_value(self, event: tk.Event, path: Path) -> str:
        value = path_text(path)
        menu = self._path_menu(value)
        menu.tk_popup(event.x_root, event.y_root)
        return "break"


def main() -> None:
    root = tk.Tk()
    XmlGeneratorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
