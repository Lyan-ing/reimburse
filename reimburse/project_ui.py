from __future__ import annotations

import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .capture import ScreenCapturer, clipboard_files, clipboard_image
from .classify import classify
from .config import load_settings, save_settings
from .extract import Extractor
from .models import SECTION_NAMES
from .parser import parse_material
from .project_docgen import build_project_document, project_filename
from .project_models import (
    PROJECT_TYPES,
    RECORD_TEMPLATES,
    SLOT_ICONS,
    SLOT_LABELS,
    STATUSES,
    STATUS_ORDER,
    Asset,
    Project,
    ProjectReminder,
    Record,
    sanitize_filename,
)
from .project_store import Workspace
from .ui import COLORS, enable_dpi_awareness


APP_TITLE = "报销台账"


def _geometry_pos(x: int, y: int) -> str:
    sx = f"+{x}" if x >= 0 else str(x)
    sy = f"+{y}" if y >= 0 else str(y)
    return f"{sx}{sy}"


def _geometry_on_screen(value: str, screen_w: int, screen_h: int) -> bool:
    match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", (value or "").strip())
    if not match:
        return False
    width, height, x, y = map(int, match.groups())
    if width < 600 or height < 420:
        return False
    if width > screen_w + 100 or height > screen_h + 100:
        return False
    return x < screen_w - 100 and y < screen_h - 100 and x + width > 100 and y + height > 100


def prepare_dialog(dialog: tk.Toplevel, parent: tk.Misc, width: int, height: int) -> None:
    """Center a modal dialog over its parent and grab input only after it is visible."""
    scale = float(getattr(parent, "ui_scale", 1.0) or 1.0)
    w = max(320, int(width * scale))
    h = max(240, int(height * scale))
    parent.update_idletasks()
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    pw = max(parent.winfo_width(), 640)
    ph = max(parent.winfo_height(), 480)
    x = px + (pw - w) // 2
    y = py + (ph - h) // 2
    sw = dialog.winfo_screenwidth()
    sh = dialog.winfo_screenheight()
    x = max(0, min(x, max(0, sw - w - 20)))
    y = max(0, min(y, max(0, sh - h - 60)))
    dialog.geometry(f"{w}x{h}{_geometry_pos(x, y)}")
    dialog.deiconify()
    dialog.lift()
    dialog.focus_force()

    def activate() -> None:
        try:
            if dialog.winfo_exists():
                dialog.grab_set()
                dialog.lift()
                dialog.focus_force()
        except tk.TclError:
            pass

    dialog.after(60, activate)


class ScrollFrame(tk.Frame):
    def __init__(self, parent, bg: str = COLORS["panel"]):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_body_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self.window, width=event.width)

    def _bind_wheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta * 3, "units")

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0)


class NewProjectDialog(tk.Toplevel):
    def __init__(self, parent, project: Project | None = None):
        super().__init__(parent)
        self.title("编辑项目" if project else "新建报销项目")
        self.resizable(False, False)
        self.transient(parent)
        self.configure(bg=COLORS["bg"])
        self.result: dict | None = None
        self.project = project

        body = tk.Frame(self, bg=COLORS["panel"], padx=22, pady=18, highlightbackground=COLORS["line"], highlightthickness=1)
        body.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(body, text="报销项目", font=("Microsoft YaHei UI", 14, "bold"), fg=COLORS["accent"], bg=COLORS["panel"]).pack(anchor="w")
        tk.Label(body, text="一个项目 = 一次出差 / 参会 / 采购", font=("Microsoft YaHei UI", 9), fg=COLORS["muted"], bg=COLORS["panel"]).pack(anchor="w", pady=(2, 14))

        self.name_var = tk.StringVar(value=project.name if project else "")
        self.type_var = tk.StringVar(value=project.type if project else "出差")
        self.start_var = tk.StringVar(value=project.date_start if project else date.today().isoformat())
        self.end_var = tk.StringVar(value=project.date_end if project else date.today().isoformat())
        self.applicant_var = tk.StringVar(value=project.applicant if project else "")
        self.department_var = tk.StringVar(value=project.department if project else "")

        form = tk.Frame(body, bg=COLORS["panel"])
        form.pack(fill="both", expand=True)
        fields = [
            ("项目名称", ttk.Entry(form, textvariable=self.name_var)),
            ("类型", ttk.Combobox(form, textvariable=self.type_var, values=PROJECT_TYPES, state="readonly")),
            ("开始日期", ttk.Entry(form, textvariable=self.start_var)),
            ("结束日期", ttk.Entry(form, textvariable=self.end_var)),
            ("报销人", ttk.Entry(form, textvariable=self.applicant_var)),
            ("部门", ttk.Entry(form, textvariable=self.department_var)),
        ]
        for row, (label, widget) in enumerate(fields):
            tk.Label(form, text=label, font=("Microsoft YaHei UI", 9), fg=COLORS["muted"], bg=COLORS["panel"]).grid(row=row, column=0, sticky="w", pady=7, padx=(0, 12))
            widget.grid(row=row, column=1, sticky="ew", pady=7)
        form.columnconfigure(1, weight=1)

        buttons = tk.Frame(form, bg=COLORS["panel"])
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(18, 0))
        ttk.Button(buttons, text="取消", style="Soft.TButton", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="保存", style="Accent.TButton", command=self._save).pack(side="right")
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self._save())
        prepare_dialog(self, parent, 520, 480)
        self.after(120, lambda: fields[0][1].focus_set())

    def _save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("项目名称", "请填写项目名称。", parent=self)
            return
        try:
            start = date.fromisoformat(self.start_var.get().strip())
            end = date.fromisoformat(self.end_var.get().strip())
        except ValueError:
            messagebox.showwarning("日期格式", "日期格式应为 2026-10-19。", parent=self)
            return
        if end < start:
            messagebox.showwarning("日期范围", "结束日期不能早于开始日期。", parent=self)
            return
        self.result = {
            "name": name,
            "type": self.type_var.get(),
            "date_start": start.isoformat(),
            "date_end": end.isoformat(),
            "applicant": self.applicant_var.get().strip(),
            "department": self.department_var.get().strip(),
        }
        self.destroy()


class AddRecordDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("添加费用记录")
        self.resizable(False, False)
        self.transient(parent)
        self.configure(bg=COLORS["bg"])
        self.result: str | None = None
        body = tk.Frame(self, bg=COLORS["panel"], padx=22, pady=18, highlightbackground=COLORS["line"], highlightthickness=1)
        body.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(body, text="选择这笔费用的类型", font=("Microsoft YaHei UI", 14, "bold"), fg=COLORS["accent"], bg=COLORS["panel"]).pack(anchor="w")
        tk.Label(body, text="软件会按类型生成需要的素材槽位。", font=("Microsoft YaHei UI", 9), fg=COLORS["muted"], bg=COLORS["panel"]).pack(anchor="w", pady=(2, 14))
        grid = tk.Frame(body, bg=COLORS["panel"])
        grid.pack(fill="both", expand=True)
        for index, (key, template) in enumerate(RECORD_TEMPLATES.items()):
            card = tk.Frame(grid, bg="#F7FCFE", highlightbackground=COLORS["line"], highlightthickness=1, bd=0, cursor="hand2")
            card.grid(row=index // 3, column=index % 3, sticky="nsew", padx=5, pady=5)
            icon = tk.Label(card, text=template["icon"], font=("Segoe UI Emoji", 24), bg="#F7FCFE")
            icon.pack(pady=(14, 4))
            title = tk.Label(card, text=template["label"], font=("Microsoft YaHei UI", 11, "bold"), fg=COLORS["ink"], bg="#F7FCFE")
            title.pack()
            slots = " · ".join(SLOT_LABELS.get(s, s) for s in template["slots"] if s in template.get("required", []))
            desc = tk.Label(card, text=slots, font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg="#F7FCFE", wraplength=150)
            desc.pack(pady=(2, 12))
            for widget in (card, icon, title, desc):
                widget.bind("<Button-1>", lambda _event, k=key: self._choose(k))
        for col in range(3):
            grid.columnconfigure(col, weight=1)
        for row in range(2):
            grid.rowconfigure(row, weight=1)
        ttk.Button(body, text="取消", style="Soft.TButton", command=self.destroy).pack(anchor="e", pady=(10, 0))
        self.bind("<Escape>", lambda _event: self.destroy())
        prepare_dialog(self, parent, 620, 420)

    def _choose(self, key: str) -> None:
        self.result = key
        self.destroy()


class ProjectApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.title(APP_TITLE)
        self.ui_scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = min(int(1280 * self.ui_scale), screen_w - 80)
        height = min(int(840 * self.ui_scale), screen_h - 120)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        saved_geometry = self.settings.get("project_window_geometry", "")
        if not _geometry_on_screen(saved_geometry, screen_w, screen_h):
            saved_geometry = f"{width}x{height}{_geometry_pos(x, y)}"
        self.geometry(saved_geometry)
        self.minsize(min(int(1100 * self.ui_scale), screen_w - 80), min(int(700 * self.ui_scale), screen_h - 120))
        self.configure(bg=COLORS["bg"])

        self.workspace_root = Path(
            self.settings.get("workspace_root")
            or (Path.home() / "Documents" / "报销台账")
        )
        self.workspace = Workspace(self.workspace_root)
        self.project: Project | None = None
        self.current_record_id: str | None = None
        self.project_scroll: ScrollFrame | None = None
        self.record_scroll: ScrollFrame | None = None
        self.editor_scroll: ScrollFrame | None = None
        self.content: tk.Frame | None = None
        self.field_vars: dict[str, tk.StringVar] = {}
        self.ocr_labels: dict[tuple[str, str], tk.Label] = {}
        self._thumb_refs: dict[str, ImageTk.PhotoImage] = {}
        self._ocr_jobs: set[tuple[str, str, str]] = set()
        self.ui_queue: queue.Queue = queue.Queue()
        self._status_by_label = {v["label"]: k for k, v in STATUSES.items()}
        self._status_labels = [STATUSES[k]["label"] for k in STATUS_ORDER]
        self.ocr_enabled = tk.BooleanVar(value=bool(self.settings.get("semi_ocr", True)))
        self.clock_var = tk.StringVar(value=datetime.now().strftime("%H:%M:%S"))
        self.date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self.count_var = tk.StringVar(value="")

        self._configure_style()
        self._build_shell()
        self._refresh_projects()
        self.after(100, self._poll_ui_queue)
        last_id = self.settings.get("last_project_id", "")
        last_record = self.settings.get("last_project_record", "")
        if last_id:
            project = self.workspace.find(last_id)
            if project:
                self.current_record_id = last_record or None
                self.open_project(project)
        self.after(0, self._tick_clock)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def px(self, value: int | float) -> int:
        return int(round(value * self.ui_scale))

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        base_font = ("Microsoft YaHei UI", 10)
        self.option_add("*Font", base_font)
        style.configure(".", font=base_font, background=COLORS["bg"], foreground=COLORS["ink"])
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["ink"])
        style.configure("Card.TLabel", background=COLORS["panel"], foreground=COLORS["ink"])
        style.configure("Muted.TLabel", foreground=COLORS["muted"])
        style.configure(
            "Accent.TButton",
            font=("Microsoft YaHei UI", 10, "bold"),
            foreground="white",
            background=COLORS["accent"],
            borderwidth=0,
            padding=(self.px(14), self.px(8)),
            focusthickness=0,
        )
        style.map(
            "Accent.TButton",
            background=[("active", COLORS["accent_hover"]), ("pressed", COLORS["accent_hover"]), ("disabled", "#A9C8CE")],
            foreground=[("disabled", "#F4FBFC")],
        )
        style.configure(
            "Soft.TButton",
            font=("Microsoft YaHei UI", 9),
            foreground=COLORS["ink"],
            background=COLORS["soft"],
            borderwidth=0,
            padding=(self.px(10), self.px(6)),
            focusthickness=0,
        )
        style.map("Soft.TButton", background=[("active", "#D7ECEF"), ("pressed", "#CDE5E9")])
        style.configure(
            "Danger.TButton",
            font=("Microsoft YaHei UI", 9),
            foreground=COLORS["error"],
            background="#FCEBEA",
            borderwidth=0,
            padding=(self.px(10), self.px(6)),
            focusthickness=0,
        )
        style.map("Danger.TButton", background=[("active", "#F7D8D5")])
        style.configure(
            "TEntry",
            fieldbackground="white",
            foreground=COLORS["ink"],
            bordercolor=COLORS["line"],
            lightcolor=COLORS["line"],
            darkcolor=COLORS["line"],
            padding=self.px(7),
        )
        style.configure(
            "TCombobox",
            fieldbackground="white",
            background="white",
            foreground=COLORS["ink"],
            bordercolor=COLORS["line"],
            lightcolor=COLORS["line"],
            darkcolor=COLORS["line"],
            padding=self.px(5),
        )
        style.map("TCombobox", fieldbackground=[("readonly", "white")], foreground=[("readonly", COLORS["ink"])])
        style.configure("TCheckbutton", background=COLORS["bg"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.map("TCheckbutton", background=[("active", COLORS["bg"])], foreground=[("active", COLORS["ink"])])
        style.configure("Horizontal.TProgressbar", troughcolor="#DDEFF2", background=COLORS["accent"], borderwidth=0)
        style.configure("TPanedwindow", background=COLORS["bg"])
        style.configure("Sash", sashthickness=self.px(8), background=COLORS["bg"])

    def _tick_clock(self) -> None:
        now = datetime.now()
        self.clock_var.set(now.strftime("%H:%M:%S"))
        self.date_var.set(now.strftime("%Y-%m-%d"))
        self.after(1000, self._tick_clock)

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                item = self.ui_queue.get_nowait()
                if item[0] == "ocr":
                    _, project_id, record_id, slot, asset_id, data, error = item
                    self._apply_ocr(project_id, record_id, slot, asset_id, data, error)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(100, self._poll_ui_queue)
        except tk.TclError:
            pass

    def _card(self, parent, pad: int = 0, bg: str | None = None):
        bg = bg or COLORS["panel"]
        outer = tk.Frame(parent, bg=bg, highlightbackground=COLORS["line"], highlightthickness=1, bd=0)
        inner = tk.Frame(outer, bg=bg, bd=0)
        inner.pack(fill="both", expand=True, padx=self.px(pad), pady=self.px(pad))
        return outer, inner

    def _build_shell(self) -> None:
        outer = tk.Frame(self, bg=COLORS["bg"], padx=self.px(16), pady=self.px(10))
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=COLORS["bg"])
        header.pack(fill="x")
        brand = tk.Frame(header, bg=COLORS["bg"])
        brand.pack(side="left")
        tk.Label(brand, text="🧾", font=("Segoe UI Emoji", 24), bg=COLORS["bg"]).pack(side="left")
        title_box = tk.Frame(brand, bg=COLORS["bg"])
        title_box.pack(side="left", padx=(10, 0))
        line = tk.Frame(title_box, bg=COLORS["bg"])
        line.pack(anchor="w")
        tk.Label(line, text="报销台账", font=("Microsoft YaHei UI", 18, "bold"), fg=COLORS["ink"], bg=COLORS["bg"]).pack(side="left")
        tk.Label(line, text="v2.0", font=("Microsoft YaHei UI", 8, "bold"), fg=COLORS["accent"], bg=COLORS["chip"], padx=self.px(7), pady=self.px(1)).pack(side="left", padx=(8, 0), pady=(2, 0))
        tk.Label(title_box, text="半自动整理 · 项目台账 · 历史状态", font=("Microsoft YaHei UI", 9), fg=COLORS["muted"], bg=COLORS["bg"]).pack(anchor="w", pady=(1, 0))

        right = tk.Frame(header, bg=COLORS["bg"])
        right.pack(side="right")
        tk.Label(right, textvariable=self.date_var, font=("Consolas", 9), fg=COLORS["muted"], bg=COLORS["bg"]).pack(side="left", padx=(0, 8))
        tk.Label(right, textvariable=self.clock_var, font=("Consolas", 9, "bold"), fg=COLORS["accent"], bg=COLORS["bg"]).pack(side="left", padx=(0, 12))
        ttk.Button(right, text="📂 台账目录", style="Soft.TButton", command=self.choose_workspace).pack(side="left", padx=(0, 6))
        ttk.Button(right, text="＋ 新建项目", style="Accent.TButton", command=self.new_project).pack(side="left")

        tk.Frame(outer, bg=COLORS["accent"], height=max(2, self.px(2))).pack(fill="x", pady=(self.px(9), self.px(10)))

        body = tk.Frame(outer, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        sidebar_outer, sidebar = self._card(body, pad=self.px(10))
        sidebar_outer.grid(row=0, column=0, sticky="nsw", padx=(0, self.px(10)))
        sidebar_outer.configure(width=self.px(278))
        sidebar_outer.grid_propagate(False)
        sidebar.configure(width=self.px(258))
        self._build_sidebar(sidebar)

        self.content = tk.Frame(body, bg=COLORS["bg"])
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.rowconfigure(0, weight=1)
        self.content.columnconfigure(0, weight=1)
        self._render_empty()

    def _build_sidebar(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, bg=COLORS["panel"])
        header.pack(fill="x")
        tk.Label(header, text="📚 历史项目", font=("Microsoft YaHei UI", 10, "bold"), fg=COLORS["ink"], bg=COLORS["panel"]).pack(side="left")
        tk.Label(header, textvariable=self.count_var, font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["panel"]).pack(side="right")

        self.search_var = tk.StringVar(value="")
        search = ttk.Entry(parent, textvariable=self.search_var)
        search.pack(fill="x", pady=(self.px(9), self.px(6)))
        self.search_var.trace_add("write", lambda *_: self._refresh_projects())

        self.status_filter_var = tk.StringVar(value="全部")
        filter_box = ttk.Combobox(
            parent,
            textvariable=self.status_filter_var,
            values=["全部"] + self._status_labels,
            state="readonly",
        )
        filter_box.pack(fill="x", pady=(0, self.px(8)))
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_projects())

        self.project_scroll = ScrollFrame(parent, bg=COLORS["panel"])
        self.project_scroll.pack(fill="both", expand=True)

        buttons = tk.Frame(parent, bg=COLORS["panel"])
        buttons.pack(fill="x", pady=(self.px(8), 0))
        ttk.Button(buttons, text="📥 导入文件夹", style="Soft.TButton", command=self.import_folder).pack(fill="x")

    def _render_empty(self) -> None:
        if self.content is None:
            return
        for child in self.content.winfo_children():
            child.destroy()
        wrap = tk.Frame(self.content, bg=COLORS["bg"])
        wrap.place(relx=0.5, rely=0.45, anchor="center")
        tk.Label(wrap, text="🧾", font=("Segoe UI Emoji", 46), bg=COLORS["bg"]).pack()
        tk.Label(wrap, text="还没有打开报销项目", font=("Microsoft YaHei UI", 16, "bold"), fg=COLORS["ink"], bg=COLORS["bg"]).pack(pady=(8, 4))
        tk.Label(wrap, text="新建一个项目，或从左侧历史台账里选择。", font=("Microsoft YaHei UI", 9), fg=COLORS["muted"], bg=COLORS["bg"]).pack()
        ttk.Button(wrap, text="＋ 新建报销项目", style="Accent.TButton", command=self.new_project).pack(pady=(16, 0))

    def _on_close(self) -> None:
        self.settings.update(
            {
                "workspace_root": str(self.workspace_root),
                "semi_ocr": self.ocr_enabled.get(),
                "project_window_geometry": self.geometry(),
                "last_project_id": self.project.id if self.project else "",
                "last_project_record": self.current_record_id or "",
            }
        )
        save_settings(self.settings)
        self.destroy()

    def _refresh_projects(self) -> None:
        if self.project_scroll is None:
            return
        self.project_scroll.clear()
        status = "all"
        if self.status_filter_var.get() != "全部":
            status = self._status_by_label.get(self.status_filter_var.get(), "all")
        projects = self.workspace.filter(self.search_var.get(), status)
        self.count_var.set(f"{len(projects)} 个")
        if not projects:
            tk.Label(
                self.project_scroll.body,
                text="没有符合条件的项目。\n\n新建项目，或切换筛选条件。",
                font=("Microsoft YaHei UI", 9),
                fg=COLORS["muted"],
                bg=COLORS["panel"],
                justify="center",
            ).pack(pady=self.px(30))
            return
        for project in projects:
            self._add_project_card(project)

    def _add_project_card(self, project: Project) -> None:
        selected = self.project is not None and project.folder == self.project.folder
        bg = "#F0FAFB" if selected else COLORS["panel"]
        card = tk.Frame(
            self.project_scroll.body,
            bg=bg,
            highlightbackground=COLORS["accent"] if selected else COLORS["line"],
            highlightthickness=1,
            bd=0,
            cursor="hand2",
        )
        card.pack(fill="x", pady=(0, self.px(7)), padx=self.px(1))
        bar = tk.Frame(card, bg=project.status_color, width=self.px(4))
        bar.pack(side="left", fill="y")
        body = tk.Frame(card, bg=bg, padx=self.px(9), pady=self.px(8))
        body.pack(side="left", fill="both", expand=True)

        top = tk.Frame(body, bg=bg)
        top.pack(fill="x")
        name = tk.Label(
            top,
            text=project.name,
            font=("Microsoft YaHei UI", 10, "bold"),
            fg=COLORS["ink"],
            bg=bg,
            anchor="w",
            justify="left",
        )
        name.pack(side="left", fill="x", expand=True)
        status = tk.Label(
            top,
            text=project.status_label,
            font=("Microsoft YaHei UI", 7, "bold"),
            fg=project.status_color,
            bg=project.status_bg,
            padx=self.px(6),
            pady=self.px(1),
        )
        status.pack(side="right")

        meta = tk.Label(
            body,
            text=f"{project.date_range_text}  ·  ¥{project.total():,.2f}",
            font=("Microsoft YaHei UI", 8),
            fg=COLORS["muted"],
            bg=bg,
            anchor="w",
        )
        meta.pack(fill="x", pady=(self.px(3), 0))
        missing = project.missing_count()
        foot = tk.Frame(body, bg=bg)
        foot.pack(fill="x", pady=(self.px(4), 0))
        tk.Label(
            foot,
            text=f"{len(project.records)} 条记录",
            font=("Microsoft YaHei UI", 8),
            fg=COLORS["muted"],
            bg=bg,
        ).pack(side="left")
        if missing:
            tk.Label(
                foot,
                text=f"待补 {missing}",
                font=("Microsoft YaHei UI", 8, "bold"),
                fg=COLORS["warn"],
                bg=bg,
            ).pack(side="right")
        else:
            tk.Label(foot, text="材料齐全", font=("Microsoft YaHei UI", 8), fg=COLORS["leaf"], bg=bg).pack(side="right")

        for widget in (card, bar, body, top, name, status, meta, foot):
            widget.bind("<Button-1>", lambda _event, p=project: self.open_project(p))
        for child in foot.winfo_children():
            child.bind("<Button-1>", lambda _event, p=project: self.open_project(p))

    def new_project(self) -> None:
        try:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("台账目录", f"无法创建台账目录：\n{exc}", parent=self)
            return
        dialog = NewProjectDialog(self)
        self.wait_window(dialog)
        if not dialog.result:
            return
        try:
            data = dict(dialog.result)
            data["project_type"] = data.pop("type")
            project = self.workspace.create(**data)
        except Exception as exc:
            messagebox.showerror("新建失败", str(exc), parent=self)
            return
        self._refresh_projects()
        self.open_project(project)

    def edit_project(self) -> None:
        if not self.project:
            return
        dialog = NewProjectDialog(self, self.project)
        self.wait_window(dialog)
        if not dialog.result:
            return
        for key, value in dialog.result.items():
            setattr(self.project, key, value)
        self.project.save()
        self._render_project()
        self._refresh_projects()

    def choose_workspace(self) -> None:
        folder = filedialog.askdirectory(title="选择报销台账目录", initialdir=str(self.workspace_root))
        if not folder:
            return
        self.workspace_root = Path(folder)
        self.workspace = Workspace(self.workspace_root)
        self.project = None
        self.current_record_id = None
        self.settings["workspace_root"] = str(self.workspace_root)
        save_settings(self.settings)
        self._refresh_projects()
        self._render_empty()

    def import_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择已有的报销文件夹")
        if not folder:
            return
        if not messagebox.askyesno(
            "导入文件夹",
            "会把这个文件夹复制到台账目录，并在副本里建立项目记录。\n原文件夹不会被修改。\n\n继续吗？",
            parent=self,
        ):
            return
        try:
            project = self.workspace.register_folder(Path(folder), copy=True)
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc), parent=self)
            return
        self._refresh_projects()
        self.open_project(project)

    def open_project(self, project: Project) -> None:
        self.project = project
        self.project.sort_records()
        if self.current_record_id and not project.find_record(self.current_record_id):
            self.current_record_id = None
        if not self.current_record_id and project.records:
            self.current_record_id = project.records[0].id
        self.settings["last_project_id"] = project.id
        self._render_project()
        self._refresh_projects()

    def _render_project(self) -> None:
        if self.content is None or self.project is None:
            return
        for child in self.content.winfo_children():
            child.destroy()
        project = self.project
        project.sort_records()

        header_outer, header = self._card(self.content, pad=self.px(12))
        header_outer.pack(fill="x", pady=(0, self.px(8)))
        top = tk.Frame(header, bg=COLORS["panel"])
        top.pack(fill="x")
        left = tk.Frame(top, bg=COLORS["panel"])
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=project.name, font=("Microsoft YaHei UI", 15, "bold"), fg=COLORS["ink"], bg=COLORS["panel"], anchor="w").pack(anchor="w")
        meta = f"{project.type}  ·  {project.date_range_text}"
        if project.applicant:
            meta += f"  ·  {project.applicant}"
        if project.department:
            meta += f"  ·  {project.department}"
        tk.Label(left, text=meta, font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["panel"]).pack(anchor="w", pady=(2, 0))

        actions = tk.Frame(top, bg=COLORS["panel"])
        actions.pack(side="right")
        ttk.Checkbutton(actions, text="OCR 辅助", variable=self.ocr_enabled, command=self._save_ocr_setting).pack(side="left", padx=(0, 8))
        self.status_var = tk.StringVar(value=project.status_label)
        status_combo = ttk.Combobox(actions, textvariable=self.status_var, values=self._status_labels, state="readonly", width=10)
        status_combo.pack(side="left", padx=(0, 8))
        status_combo.bind("<<ComboboxSelected>>", lambda _event: self.change_status())
        ttk.Button(actions, text="⚙ 项目设置", style="Soft.TButton", command=self.edit_project).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="🗑 删除", style="Danger.TButton", command=self.delete_project).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="📄 生成 Word", style="Accent.TButton", command=self.generate_word).pack(side="left")

        stats = tk.Frame(self.content, bg=COLORS["bg"])
        stats.pack(fill="x", pady=(0, self.px(8)))
        self._build_stats(stats)

        body = ttk.PanedWindow(self.content, orient=tk.HORIZONTAL)
        body.pack(fill="both", expand=True)
        records_outer, records = self._card(body, pad=0)
        body.add(records_outer, weight=2)
        editor_outer, editor = self._card(body, pad=0)
        body.add(editor_outer, weight=3)

        rec_head = tk.Frame(records, bg=COLORS["panel"], padx=self.px(10), pady=self.px(8))
        rec_head.pack(fill="x")
        tk.Label(rec_head, text="📋 费用记录", font=("Microsoft YaHei UI", 10, "bold"), fg=COLORS["ink"], bg=COLORS["panel"]).pack(side="left")
        ttk.Button(rec_head, text="＋ 添加", style="Soft.TButton", command=self.add_record).pack(side="right")
        self.record_scroll = ScrollFrame(records, bg=COLORS["panel"])
        self.record_scroll.pack(fill="both", expand=True)
        self._render_records()

        self.editor_scroll = ScrollFrame(editor, bg=COLORS["panel"])
        self.editor_scroll.pack(fill="both", expand=True)
        self._render_editor()

    def _build_stats(self, parent: tk.Frame) -> None:
        project = self.project
        if project is None:
            return
        self.stat_value_labels = {}
        items = [
            ("💰", "总额", f"¥{project.total():,.2f}", COLORS["accent"]),
            ("📋", "费用记录", f"{len(project.records)} 条", COLORS["sky"]),
            ("⚠", "待补", f"{project.missing_count()} 项", COLORS["warn"] if project.missing_count() else COLORS["leaf"]),
            ("🏷", "状态", project.status_label, project.status_color),
        ]
        for index, (icon, label, value, color) in enumerate(items):
            card = tk.Frame(parent, bg=COLORS["panel"], highlightbackground=COLORS["line"], highlightthickness=1, bd=0)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 7, 0))
            tk.Frame(card, bg=color, width=self.px(3)).pack(side="left", fill="y")
            body = tk.Frame(card, bg=COLORS["panel"], padx=self.px(10), pady=self.px(6))
            body.pack(side="left", fill="both", expand=True)
            tk.Label(body, text=f"{icon} {label}", font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["panel"]).pack(anchor="w")
            value_label = tk.Label(body, text=value, font=("Microsoft YaHei UI", 12, "bold"), fg=color, bg=COLORS["panel"])
            value_label.pack(anchor="w")
            self.stat_value_labels[label] = value_label
            parent.columnconfigure(index, weight=1)

    def _save_ocr_setting(self) -> None:
        self.settings["semi_ocr"] = self.ocr_enabled.get()
        save_settings(self.settings)

    def change_status(self) -> None:
        if not self.project:
            return
        status = self._status_by_label.get(self.status_var.get())
        if not status:
            return
        self.project.status = status
        self.project.save()
        self._render_project()
        self._refresh_projects()

    def delete_project(self) -> None:
        if not self.project:
            return
        if not messagebox.askyesno(
            "删除项目",
            f"确定删除“{self.project.name}”吗？\n\n项目文件夹会移到台账目录的“_回收站”。",
            parent=self,
            icon="warning",
        ):
            return
        self.workspace.remove_project(self.project)
        self.project = None
        self.current_record_id = None
        self._refresh_projects()
        self._render_empty()

    def add_record(self) -> None:
        if not self.project:
            return
        dialog = AddRecordDialog(self)
        self.wait_window(dialog)
        if not dialog.result:
            return
        record = self.project.add_record(dialog.result, self.project.date_start)
        self.project.save()
        self.current_record_id = record.id
        self._render_records()
        self._render_editor()
        self._refresh_projects()

    def _render_records(self) -> None:
        if self.record_scroll is None or self.project is None:
            return
        self.record_scroll.clear()
        project = self.project
        if not project.records:
            tk.Label(
                self.record_scroll.body,
                text="还没有费用记录。\n\n点击右上角“＋ 添加”开始。",
                font=("Microsoft YaHei UI", 9),
                fg=COLORS["muted"],
                bg=COLORS["panel"],
                justify="center",
            ).pack(pady=self.px(30))
            return
        for record in project.records:
            selected = record.id == self.current_record_id
            bg = "#F0FAFB" if selected else COLORS["panel"]
            card = tk.Frame(
                self.record_scroll.body,
                bg=bg,
                highlightbackground=COLORS["accent"] if selected else COLORS["line"],
                highlightthickness=1,
                bd=0,
                cursor="hand2",
            )
            card.pack(fill="x", pady=(0, self.px(6)), padx=self.px(1))
            body = tk.Frame(card, bg=bg, padx=self.px(9), pady=self.px(7))
            body.pack(fill="both", expand=True)
            top = tk.Frame(body, bg=bg)
            top.pack(fill="x")
            tk.Label(top, text=record.icon, font=("Segoe UI Emoji", 13), bg=bg).pack(side="left")
            tk.Label(
                top,
                text=record.display_title,
                font=("Microsoft YaHei UI", 9, "bold"),
                fg=COLORS["ink"],
                bg=bg,
                anchor="w",
            ).pack(side="left", padx=(6, 0), fill="x", expand=True)
            tk.Label(
                top,
                text=f"¥{record.effective_amount:,.2f}",
                font=("Microsoft YaHei UI", 9, "bold"),
                fg=COLORS["accent"],
                bg=bg,
            ).pack(side="right")
            meta = tk.Frame(body, bg=bg)
            meta.pack(fill="x", pady=(self.px(3), 0))
            tk.Label(meta, text=record.date, font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=bg).pack(side="left")
            tk.Label(meta, text=f"· {record.label}", font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=bg).pack(side="left", padx=(5, 0))
            missing = [m for m in record.missing_slots() if m[2] == "error"]
            if missing:
                tk.Label(
                    meta,
                    text=f"待补 {len(missing)}",
                    font=("Microsoft YaHei UI", 8, "bold"),
                    fg=COLORS["warn"],
                    bg=bg,
                ).pack(side="right")
            elif record.has_slot("invoice") and record.has_slot("payment"):
                tk.Label(meta, text="✓ 齐全", font=("Microsoft YaHei UI", 8), fg=COLORS["leaf"], bg=bg).pack(side="right")

            for widget in (card, body, top, meta):
                widget.bind("<Button-1>", lambda _event, rid=record.id: self.select_record(rid))
            for child in body.winfo_children():
                child.bind("<Button-1>", lambda _event, rid=record.id: self.select_record(rid))
            for child in top.winfo_children():
                child.bind("<Button-1>", lambda _event, rid=record.id: self.select_record(rid))

    def select_record(self, record_id: str) -> None:
        self.current_record_id = record_id
        self._render_records()
        self._render_editor()
        self.settings["last_project_record"] = record_id

    def _record(self) -> Record | None:
        if not self.project or not self.current_record_id:
            return None
        return self.project.find_record(self.current_record_id)

    def _render_editor(self) -> None:
        if self.editor_scroll is None or self.project is None:
            return
        self.editor_scroll.clear()
        record = self._record()
        if record is None:
            tk.Label(
                self.editor_scroll.body,
                text="选择左侧一条费用记录，\n在这里添加发票、支付记录和其他材料。",
                font=("Microsoft YaHei UI", 9),
                fg=COLORS["muted"],
                bg=COLORS["panel"],
                justify="center",
            ).pack(pady=self.px(50))
            return

        top = tk.Frame(self.editor_scroll.body, bg=COLORS["panel"], padx=self.px(12), pady=self.px(8))
        top.pack(fill="x")
        tk.Label(top, text=f"{record.icon} {record.label}", font=("Microsoft YaHei UI", 12, "bold"), fg=COLORS["ink"], bg=COLORS["panel"]).pack(side="left")
        tk.Label(top, text=record.date, font=("Consolas", 8), fg=COLORS["muted"], bg=COLORS["panel"]).pack(side="left", padx=(8, 0))
        ttk.Button(top, text="删除记录", style="Danger.TButton", command=self.delete_record).pack(side="right")

        # 先放素材槽位，识别信息/时间放到最下面。
        slots_title = tk.Frame(self.editor_scroll.body, bg=COLORS["panel"], padx=self.px(12), pady=self.px(6))
        slots_title.pack(fill="x")
        tk.Label(slots_title, text="材料槽位", font=("Microsoft YaHei UI", 9, "bold"), fg=COLORS["ink"], bg=COLORS["panel"]).pack(side="left")
        tk.Label(slots_title, text="文件 / 粘贴 / 截图", font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["panel"]).pack(side="right")
        for key in record.template()["slots"]:
            self._build_slot(record, key)

        info_head = tk.Frame(self.editor_scroll.body, bg=COLORS["panel"], padx=self.px(12), pady=self.px(8))
        info_head.pack(fill="x", pady=(self.px(8), 0))
        tk.Label(info_head, text="识别信息 / 时间", font=("Microsoft YaHei UI", 9, "bold"), fg=COLORS["ink"], bg=COLORS["panel"]).pack(side="left")
        tk.Label(info_head, text="时间自动识别，识别不到再改", font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["panel"]).pack(side="right")

        form = tk.Frame(self.editor_scroll.body, bg=COLORS["panel"], padx=self.px(12), pady=self.px(4))
        form.pack(fill="x")
        self.field_vars = {
            "date": tk.StringVar(value=record.date),
            "amount": tk.StringVar(value=f"{record.amount:.2f}" if record.amount is not None else ""),
            "payment_amount": tk.StringVar(value=f"{record.payment_amount:.2f}" if record.payment_amount is not None else ""),
            "invoice_no": tk.StringVar(value=record.invoice_no),
            "seller": tk.StringVar(value=record.seller),
            "note": tk.StringVar(value=record.note),
        }
        fields = [
            ("业务日期", "date", 0, 0, 1),
            ("发票金额", "amount", 0, 1, 1),
            ("支付金额（多笔净额）", "payment_amount", 1, 0, 1),
            ("发票号码", "invoice_no", 2, 0, 2),
            ("销售方", "seller", 3, 0, 2),
            ("备注", "note", 4, 0, 2),
        ]
        for label, key, row, col, span in fields:
            tk.Label(form, text=label, font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["panel"]).grid(
                row=row * 2, column=col, columnspan=span, sticky="w", padx=(0, 8), pady=(4, 0)
            )
            entry = ttk.Entry(form, textvariable=self.field_vars[key])
            entry.grid(row=row * 2 + 1, column=col, columnspan=span, sticky="ew", padx=(0, 10), pady=(2, 4))
            entry.bind("<FocusOut>", lambda _event, r=record: self.save_fields(r))
            entry.bind("<Return>", lambda _event, r=record: self.save_fields(r))
            if key == "date":
                entry.bind("<KeyRelease>", lambda _event, r=record: setattr(r, "date_manual", True))
            if key == "payment_amount":
                entry.bind("<KeyRelease>", lambda _event, r=record: setattr(r, "payment_manual", True))
        form.columnconfigure(0, weight=1)
        form.columnconfigure(1, weight=1)

    def _is_required(self, record: Record, key: str) -> bool:
        required = list(record.template().get("required", []))
        if record.type == "purchase" and (record.amount or 0) > 500 and key == "photo":
            return True
        return key in required

    def _build_slot(self, record: Record, key: str) -> None:
        assets = record.slot_assets(key)
        required = self._is_required(record, key)
        card = tk.Frame(
            self.editor_scroll.body,
            bg="#F8FCFE",
            highlightbackground=COLORS["line"],
            highlightthickness=1,
            bd=0,
        )
        card.pack(fill="x", padx=self.px(12), pady=self.px(4))
        head = tk.Frame(card, bg="#F8FCFE", padx=self.px(9), pady=self.px(6))
        head.pack(fill="x")
        tk.Label(head, text=SLOT_ICONS.get(key, "📎"), font=("Segoe UI Emoji", 11), bg="#F8FCFE").pack(side="left")
        tk.Label(head, text=SLOT_LABELS.get(key, key), font=("Microsoft YaHei UI", 9, "bold"), fg=COLORS["ink"], bg="#F8FCFE").pack(side="left", padx=(5, 0))
        if required:
            tk.Label(head, text="必填", font=("Microsoft YaHei UI", 7, "bold"), fg=COLORS["error"], bg="#FCEBEA", padx=self.px(5)).pack(side="left", padx=(6, 0))
        self.ocr_labels[(record.id, key)] = tk.Label(
            head,
            text="",
            font=("Microsoft YaHei UI", 7),
            fg=COLORS["muted"],
            bg="#F8FCFE",
        )
        self.ocr_labels[(record.id, key)].pack(side="right")

        actions = tk.Frame(card, bg="#F8FCFE", padx=self.px(8), pady=self.px(2))
        actions.pack(fill="x")
        ttk.Button(actions, text="选择文件", style="Soft.TButton", command=lambda: self.add_asset(record, key, "file")).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text="粘贴", style="Soft.TButton", command=lambda: self.add_asset(record, key, "paste")).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text="截图", style="Soft.TButton", command=lambda: self.add_asset(record, key, "capture")).pack(side="left", padx=(0, 4))
        if key in {"invoice", "payment", "itinerary"}:
            ttk.Button(actions, text="识别", style="Soft.TButton", command=lambda: self._reocr_slot(record, key)).pack(side="left", padx=(0, 4))

        if not assets:
            tk.Label(
                card,
                text="未添加",
                font=("Microsoft YaHei UI", 8),
                fg="#9AAAB4",
                bg="#F8FCFE",
                anchor="w",
            ).pack(fill="x", padx=self.px(10), pady=(0, self.px(8)))
            return
        for asset in assets:
            row = tk.Frame(card, bg="#F8FCFE", padx=self.px(9), pady=self.px(5))
            row.pack(fill="x")
            row.columnconfigure(1, weight=1)
            thumb = self._make_thumb(asset, self.project)
            if thumb is not None:
                label = tk.Label(row, image=thumb, bg="#F8FCFE")
                label.image = thumb
                label.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, self.px(8)))
            else:
                tk.Label(row, text="📄", font=("Segoe UI Emoji", 20), bg="#F8FCFE").grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, self.px(8)))
            info = tk.Frame(row, bg="#F8FCFE")
            info.grid(row=0, column=1, sticky="ew")
            tk.Label(info, text=asset.display_name, font=("Microsoft YaHei UI", 8), fg=COLORS["ink"], bg="#F8FCFE", anchor="w", wraplength=self.px(250)).pack(anchor="w")
            meta = f"{asset.source} · {asset.added_at[:16]}"
            if asset.amount:
                if asset.refund_amount:
                    net = round(asset.amount - asset.refund_amount, 2)
                    meta = f"支付 ¥{asset.amount:,.2f} · 退款 ¥{asset.refund_amount:,.2f} · 净 ¥{net:,.2f} · " + meta
                else:
                    meta = f"识别到 ¥{asset.amount:,.2f} · " + meta
            tk.Label(info, text=meta, font=("Microsoft YaHei UI", 7), fg=COLORS["muted"], bg="#F8FCFE").pack(anchor="w")
            actions = tk.Frame(row, bg="#F8FCFE")
            actions.grid(row=0, column=2, rowspan=2, sticky="e", padx=(self.px(8), 0))
            ttk.Button(actions, text="查看", style="Soft.TButton", command=lambda a=asset: self.open_asset(a)).pack(side="left", padx=(0, 4))
            ttk.Button(actions, text="删除", style="Danger.TButton", command=lambda a=asset, k=key: self.delete_asset(record, k, a)).pack(side="left")

    def save_fields(self, record: Record) -> None:
        if not self.project or self.project.find_record(record.id) is None:
            return
        new_date = self.field_vars["date"].get().strip()
        if new_date:
            try:
                date.fromisoformat(new_date)
            except ValueError:
                messagebox.showwarning("日期格式", "业务日期格式应为 2026-10-19。", parent=self)
                return
        else:
            new_date = record.date
        if new_date != record.date:
            record.date_manual = True
            record.date_ocr = False
        record.date = new_date
        record.amount = self._parse_amount(self.field_vars["amount"].get())
        new_payment = self._parse_amount(self.field_vars["payment_amount"].get())
        if new_payment != record.payment_amount:
            record.payment_manual = True
        record.payment_amount = new_payment
        record.invoice_no = self.field_vars["invoice_no"].get().strip()
        record.seller = self.field_vars["seller"].get().strip()
        record.note = self.field_vars["note"].get().strip()
        record.updated_at = datetime.now().isoformat(timespec="seconds")
        self.project.save()
        self._refresh_stats_only()
        self._render_records()

    @staticmethod
    def _parse_amount(value: str) -> float | None:
        value = value.strip().replace(",", "").replace("¥", "").replace("元", "")
        if not value:
            return None
        try:
            return round(float(value), 2)
        except ValueError:
            return None

    def _refresh_stats_only(self) -> None:
        if self.project is None or not hasattr(self, "stat_value_labels"):
            return
        missing = self.project.missing_count()
        values = {
            "总额": f"¥{self.project.total():,.2f}",
            "费用记录": f"{len(self.project.records)} 条",
            "待补": f"{missing} 项",
            "状态": self.project.status_label,
        }
        colors = {
            "总额": COLORS["accent"],
            "费用记录": COLORS["sky"],
            "待补": COLORS["warn"] if missing else COLORS["leaf"],
            "状态": self.project.status_color,
        }
        for key, value in values.items():
            label = self.stat_value_labels.get(key)
            if label is not None:
                label.configure(text=value, fg=colors[key])

    def delete_record(self) -> None:
        record = self._record()
        if not self.project or not record:
            return
        if not messagebox.askyesno(
            "删除记录",
            f"确定删除“{record.display_title}”这条记录吗？\n\n已添加的素材文件也会删除。",
            parent=self,
            icon="warning",
        ):
            return
        self.project.remove_record(record.id, delete_assets=True)
        self.project.save()
        self.current_record_id = self.project.records[0].id if self.project.records else None
        self._render_records()
        self._render_editor()
        self._refresh_projects()

    def _make_thumb(self, asset: Asset, project: Project | None, size: tuple[int, int] | None = None) -> ImageTk.PhotoImage | None:
        if project is None:
            return None
        path = asset.path(project.folder)
        if not path.exists():
            return None
        size = size or (self.px(100), self.px(72))
        key = f"{path}:{size}:{path.stat().st_mtime_ns}"
        if key in self._thumb_refs:
            return self._thumb_refs[key]
        try:
            image = None
            if path.suffix.lower() == ".pdf":
                try:
                    import pypdfium2 as pdfium

                    pdf = pdfium.PdfDocument(str(path))
                    try:
                        bitmap = pdf[0].render(scale=1.2)
                        image = bitmap.to_pil()
                    finally:
                        pdf.close()
                except Exception:
                    image = None
            if image is None:
                image = Image.open(path)
            image = image.convert("RGB")
            image.thumbnail(size, Image.LANCZOS)
            photo = ImageTk.PhotoImage(image)
            self._thumb_refs[key] = photo
            return photo
        except Exception:
            return None

    @staticmethod
    def _is_multi_slot(record: Record, slot: str) -> bool:
        if slot in {"payment", "toll", "other"}:
            return True
        if slot == "itinerary" and record.type == "taxi":
            return True
        return False

    def add_asset(self, record: Record, slot: str, mode: str) -> None:
        if not self.project:
            return
        single = not self._is_multi_slot(record, slot)
        if single and record.has_slot(slot):
            if not messagebox.askyesno(
                "替换素材",
                f"{SLOT_LABELS.get(slot, slot)}已经有一份素材，是否替换？",
                parent=self,
            ):
                return
        if mode == "capture":
            self._capture_for(record, slot)
            return
        assets: list[Asset] = []
        if mode == "file":
            paths = filedialog.askopenfilenames(
                title=f"选择{SLOT_LABELS.get(slot, slot)}",
                filetypes=[
                    ("支持的文件", "*.pdf *.ofd *.xml *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp"),
                    ("所有文件", "*.*"),
                ],
            )
            for path in paths:
                try:
                    assets.append(self.project.import_asset(Path(path), source_kind="file"))
                except Exception as exc:
                    messagebox.showerror("导入失败", str(exc), parent=self)
            if single and assets:
                assets = assets[:1]
        elif mode == "paste":
            image = clipboard_image()
            if image is not None:
                assets.append(self.project.save_image_asset(image, "paste"))
            else:
                for path in clipboard_files():
                    try:
                        assets.append(self.project.import_asset(Path(path), source_kind="paste"))
                    except Exception:
                        pass
            if not assets:
                messagebox.showinfo("剪贴板", "剪贴板里没有图片或文件。", parent=self)
                return
            if single and assets:
                assets = assets[:1]
        if assets:
            self._append_assets(record, slot, assets, replace=single)

    def _capture_for(self, record: Record, slot: str) -> None:
        project_id = self.project.id if self.project else ""
        record_id = record.id

        def done(image: Image.Image | None) -> None:
            if image is None:
                return
            project = self.workspace.find(project_id) or self.project
            if not project:
                return
            rec = project.find_record(record_id)
            if not rec:
                return
            asset = project.save_image_asset(image, "screenshot")
            self._append_assets(rec, slot, [asset], replace=not self._is_multi_slot(rec, slot))

        ScreenCapturer(self, done).start()

    def _append_assets(self, record: Record, slot: str, assets: list[Asset], replace: bool = False) -> None:
        if not self.project:
            return
        if replace:
            old = record.slot_assets(slot)
            for asset in old:
                try:
                    asset.path(self.project.folder).unlink(missing_ok=True)
                except OSError:
                    pass
            record.slots[slot] = []
        record.slot_assets(slot).extend(assets)
        if slot == "payment":
            record.refresh_payment_total()
        record.updated_at = datetime.now().isoformat(timespec="seconds")
        self.project.save()
        self._render_editor()
        self._render_records()
        self._refresh_projects()
        for asset in assets:
            self._start_ocr(record, slot, asset)

    def delete_asset(self, record: Record, slot: str, asset: Asset) -> None:
        if not self.project:
            return
        if not messagebox.askyesno("删除素材", f"确定删除“{asset.name}”吗？", parent=self):
            return
        try:
            asset.path(self.project.folder).unlink(missing_ok=True)
        except OSError:
            pass
        record.slots[slot] = [a for a in record.slot_assets(slot) if a.id != asset.id]
        if slot == "payment":
            record.refresh_payment_total()
        record.updated_at = datetime.now().isoformat(timespec="seconds")
        self.project.save()
        self._render_editor()
        self._render_records()
        self._refresh_projects()

    def open_asset(self, asset: Asset) -> None:
        if not self.project:
            return
        path = asset.path(self.project.folder)
        try:
            os.startfile(str(path))
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def _reocr_slot(self, record: Record, slot: str) -> None:
        if not self.ocr_enabled.get():
            messagebox.showinfo("OCR 辅助", "请先勾选顶部的“OCR 辅助”。", parent=self)
            return
        assets = record.slot_assets(slot)
        if not assets:
            messagebox.showinfo("重新识别", "这个槽位还没有素材。", parent=self)
            return
        for asset in assets:
            self._start_ocr(record, slot, asset)

    def _start_ocr(self, record: Record, slot: str, asset: Asset) -> None:
        if not self.project or not self.ocr_enabled.get() or slot not in {"invoice", "payment", "itinerary"}:
            return
        key = (record.id, slot, asset.id)
        if key in self._ocr_jobs:
            return
        self._ocr_jobs.add(key)
        label = self.ocr_labels.get((record.id, slot))
        if label is not None:
            label.configure(text="识别中…", fg=COLORS["warn"])
        project_id = self.project.id
        path = asset.path(self.project.folder)

        def worker() -> None:
            data = {}
            error = ""
            extractor = Extractor(use_ocr=True)
            try:
                material = extractor.extract(path)
                classify(material)
                parse_material(material)
                data = {
                    "amount": material.amount,
                    "refund": material.refund_amount,
                    "date": material.business_date.isoformat() if material.business_date else "",
                    "invoice_no": material.invoice_no,
                    "seller": material.seller or material.payee,
                }
                error = material.error
            except Exception as exc:
                error = str(exc)
            finally:
                extractor.cleanup()
            self.ui_queue.put(("ocr", project_id, record.id, slot, asset.id, data, error))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_ocr(self, project_id: str, record_id: str, slot: str, asset_id: str, data: dict, error: str) -> None:
        self._ocr_jobs = {job for job in self._ocr_jobs if job[1] != record_id or job[0] != project_id}
        project = self.workspace.find(project_id) or self.project
        if not project:
            return
        record = project.find_record(record_id)
        if not record:
            return
        changed = False
        if slot == "invoice":
            if record.amount is None and data.get("amount"):
                record.amount = data["amount"]
                changed = True
            if data.get("invoice_no") and not record.invoice_no:
                record.invoice_no = data["invoice_no"]
                changed = True
            if data.get("seller") and not record.seller:
                record.seller = data["seller"]
                changed = True
        elif slot == "payment":
            asset = next((a for a in record.slot_assets("payment") if a.id == asset_id), None)
            if asset is not None:
                if data.get("amount"):
                    asset.amount = data["amount"]
                    changed = True
                if data.get("refund"):
                    asset.refund_amount = data["refund"]
                    changed = True
            before = record.payment_amount
            record.refresh_payment_total()
            if record.payment_amount != before:
                changed = True
        if data.get("date") and record.apply_ocr_date(data["date"]):
            changed = True
        label = self.ocr_labels.get((record.id, slot))
        if label is not None:
            try:
                if changed:
                    label.configure(text="已识别，请核对", fg=COLORS["leaf"])
                elif error:
                    label.configure(text="识别失败", fg=COLORS["muted"])
                else:
                    label.configure(text="未识别到新字段", fg=COLORS["muted"])
            except tk.TclError:
                pass
        if changed:
            project.save()
            if self.project and self.project.id == project.id and self.current_record_id == record.id:
                self._render_editor()
                self._render_records()
                self._refresh_projects()

    def generate_word(self) -> None:
        if not self.project:
            return
        if not self.project.records:
            messagebox.showinfo("生成 Word", "还没有费用记录，先添加一条吧。", parent=self)
            return
        reminders = self.project.missing_reminders()
        errors = [r for r in reminders if r.level == "error"]
        warns = [r for r in reminders if r.level == "warn"]
        if errors or warns:
            lines = [f"发现 {len(errors)} 项必须补充、{len(warns)} 项建议核对：", ""]
            for item in (errors + warns)[:10]:
                lines.append(f"· {item.message}")
            if len(errors) + len(warns) > 10:
                lines.append("…")
            lines.extend(["", "仍然生成 Word 吗？"])
            if not messagebox.askyesno("生成前检查", "\n".join(lines), parent=self, icon="warning"):
                return
        default_path = self.project.output_dir / project_filename(self.project)
        path = filedialog.asksaveasfilename(
            title="保存报销材料 Word",
            initialdir=str(self.project.output_dir),
            initialfile=default_path.name,
            defaultextension=".docx",
            filetypes=[("Word 文档", "*.docx")],
        )
        if not path:
            return
        try:
            generated = build_project_document(self.project, Path(path))
        except Exception as exc:
            messagebox.showerror("生成失败", str(exc), parent=self)
            return
        self._refresh_projects()
        if messagebox.askyesno("生成完成", f"已生成：\n{generated}\n\n是否打开文档？", parent=self):
            try:
                os.startfile(str(generated))
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc), parent=self)


