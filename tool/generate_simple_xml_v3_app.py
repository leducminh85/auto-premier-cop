#!/usr/bin/env python3
"""Desktop interface for the Karen 0509 Premiere XML generator."""

from __future__ import annotations

import threading
import tkinter as tk
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


class Palette:
    BG = "#141412"
    RAIL = "#1A1A17"
    SURFACE = "#20201C"
    FIELD = "#272721"
    BORDER = "#414139"
    TEXT = "#F2EFE7"
    MUTED = "#AAA79E"
    DIM = "#77746D"
    ACCENT = "#E0644B"
    ACCENT_ACTIVE = "#C9533D"
    SUCCESS = "#93B58B"
    ERROR = "#E58A78"


FONT_UI = "Avenir Next"
FONT_MONO = "SF Mono"


def path_text(path: Path | str) -> str:
    return str(Path(path).expanduser())


def component_is_ready(component: ArticleComponent) -> bool:
    if not component.present or component.text_block_count == 0:
        return False
    if component.key == "outro":
        return True
    return component.video_cut_count > 0


class ComponentReviewDialog:
    def __init__(
        self,
        parent: tk.Tk,
        content_path: Path,
        components: list[ArticleComponent],
    ) -> None:
        self.result = False
        self.window = tk.Toplevel(parent)
        self.window.title("Kiểm tra cấu trúc bài")
        self.window.geometry("780x690")
        self.window.minsize(680, 560)
        self.window.configure(bg=Palette.BG)
        self.window.transient(parent)
        self.window.grab_set()

        self.components = components
        self.variables: list[tk.BooleanVar] = []
        self.confirm_button: tk.Button | None = None

        self._build(content_path)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        self.window.bind("<Escape>", lambda _event: self._cancel())
        self.window.after(80, self.window.focus_force)

    def _build(self, content_path: Path) -> None:
        header = tk.Frame(self.window, bg=Palette.BG, padx=32, pady=26)
        header.pack(fill="x")
        tk.Label(
            header,
            text="KIỂM TRA CẤU TRÚC BÀI",
            bg=Palette.BG,
            fg=Palette.ACCENT,
            font=(FONT_MONO, 10, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            header,
            text="Xác nhận trước khi dựng timeline",
            bg=Palette.BG,
            fg=Palette.TEXT,
            font=(FONT_UI, 24, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(7, 5))
        tk.Label(
            header,
            text=path_text(content_path),
            bg=Palette.BG,
            fg=Palette.MUTED,
            font=(FONT_MONO, 10),
            anchor="w",
            wraplength=700,
            justify="left",
        ).pack(fill="x")

        divider = tk.Frame(self.window, bg=Palette.BORDER, height=1)
        divider.pack(fill="x", padx=32)

        body = tk.Frame(self.window, bg=Palette.BG)
        body.pack(fill="both", expand=True, padx=24, pady=14)
        canvas = tk.Canvas(body, bg=Palette.BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        content = tk.Frame(canvas, bg=Palette.BG)
        content_window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(content_window, width=event.width),
        )

        for component in self.components:
            self._add_component(content, component)

        footer = tk.Frame(self.window, bg=Palette.RAIL, padx=32, pady=18)
        footer.pack(fill="x")
        footer.columnconfigure(0, weight=1)
        footer.columnconfigure(1, weight=0)
        footer.columnconfigure(2, weight=0)

        missing = [item.title for item in self.components if not component_is_ready(item)]
        status_text = (
            "Thiếu: " + ", ".join(missing)
            if missing
            else "Đã tìm thấy đủ 8 thành phần bắt buộc."
        )
        tk.Label(
            footer,
            text=status_text,
            bg=Palette.RAIL,
            fg=Palette.ERROR if missing else Palette.SUCCESS,
            font=(FONT_UI, 11, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        select_all = self._button(
            footer,
            "CHỌN TẤT CẢ",
            self._select_all,
            bg=Palette.FIELD,
            active=Palette.BORDER,
        )
        select_all.grid(row=0, column=1, padx=(16, 10))
        self.confirm_button = self._button(
            footer,
            "XÁC NHẬN",
            self._confirm,
            bg=Palette.ACCENT,
            active=Palette.ACCENT_ACTIVE,
        )
        self.confirm_button.grid(row=0, column=2)
        self._update_confirm_state()

    def _add_component(self, parent: tk.Frame, component: ArticleComponent) -> None:
        ready = component_is_ready(component)
        variable = tk.BooleanVar(value=False)
        self.variables.append(variable)

        row = tk.Frame(
            parent,
            bg=Palette.SURFACE,
            highlightthickness=1,
            highlightbackground=Palette.BORDER,
            padx=18,
            pady=14,
        )
        row.pack(fill="x", padx=8, pady=6)
        row.columnconfigure(1, weight=1)

        checkbox = tk.Checkbutton(
            row,
            variable=variable,
            command=self._update_confirm_state,
            bg=Palette.SURFACE,
            activebackground=Palette.SURFACE,
            selectcolor=Palette.FIELD,
            fg=Palette.TEXT,
            activeforeground=Palette.TEXT,
            highlightthickness=0,
            bd=0,
            state="normal" if ready else "disabled",
        )
        checkbox.grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, 12))

        tk.Label(
            row,
            text=component.title,
            bg=Palette.SURFACE,
            fg=Palette.TEXT if ready else Palette.ERROR,
            font=(FONT_UI, 14, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="ew")

        if component.key == "outro":
            metrics = f"{component.text_block_count} khối lời đọc"
        else:
            metrics = (
                f"{component.video_cut_count} video cut  /  "
                f"{component.text_block_count} khối lời đọc"
            )
        tk.Label(
            row,
            text=metrics if ready else "Không đủ dữ liệu để xác nhận",
            bg=Palette.SURFACE,
            fg=Palette.MUTED if ready else Palette.ERROR,
            font=(FONT_MONO, 10),
            anchor="w",
        ).grid(row=1, column=1, sticky="ew", pady=(4, 0))

        if component.timecodes:
            detail = "  ".join(component.timecodes)
            tk.Label(
                row,
                text=detail,
                bg=Palette.SURFACE,
                fg=Palette.DIM,
                font=(FONT_MONO, 9),
                anchor="w",
                justify="left",
                wraplength=620,
            ).grid(row=2, column=1, sticky="ew", pady=(7, 0))

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        bg: str,
        active: str,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=Palette.TEXT,
            activebackground=active,
            activeforeground=Palette.TEXT,
            disabledforeground=Palette.DIM,
            font=(FONT_UI, 10, "bold"),
            relief="flat",
            bd=0,
            padx=18,
            pady=10,
            cursor="hand2",
        )

    def _select_all(self) -> None:
        for component, variable in zip(self.components, self.variables, strict=True):
            variable.set(component_is_ready(component))
        self._update_confirm_state()

    def _update_confirm_state(self) -> None:
        if self.confirm_button is None:
            return
        ready = all(component_is_ready(item) for item in self.components)
        checked = all(variable.get() for variable in self.variables)
        self.confirm_button.configure(state="normal" if ready and checked else "disabled")

    def _confirm(self) -> None:
        self.result = True
        self.window.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.window.destroy()

    def show(self) -> bool:
        self.window.wait_window()
        return self.result


class XmlGeneratorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("XML Cut Room")
        self.root.geometry("1160x790")
        self.root.minsize(980, 680)
        self.root.configure(bg=Palette.BG)

        self.project_var = tk.StringVar()
        self.content_var = tk.StringVar()
        self.xml_var = tk.StringVar()
        self.video_var = tk.StringVar()
        self.audio_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.sequence_var = tk.StringVar(value="Bài 0509")
        self.status_var = tk.StringVar(value="Chưa kiểm tra content.docx")
        self.verified_content: Path | None = None
        self.is_running = False
        self.generate_button: tk.Button | None = None
        self.log_text: tk.Text | None = None
        self._suspend_trace = False

        self._configure_ttk()
        self._build()
        for variable in (
            self.project_var,
            self.content_var,
            self.xml_var,
            self.video_var,
            self.audio_var,
            self.output_var,
        ):
            variable.trace_add("write", self._inputs_changed)
        self._load_initial_project()

    def _configure_ttk(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Vertical.TScrollbar",
            background=Palette.FIELD,
            troughcolor=Palette.BG,
            bordercolor=Palette.BG,
            arrowcolor=Palette.MUTED,
        )

    def _build(self) -> None:
        shell = tk.Frame(self.root, bg=Palette.BG)
        shell.pack(fill="both", expand=True)
        shell.grid_rowconfigure(0, weight=1)
        shell.grid_columnconfigure(1, weight=1)

        rail = tk.Frame(shell, bg=Palette.RAIL, width=292, padx=28, pady=30)
        rail.grid(row=0, column=0, sticky="nsew")
        rail.grid_propagate(False)
        rail.grid_rowconfigure(6, weight=1)

        tk.Label(
            rail,
            text="XML CUT ROOM",
            bg=Palette.RAIL,
            fg=Palette.ACCENT,
            font=(FONT_MONO, 11, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            rail,
            text="Dựng timeline\nđúng cấu trúc.",
            bg=Palette.RAIL,
            fg=Palette.TEXT,
            font=(FONT_UI, 27, "bold"),
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(15, 12))
        tk.Label(
            rail,
            text="Chọn project, xác nhận từng phần của bài, sau đó xuất XML để import vào Premiere.",
            bg=Palette.RAIL,
            fg=Palette.MUTED,
            font=(FONT_UI, 12),
            wraplength=225,
            justify="left",
            anchor="w",
        ).grid(row=2, column=0, sticky="ew")

        self._section_label(rail, "PROJECT").grid(row=3, column=0, sticky="ew", pady=(35, 9))
        project_entry = self._entry(rail, self.project_var)
        project_entry.grid(row=4, column=0, sticky="ew")
        self._button(
            rail,
            "CHỌN PROJECT",
            self._browse_project,
            primary=False,
        ).grid(row=5, column=0, sticky="ew", pady=(10, 0))

        rail_status = tk.Frame(
            rail,
            bg=Palette.SURFACE,
            highlightthickness=1,
            highlightbackground=Palette.BORDER,
            padx=15,
            pady=14,
        )
        rail_status.grid(row=7, column=0, sticky="sew")
        tk.Label(
            rail_status,
            text="TRẠNG THÁI",
            bg=Palette.SURFACE,
            fg=Palette.DIM,
            font=(FONT_MONO, 9, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            rail_status,
            textvariable=self.status_var,
            bg=Palette.SURFACE,
            fg=Palette.TEXT,
            font=(FONT_UI, 11, "bold"),
            anchor="w",
            justify="left",
            wraplength=210,
        ).pack(fill="x", pady=(7, 0))

        main = tk.Frame(shell, bg=Palette.BG, padx=42, pady=30)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(11, weight=1)

        tk.Label(
            main,
            text="Chuẩn bị dữ liệu",
            bg=Palette.BG,
            fg=Palette.TEXT,
            font=(FONT_UI, 25, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        tk.Label(
            main,
            text="Các đường dẫn được tự điền khi chọn project và vẫn có thể thay đổi riêng.",
            bg=Palette.BG,
            fg=Palette.MUTED,
            font=(FONT_UI, 12),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(4, 22))

        fields = [
            (
                "CONTENT DOCX",
                self.content_var,
                "Kịch bản chứa Intro, TH1-TH6 và Kết bài",
                self._browse_content,
            ),
            (
                "XML BLUEPRINT",
                self.xml_var,
                "Timeline tham chiếu có 103 vị trí voice-over",
                self._browse_xml,
            ),
            (
                "VIDEO FOLDER",
                self.video_var,
                "Thư mục chứa 1.mp4 đến 6.mp4",
                self._browse_video,
            ),
            (
                "AUDIO FOLDER",
                self.audio_var,
                "Thư mục chứa voice-over theo natural sort",
                self._browse_audio,
            ),
            (
                "OUTPUT XML",
                self.output_var,
                "File mới, không ghi đè XML blueprint",
                self._browse_output,
            ),
        ]
        for index, (label, variable, helper, command) in enumerate(fields, start=2):
            self._path_field(main, index, label, variable, helper, command)

        options = tk.Frame(main, bg=Palette.BG)
        options.grid(row=7, column=0, sticky="ew", pady=(18, 0))
        options.grid_columnconfigure(0, weight=1)
        options.grid_columnconfigure(1, weight=0)
        sequence = tk.Frame(options, bg=Palette.BG)
        sequence.grid(row=0, column=0, sticky="ew", padx=(0, 18))
        self._section_label(sequence, "TÊN SEQUENCE").pack(fill="x", pady=(0, 7))
        self._entry(sequence, self.sequence_var).pack(fill="x")

        review_button = self._button(
            options,
            "KIỂM TRA BÀI",
            self._review_content,
            primary=False,
        )
        review_button.grid(row=0, column=1, sticky="se", pady=(24, 0))

        actions = tk.Frame(main, bg=Palette.BG)
        actions.grid(row=8, column=0, sticky="ew", pady=(20, 16))
        actions.grid_columnconfigure(0, weight=1)
        tk.Label(
            actions,
            text="Chỉ có thể tạo XML sau khi checklist đã được xác nhận.",
            bg=Palette.BG,
            fg=Palette.DIM,
            font=(FONT_UI, 10),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.generate_button = self._button(
            actions,
            "TẠO XML",
            self._generate,
            primary=True,
        )
        self.generate_button.grid(row=0, column=1, sticky="e")

        tk.Frame(main, bg=Palette.BORDER, height=1).grid(row=9, column=0, sticky="ew")
        self._section_label(main, "NHẬT KÝ").grid(
            row=10, column=0, sticky="ew", pady=(17, 8)
        )
        self.log_text = tk.Text(
            main,
            bg=Palette.SURFACE,
            fg=Palette.MUTED,
            insertbackground=Palette.TEXT,
            selectbackground=Palette.ACCENT_ACTIVE,
            font=(FONT_MONO, 10),
            relief="flat",
            bd=0,
            padx=16,
            pady=14,
            height=8,
            wrap="word",
            state="disabled",
        )
        self.log_text.grid(row=11, column=0, sticky="nsew")
        self._log("Chưa có tác vụ. Chọn hoặc kiểm tra content.docx để bắt đầu.")

    def _section_label(self, parent: tk.Misc, text: str) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=parent.cget("bg"),
            fg=Palette.DIM,
            font=(FONT_MONO, 9, "bold"),
            anchor="w",
        )

    def _entry(self, parent: tk.Misc, variable: tk.StringVar) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            bg=Palette.FIELD,
            fg=Palette.TEXT,
            insertbackground=Palette.TEXT,
            disabledbackground=Palette.FIELD,
            disabledforeground=Palette.MUTED,
            selectbackground=Palette.ACCENT_ACTIVE,
            font=(FONT_MONO, 10),
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=Palette.BORDER,
            highlightcolor=Palette.ACCENT,
        )

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        primary: bool,
    ) -> tk.Button:
        background = Palette.ACCENT if primary else Palette.FIELD
        active = Palette.ACCENT_ACTIVE if primary else Palette.BORDER
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=background,
            fg=Palette.TEXT,
            activebackground=active,
            activeforeground=Palette.TEXT,
            disabledforeground=Palette.DIM,
            font=(FONT_UI, 10, "bold"),
            relief="flat",
            bd=0,
            padx=20,
            pady=11,
            cursor="hand2",
        )

    def _path_field(
        self,
        parent: tk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        helper: str,
        command: Callable[[], None],
    ) -> None:
        block = tk.Frame(parent, bg=Palette.BG)
        block.grid(row=row, column=0, sticky="ew", pady=7)
        block.grid_columnconfigure(0, weight=1)
        self._section_label(block, label).grid(row=0, column=0, sticky="w")
        tk.Label(
            block,
            text=helper,
            bg=Palette.BG,
            fg=Palette.DIM,
            font=(FONT_UI, 9),
            anchor="e",
        ).grid(row=0, column=1, sticky="e")
        entry = self._entry(block, variable)
        entry.grid(row=1, column=0, sticky="ew", pady=(6, 0), ipady=9)
        self._button(block, "CHỌN", command, primary=False).grid(
            row=1, column=1, sticky="e", padx=(10, 0), pady=(6, 0)
        )

    def _inputs_changed(self, *_args: object) -> None:
        if self._suspend_trace:
            return
        if self.verified_content is not None:
            try:
                current = Path(self.content_var.get()).expanduser().resolve()
            except OSError:
                current = None
            if current != self.verified_content:
                self.verified_content = None
                self.status_var.set("Content đã thay đổi, cần kiểm tra lại")
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
        self.status_var.set("Đã nạp project, cần kiểm tra content")
        self._log(f"Đã nạp project: {project_dir}")
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

    def _browse_xml(self) -> None:
        selected = filedialog.askopenfilename(
            title="Chọn XML blueprint",
            initialdir=self.project_var.get() or str(Path.cwd()),
            filetypes=(("XML", "*.xml"), ("All files", "*.*")),
        )
        if selected:
            self.xml_var.set(path_text(selected))

    def _browse_video(self) -> None:
        selected = filedialog.askdirectory(
            title="Chọn thư mục video",
            initialdir=self.video_var.get() or self.project_var.get() or str(Path.cwd()),
        )
        if selected:
            self.video_var.set(path_text(selected))

    def _browse_audio(self) -> None:
        selected = filedialog.askdirectory(
            title="Chọn thư mục audio",
            initialdir=self.audio_var.get() or self.project_var.get() or str(Path.cwd()),
        )
        if selected:
            self.audio_var.set(path_text(selected))

    def _browse_output(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Chọn XML đầu ra",
            initialdir=self.project_var.get() or str(Path.cwd()),
            initialfile=Path(self.output_var.get()).name if self.output_var.get() else "output_v3.xml",
            defaultextension=".xml",
            filetypes=(("XML", "*.xml"),),
        )
        if selected:
            self.output_var.set(path_text(selected))

    def _review_content(self) -> None:
        raw_path = self.content_var.get().strip()
        if not raw_path:
            self._show_error("Hãy chọn content.docx trước")
            return
        content_path = Path(raw_path).expanduser().resolve()
        if not content_path.is_file():
            self._show_error(f"Không tìm thấy content.docx: {content_path}")
            return
        try:
            components = inspect_docx_components(content_path)
        except GeneratorError as exc:
            self._show_error(str(exc))
            return

        dialog = ComponentReviewDialog(self.root, content_path, components)
        if dialog.show():
            self.verified_content = content_path
            cut_count = sum(item.video_cut_count for item in components if item.key != "outro")
            self.status_var.set("Content đã xác nhận, sẵn sàng tạo XML")
            self._log(f"Checklist đã xác nhận: 8 thành phần, {cut_count} video cut")
        else:
            self.verified_content = None
            self.status_var.set("Checklist chưa được xác nhận")
            self._log("Đã đóng checklist mà chưa xác nhận.")
        self._update_generate_state()

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

    def _update_generate_state(self) -> None:
        if self.generate_button is None:
            return
        enabled = (
            not self.is_running
            and self.verified_content is not None
            and self._required_paths_ready()
        )
        self.generate_button.configure(state="normal" if enabled else "disabled")

    def _generate(self) -> None:
        if self.verified_content is None:
            self._show_error("Hãy kiểm tra và xác nhận content.docx trước")
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
        self.status_var.set("Đang kiểm tra media và tạo XML")
        if self.generate_button is not None:
            self.generate_button.configure(text="ĐANG TẠO...", state="disabled")
        self._log("Bắt đầu kiểm tra 6 video và 103 voice-over...")

        def worker() -> None:
            try:
                summary = generate_project_xml(**snapshot)
            except Exception as exc:
                self.root.after(0, lambda error=exc: self._generation_failed(error))
                return
            self.root.after(0, lambda: self._generation_succeeded(summary))

        threading.Thread(target=worker, daemon=True).start()

    def _generation_succeeded(self, summary: GenerationSummary) -> None:
        self.is_running = False
        if self.generate_button is not None:
            self.generate_button.configure(text="TẠO XML")
        self.status_var.set("XML đã tạo và kiểm tra thành công")
        self._update_generate_state()
        self._log(
            f"Hoàn tất: {summary.video_cut_count} video cut, "
            f"{summary.narration_count} voice-over, {summary.duration_seconds:.2f}s"
        )
        self._log(f"Output: {summary.output}")
        messagebox.showinfo(
            "Đã tạo XML",
            f"File đã sẵn sàng để import vào Premiere:\n\n{summary.output}",
            parent=self.root,
        )

    def _generation_failed(self, error: Exception) -> None:
        self.is_running = False
        if self.generate_button is not None:
            self.generate_button.configure(text="TẠO XML")
        self.status_var.set("Tạo XML thất bại")
        self._update_generate_state()
        self._show_error(str(error))

    def _show_error(self, message: str) -> None:
        self.status_var.set("Cần xử lý lỗi trước khi tiếp tục")
        self._log(f"LỖI: {message}")
        messagebox.showerror("Không thể tiếp tục", message, parent=self.root)

    def _log(self, message: str) -> None:
        if self.log_text is None:
            return
        self.log_text.configure(state="normal")
        current = self.log_text.get("1.0", "end-1c")
        if current.startswith("Chưa có tác vụ."):
            self.log_text.delete("1.0", "end")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    root = tk.Tk()
    XmlGeneratorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
