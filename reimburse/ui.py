from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import load_settings, save_settings
from .appearance import COLORS, apply_window_icon, enable_dpi_awareness
from .docgen import build_document, default_filename
from .models import (
    EVENT_TYPES,
    ROLE_NAMES,
    SECTION_NAMES,
    Material,
    ScanResult,
)
from .pipeline import ScanCancelled, recompute, run_scan
from .scanner import human_size, parse_batch_info

SECTION_BY_LABEL = {v: k for k, v in SECTION_NAMES.items()}
ROLE_BY_LABEL = {v: k for k, v in ROLE_NAMES.items()}
ROLE_LABELS = list(ROLE_NAMES.values())

LEVEL_LABEL = {"error": "必须补充", "warn": "建议核对", "info": "提示"}


def parse_date_entry(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            from datetime import datetime

            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


class EditDialog(tk.Toplevel):
    def __init__(self, parent, material: Material):
        super().__init__(parent)
        self.title("编辑材料")
        self.geometry("560x500")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.material = material
        self.saved = False
        self.configure(bg=COLORS["bg"])

        panel = ttk.Frame(self, padding=18)
        panel.pack(fill="both", expand=True)

        ttk.Label(panel, text=material.name, font=("Microsoft YaHei UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        ttk.Label(panel, text=f"{human_size(material.size_bytes)} · {material.method}", foreground=COLORS["muted"]).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )

        self.section_var = tk.StringVar(value=SECTION_NAMES.get(material.section, "其他票据"))
        self.role_var = tk.StringVar(value=ROLE_NAMES.get(material.role, "其他材料"))
        self.date_var = tk.StringVar(value=material.business_date.isoformat() if material.business_date else "")
        self.amount_var = tk.StringVar(value=f"{material.amount:.2f}" if material.amount is not None else "")
        self.order_var = tk.StringVar(value=f"{material.order_amount:.2f}" if material.order_amount is not None else "")
        self.invoice_var = tk.StringVar(value=material.invoice_no)
        self.seller_var = tk.StringVar(value=material.seller)
        self.payee_var = tk.StringVar(value=material.payee)
        self.note_var = tk.StringVar(value=material.note)
        self.include_var = tk.BooleanVar(value=material.include)

        fields = [
            ("分区", ttk.Combobox(panel, textvariable=self.section_var, values=list(SECTION_BY_LABEL), state="readonly")),
            ("材料角色", ttk.Combobox(panel, textvariable=self.role_var, values=ROLE_LABELS, state="readonly")),
            ("业务日期", ttk.Entry(panel, textvariable=self.date_var)),
            ("金额", ttk.Entry(panel, textvariable=self.amount_var)),
            ("订单金额", ttk.Entry(panel, textvariable=self.order_var)),
            ("发票号码", ttk.Entry(panel, textvariable=self.invoice_var)),
            ("销售方", ttk.Entry(panel, textvariable=self.seller_var)),
            ("收款方", ttk.Entry(panel, textvariable=self.payee_var)),
            ("备注", ttk.Entry(panel, textvariable=self.note_var)),
        ]
        row = 2
        for label, widget in fields:
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 12))
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            row += 1
        ttk.Checkbutton(panel, text="纳入本次报销材料", variable=self.include_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 4)
        )
        row += 1
        ttk.Label(
            panel,
            text="日期格式：2026-10-19；金额留空表示未识别。修改后会自动重新分组。",
            foreground=COLORS["muted"],
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 8))
        panel.columnconfigure(1, weight=1)

        buttons = ttk.Frame(panel)
        buttons.grid(row=row + 1, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="保存", style="Accent.TButton", command=self._save).pack(side="right")

        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self._save())

    def _save(self) -> None:
        m = self.material
        section = SECTION_BY_LABEL.get(self.section_var.get(), m.section)
        role = ROLE_BY_LABEL.get(self.role_var.get(), m.role)
        m.section = section
        m.role = role
        if role == "notice":
            m.section = "notice"
        if role == "payment":
            m.section = "payment"
        if role == "photo":
            m.section = "other"
        date_value = parse_date_entry(self.date_var.get())
        if self.date_var.get().strip() and date_value is None:
            messagebox.showwarning("日期格式", "日期格式应为 2026-10-19。", parent=self)
            return
        m.date = date_value
        m.amount = self._parse_float(self.amount_var.get())
        m.order_amount = self._parse_float(self.order_var.get())
        if self.amount_var.get().strip() and m.amount is None:
            messagebox.showwarning("金额格式", "金额应为数字，例如 1280.50。", parent=self)
            return
        m.invoice_no = self.invoice_var.get().strip()
        m.seller = self.seller_var.get().strip()
        m.payee = self.payee_var.get().strip()
        m.note = self.note_var.get().strip()
        m.include = self.include_var.get()
        m.confidence = "人工"
        m.manual_fields.update({"section", "role", "date", "amount", "note"})
        self.saved = True
        self.destroy()

    @staticmethod
    def _parse_float(value: str) -> float | None:
        value = value.strip().replace(",", "").replace("¥", "").replace("元", "")
        if not value:
            return None
        try:
            return round(float(value), 2)
        except ValueError:
            return None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        apply_window_icon(self)
        self.settings = load_settings()
        self.title("报销材料整理助手")
        self.ui_scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        default_w = min(int(1280 * self.ui_scale), screen_w - 80)
        default_h = min(int(840 * self.ui_scale), screen_h - 120)
        self.geometry(self.settings.get("window_geometry") or f"{default_w}x{default_h}")
        self.minsize(min(int(1080 * self.ui_scale), screen_w - 80), min(int(680 * self.ui_scale), screen_h - 120))
        self.configure(bg=COLORS["bg"])

        self.result: ScanResult | None = None
        self.scan_queue: queue.Queue = queue.Queue()
        self.scan_token = 0
        self.scanning = False
        self.generating = False
        self.material_rows: dict[str, Material] = {}
        self.reminder_map: dict[str, tuple[str, str]] = {}
        self.current_output: Path | None = None

        self._configure_style()
        self._build_ui()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        last_folder = self.settings.get("last_folder", "")
        if last_folder and Path(last_folder).exists():
            self.folder_var.set(last_folder)
            self.after(300, self.start_scan)

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
        style.configure("Card.TFrame", background=COLORS["panel"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["ink"])
        style.configure("Card.TLabel", background=COLORS["panel"], foreground=COLORS["ink"])
        style.configure("Muted.TLabel", foreground=COLORS["muted"])
        style.configure("CardMuted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"])
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"), foreground=COLORS["ink"])
        style.configure("Subtitle.TLabel", font=("Microsoft YaHei UI", 9), foreground=COLORS["muted"])
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"), foreground=COLORS["accent"])
        style.configure("Total.TLabel", font=("Microsoft YaHei UI", 15, "bold"), foreground=COLORS["accent"])
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
        style.map(
            "Soft.TButton",
            background=[("active", "#D7ECEF"), ("pressed", "#CDE5E9")],
            foreground=[("active", COLORS["ink"])],
        )
        style.configure(
            "Ghost.TButton",
            font=("Microsoft YaHei UI", 9),
            foreground=COLORS["muted"],
            background=COLORS["bg"],
            borderwidth=0,
            padding=(self.px(8), self.px(5)),
        )
        style.map("Ghost.TButton", background=[("active", COLORS["soft"])], foreground=[("active", COLORS["ink"])])
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
        style.configure(
            "TCheckbutton",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        style.map("TCheckbutton", background=[("active", COLORS["panel"])], foreground=[("active", COLORS["ink"])])
        style.configure("Treeview", rowheight=self.px(30), fieldbackground=COLORS["panel"], background=COLORS["panel"], borderwidth=0)
        style.configure(
            "Treeview.Heading",
            font=("Microsoft YaHei UI", 9, "bold"),
            background="#E6F4F8",
            foreground=COLORS["accent"],
            relief="flat",
            padding=(self.px(6), self.px(7)),
        )
        style.map("Treeview", background=[("selected", "#D5EDF0")], foreground=[("selected", COLORS["ink"])])
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(self.px(14), self.px(6)))
        style.configure("Horizontal.TProgressbar", troughcolor="#DDEFF2", background=COLORS["accent"], borderwidth=0)
        style.configure("TPanedwindow", background=COLORS["bg"])
        style.configure("Sash", sashthickness=self.px(8), background=COLORS["bg"])

    def _card(self, parent, pad: int = 0):
        outer = tk.Frame(
            parent,
            bg=COLORS["panel"],
            highlightbackground=COLORS["line"],
            highlightthickness=1,
            bd=0,
        )
        inner = tk.Frame(outer, bg=COLORS["panel"], bd=0)
        inner.pack(fill="both", expand=True, padx=self.px(pad), pady=self.px(pad))
        return outer, inner

    def _pill(self, parent, text: str, fg: str | None = None, bg: str | None = None):
        return tk.Label(
            parent,
            text=text,
            bg=bg or COLORS["chip"],
            fg=fg or COLORS["accent"],
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=self.px(8),
            pady=self.px(2),
        )

    def _tick_clock(self) -> None:
        from datetime import datetime

        now = datetime.now()
        if hasattr(self, "clock_var"):
            self.clock_var.set(now.strftime("%H:%M:%S"))
            self.date_var.set(now.strftime("%Y-%m-%d"))
        self.after(1000, self._tick_clock)

    def _build_ui(self) -> None:
        from datetime import datetime

        background = tk.Frame(self, bg=COLORS["bg"])
        background.pack(fill="both", expand=True)
        outer = tk.Frame(background, bg=COLORS["bg"], padx=self.px(18), pady=self.px(12))
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=COLORS["bg"])
        header.pack(fill="x")
        brand = tk.Frame(header, bg=COLORS["bg"])
        brand.pack(side="left")
        tk.Label(brand, text="🧾", font=("Segoe UI Emoji", 26), bg=COLORS["bg"]).pack(side="left")
        title_box = tk.Frame(brand, bg=COLORS["bg"])
        title_box.pack(side="left", padx=(10, 0))
        title_line = tk.Frame(title_box, bg=COLORS["bg"])
        title_line.pack(anchor="w")
        tk.Label(
            title_line,
            text="报销材料整理助手",
            font=("Microsoft YaHei UI", 18, "bold"),
            fg=COLORS["ink"],
            bg=COLORS["bg"],
        ).pack(side="left")
        self._pill(title_line, "v1.0").pack(side="left", padx=(8, 0), pady=(3, 0))
        tk.Label(
            title_box,
            text="离线整理 · 一次出差 / 参会 / 采购生成一份 Word",
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["muted"],
            bg=COLORS["bg"],
        ).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(header, bg=COLORS["bg"])
        right.pack(side="right")
        self.clock_var = tk.StringVar(value=datetime.now().strftime("%H:%M:%S"))
        self.date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        tk.Label(right, textvariable=self.date_var, font=("Consolas", 10), fg=COLORS["muted"], bg=COLORS["bg"]).pack(side="left", padx=(0, 10))
        tk.Label(right, textvariable=self.clock_var, font=("Consolas", 10, "bold"), fg=COLORS["accent"], bg=COLORS["bg"]).pack(side="left", padx=(0, 14))
        dot = tk.Label(right, text="●", font=("Segoe UI", 9), fg=COLORS["leaf"], bg=COLORS["bg"])
        dot.pack(side="left")
        self.ocr_state = tk.Label(right, text="本地 OCR", font=("Microsoft YaHei UI", 9), fg=COLORS["muted"], bg=COLORS["bg"])
        self.ocr_state.pack(side="left", padx=(4, 0))

        accent = tk.Frame(outer, bg=COLORS["accent"], height=max(2, self.px(2)))
        accent.pack(fill="x", pady=(self.px(10), self.px(12)))

        folder_outer, folder_card = self._card(outer, pad=14)
        folder_outer.pack(fill="x", pady=(0, 10))
        folder_head = tk.Frame(folder_card, bg=COLORS["panel"])
        folder_head.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        tk.Label(folder_head, text="📁 报销项目", font=("Microsoft YaHei UI", 10, "bold"), fg=COLORS["accent"], bg=COLORS["panel"]).pack(side="left")
        tk.Label(
            folder_head,
            text="一个文件夹 = 一次出差 / 参会 / 采购",
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
        ).pack(side="left", padx=(12, 0))
        self.range_label = tk.Label(folder_head, text="", font=("Consolas", 9), fg=COLORS["muted"], bg=COLORS["panel"])
        self.range_label.pack(side="right")

        tk.Label(folder_card, text="材料文件夹", font=("Microsoft YaHei UI", 9), fg=COLORS["muted"], bg=COLORS["panel"]).grid(row=1, column=0, sticky="w")
        self.folder_var = tk.StringVar(value=self.settings.get("last_folder", ""))
        self.folder_entry = ttk.Entry(folder_card, textvariable=self.folder_var, font=("Microsoft YaHei UI", 10))
        self.folder_entry.grid(row=1, column=1, sticky="ew", padx=(10, 8), ipady=2)
        ttk.Button(folder_card, text="📂 选择", style="Soft.TButton", command=self.browse_folder).grid(row=1, column=2, padx=(0, 6))
        self.scan_button = ttk.Button(folder_card, text="🔍 扫描材料", style="Accent.TButton", command=self.start_scan)
        self.scan_button.grid(row=1, column=3)
        folder_card.columnconfigure(1, weight=1)

        meta = tk.Frame(folder_card, bg=COLORS["panel"])
        meta.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        tk.Label(meta, text="事项类型", font=("Microsoft YaHei UI", 9), fg=COLORS["muted"], bg=COLORS["panel"]).pack(side="left")
        self.event_var = tk.StringVar(value=self.settings.get("event_type", "出差"))
        self.event_combo = ttk.Combobox(meta, textvariable=self.event_var, values=EVENT_TYPES, state="readonly", width=7)
        self.event_combo.pack(side="left", padx=(6, 16))
        self.event_combo.bind("<<ComboboxSelected>>", self._on_event_type_changed)
        tk.Label(meta, text="报销人", font=("Microsoft YaHei UI", 9), fg=COLORS["muted"], bg=COLORS["panel"]).pack(side="left")
        self.applicant_var = tk.StringVar(value=self.settings.get("applicant", ""))
        ttk.Entry(meta, textvariable=self.applicant_var, width=10).pack(side="left", padx=(6, 16))
        tk.Label(meta, text="部门", font=("Microsoft YaHei UI", 9), fg=COLORS["muted"], bg=COLORS["panel"]).pack(side="left")
        self.department_var = tk.StringVar(value=self.settings.get("department", ""))
        ttk.Entry(meta, textvariable=self.department_var, width=12).pack(side="left", padx=(6, 16))
        self.ocr_var = tk.BooleanVar(value=bool(self.settings.get("use_ocr", True)))
        ttk.Checkbutton(meta, text="图片 OCR", variable=self.ocr_var).pack(side="left", padx=(0, 10))
        self.recursive_var = tk.BooleanVar(value=bool(self.settings.get("include_subfolders", True)))
        ttk.Checkbutton(meta, text="包含子文件夹", variable=self.recursive_var).pack(side="left")

        stats = tk.Frame(outer, bg=COLORS["bg"])
        stats.pack(fill="x", pady=(0, 10))
        self.stat_total_var = tk.StringVar(value="¥0.00")
        self.stat_materials_var = tk.StringVar(value="0 份")
        self.stat_groups_var = tk.StringVar(value="0 组")
        self.stat_missing_var = tk.StringVar(value="0 项")
        stat_defs = [
            ("💰", "本次报销总额", self.stat_total_var, COLORS["accent"]),
            ("📎", "材料", self.stat_materials_var, COLORS["sky"]),
            ("🗂", "票据组", self.stat_groups_var, COLORS["leaf"]),
            ("⚠", "待补提醒", self.stat_missing_var, COLORS["clay"]),
        ]
        for index, (icon, label, var, color) in enumerate(stat_defs):
            card = tk.Frame(stats, bg=COLORS["panel"], highlightbackground=COLORS["line"], highlightthickness=1, bd=0)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 8, 0))
            tk.Frame(card, bg=color, width=self.px(3)).pack(side="left", fill="y")
            body = tk.Frame(card, bg=COLORS["panel"], padx=self.px(12), pady=self.px(7))
            body.pack(side="left", fill="both", expand=True)
            head = tk.Frame(body, bg=COLORS["panel"])
            head.pack(anchor="w")
            tk.Label(head, text=icon, font=("Segoe UI Emoji", 10), bg=COLORS["panel"]).pack(side="left")
            tk.Label(head, text=label, font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["panel"]).pack(side="left", padx=(5, 0))
            value = tk.Label(body, textvariable=var, font=("Microsoft YaHei UI", 13, "bold"), fg=color, bg=COLORS["panel"])
            value.pack(anchor="w", pady=(1, 0))
            if label == "待补提醒":
                self.stat_missing_label = value
            stats.columnconfigure(index, weight=1)

        toolbar = tk.Frame(outer, bg=COLORS["bg"])
        toolbar.pack(fill="x", pady=(0, 10))
        self.rescan_button = ttk.Button(toolbar, text="↻ 重新扫描", style="Soft.TButton", command=self.start_scan)
        self.rescan_button.pack(side="left")
        ttk.Button(toolbar, text="全选", style="Soft.TButton", command=lambda: self.set_all_included(True)).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="全不选", style="Soft.TButton", command=lambda: self.set_all_included(False)).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="编辑", style="Soft.TButton", command=self.edit_selected).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="打开文件", style="Soft.TButton", command=self.open_selected_file).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="打开文件夹", style="Soft.TButton", command=self.open_folder).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="导出提醒", style="Soft.TButton", command=self.export_reminders).pack(side="left", padx=(6, 0))
        self.total_var = tk.StringVar(value="¥0.00")
        self.generate_button = ttk.Button(toolbar, text="生成 Word  →", style="Accent.TButton", command=self.generate)
        self.generate_button.pack(side="right")

        main = ttk.PanedWindow(outer, orient=tk.VERTICAL)
        main.pack(fill="both", expand=True)
        top = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
        main.add(top, weight=4)

        tree_outer, tree_card = self._card(top, pad=0)
        top.add(tree_outer, weight=3)
        tree_header = tk.Frame(tree_card, bg=COLORS["panel"], padx=12, pady=9)
        tree_header.pack(fill="x")
        tk.Label(tree_header, text="📋 材料与分组", font=("Microsoft YaHei UI", 10, "bold"), fg=COLORS["ink"], bg=COLORS["panel"]).pack(side="left")
        tk.Label(
            tree_header,
            text="单击“含”勾选  ·  双击编辑  ·  按空格切换",
            font=("Microsoft YaHei UI", 8),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
        ).pack(side="left", padx=(10, 0))
        tree_frame = tk.Frame(tree_card, bg=COLORS["panel"])
        tree_frame.pack(fill="both", expand=True)
        columns = ("include", "section", "group", "date", "amount", "name", "confidence", "status")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="browse")
        headings = {
            "include": "含",
            "section": "分区",
            "group": "票据组",
            "date": "日期",
            "amount": "金额",
            "name": "文件",
            "confidence": "识别",
            "status": "状态",
        }
        widths = {
            "include": 34,
            "section": 82,
            "group": 160,
            "date": 82,
            "amount": 82,
            "name": 220,
            "confidence": 48,
            "status": 170,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            width = self.px(widths[column])
            self.tree.column(column, width=width, minwidth=width, anchor="w" if column in {"group", "name", "status"} else "center")
        self.tree.column("#0", width=self.px(26), stretch=False)
        self.tree.tag_configure("group", background=COLORS["group"], foreground=COLORS["accent"], font=("Microsoft YaHei UI", 9, "bold"))
        self.tree.tag_configure("duplicate", foreground=COLORS["error"])
        self.tree.tag_configure("low", foreground=COLORS["warn"])
        self.tree.tag_configure("notice", foreground="#6B7C87")
        self.tree.tag_configure("payment", foreground=COLORS["accent_hover"])
        self.tree.tag_configure("excluded", foreground="#A3B2BC")
        y_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        reminder_outer, reminder_card = self._card(top, pad=12)
        top.add(reminder_outer, weight=2)
        reminder_header = tk.Frame(reminder_card, bg=COLORS["panel"])
        reminder_header.pack(fill="x", pady=(0, 8))
        tk.Label(reminder_header, text="🧩 待补提醒", font=("Microsoft YaHei UI", 10, "bold"), fg=COLORS["ink"], bg=COLORS["panel"]).pack(side="left")
        self.reminder_title = tk.StringVar(value="")
        self.reminder_badge = tk.Label(
            reminder_header,
            textvariable=self.reminder_title,
            font=("Microsoft YaHei UI", 8, "bold"),
            fg=COLORS["accent"],
            bg=COLORS["chip"],
            padx=7,
            pady=2,
        )
        self.reminder_badge.pack(side="left", padx=(8, 0))
        tk.Label(reminder_header, text="双击定位", font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["panel"]).pack(side="right")
        self.reminder_body = tk.Frame(reminder_card, bg=COLORS["panel"])
        self.reminder_tree = ttk.Treeview(self.reminder_body, columns=("level", "message"), show="headings", selectmode="browse")
        self.reminder_tree.heading("level", text="级别")
        self.reminder_tree.heading("message", text="提醒内容")
        self.reminder_tree.column("level", width=self.px(62), anchor="center", stretch=False)
        self.reminder_tree.column("message", width=self.px(420), anchor="w")
        self.reminder_tree.tag_configure("error", foreground=COLORS["error"])
        self.reminder_tree.tag_configure("warn", foreground=COLORS["warn"])
        self.reminder_tree.tag_configure("info", foreground=COLORS["info"])
        reminder_y = ttk.Scrollbar(self.reminder_body, orient="vertical", command=self.reminder_tree.yview)
        reminder_x = ttk.Scrollbar(self.reminder_body, orient="horizontal", command=self.reminder_tree.xview)
        self.reminder_tree.configure(yscrollcommand=reminder_y.set, xscrollcommand=reminder_x.set)
        self.reminder_tree.grid(row=0, column=0, sticky="nsew")
        reminder_y.grid(row=0, column=1, sticky="ns")
        reminder_x.grid(row=1, column=0, sticky="ew")
        self.reminder_body.rowconfigure(0, weight=1)
        self.reminder_body.columnconfigure(0, weight=1)
        self.reminder_tree.bind("<Double-1>", self._on_reminder_double_click)
        self.reminder_tree.bind("<<TreeviewSelect>>", self._on_reminder_select)
        self.reminder_empty = tk.Label(
            reminder_card,
            text="✓  未发现明显缺失材料\n\n扫描后会在这里列出缺发票、缺支付、缺行程单、缺照片等提醒。",
            font=("Microsoft YaHei UI", 9),
            fg=COLORS["muted"],
            bg=COLORS["panel"],
            justify="center",
        )
        self.reminder_empty.pack(fill="both", expand=True, pady=(24, 0))

        bottom_outer, bottom = self._card(main, pad=10)
        main.add(bottom_outer, weight=1)
        detail_header = tk.Frame(bottom, bg=COLORS["panel"])
        detail_header.pack(fill="x")
        tk.Label(detail_header, text="🔎 识别文本 / 材料信息", font=("Microsoft YaHei UI", 9, "bold"), fg=COLORS["ink"], bg=COLORS["panel"]).pack(side="left", padx=(2, 0), pady=(0, 4))
        self.detail_info = tk.Label(detail_header, text="", font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["panel"])
        self.detail_info.pack(side="right", padx=(0, 2))
        self.detail_text = tk.Text(
            bottom,
            height=7,
            wrap="word",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#DCEBF1",
            background="#F8FCFE",
            foreground=COLORS["ink"],
            font=("Microsoft YaHei UI", 9),
            padx=self.px(10),
            pady=self.px(8),
        )
        self.detail_text.pack(fill="both", expand=True, pady=(0, 0))
        self.detail_text.insert("1.0", "选择左侧材料，这里会显示识别文本和字段；双击可修改。")
        self.detail_text.configure(state="disabled")

        status = tk.Frame(outer, bg=COLORS["bg"])
        status.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(status, mode="determinate", length=self.px(200))
        self.progress.pack(side="left")
        self.status_var = tk.StringVar(value="选择材料文件夹后开始扫描。")
        tk.Label(status, textvariable=self.status_var, font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["bg"]).pack(side="left", padx=(10, 0))
        self.count_var = tk.StringVar(value="")
        tk.Label(status, textvariable=self.count_var, font=("Microsoft YaHei UI", 8), fg=COLORS["muted"], bg=COLORS["bg"]).pack(side="right")
        self._tick_clock()

    def _bind_shortcuts(self) -> None:
        self.bind("<Control-o>", lambda _event: self.browse_folder())
        self.bind("<F5>", lambda _event: self.start_scan())
        self.bind("<Control-g>", lambda _event: self.generate())
        self.tree.bind("<space>", lambda _event: self.toggle_selected())
        self.tree.bind("<Delete>", lambda _event: None)

    def browse_folder(self) -> None:
        initial = self.folder_var.get() or str(Path.home())
        folder = filedialog.askdirectory(title="选择报销材料文件夹", initialdir=initial)
        if folder:
            self.folder_var.set(folder)
            self.start_scan()

    def start_scan(self) -> None:
        folder_text = self.folder_var.get().strip()
        if not folder_text:
            messagebox.showinfo("选择文件夹", "请先选择材料文件夹。", parent=self)
            return
        folder = Path(folder_text)
        if not folder.exists() or not folder.is_dir():
            messagebox.showerror("文件夹不存在", f"找不到文件夹：\n{folder}", parent=self)
            return
        if self.scanning:
            self.scan_token += 1
            self.status_var.set("正在停止上一次扫描…")
            return
        self.scan_token += 1
        token = self.scan_token
        self.scanning = True
        self.scan_button.configure(state="disabled")
        self.rescan_button.configure(state="disabled")
        self.generate_button.configure(state="disabled")
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.status_var.set("正在扫描材料…")
        self.tree.delete(*self.tree.get_children())
        self.reminder_tree.delete(*self.reminder_tree.get_children())
        self.material_rows.clear()
        self.reminder_map.clear()

        event_type = self.event_var.get()
        use_ocr = self.ocr_var.get()
        recursive = self.recursive_var.get()

        def progress(index: int, total: int, name: str, stage: str) -> None:
            self.scan_queue.put(("progress", index, total, name, stage))

        def worker() -> None:
            try:
                result = run_scan(
                    folder,
                    event_type=event_type,
                    use_ocr=use_ocr,
                    recursive=recursive,
                    progress=progress,
                    cancel_check=lambda: token != self.scan_token,
                )
                self.scan_queue.put(("done", result, token))
            except ScanCancelled:
                self.scan_queue.put(("cancelled", token))
            except Exception as exc:
                self.scan_queue.put(("error", str(exc), token))

        threading.Thread(target=worker, daemon=True).start()
        self.after(80, self._poll_scan)

    def _poll_scan(self) -> None:
        try:
            while True:
                event = self.scan_queue.get_nowait()
                kind = event[0]
                if kind == "progress":
                    _, index, total, name, stage = event
                    if total:
                        self.progress.configure(maximum=total, value=index)
                    self.status_var.set(f"{stage}：{name}" if name else stage)
                elif kind == "done":
                    _, result, token = event
                    if token == self.scan_token:
                        self._finish_scan(result)
                elif kind == "cancelled":
                    if event[1] == self.scan_token:
                        self._end_scan()
                        self.status_var.set("扫描已取消。")
                elif kind == "error":
                    if event[2] == self.scan_token:
                        self._end_scan()
                        self.status_var.set("扫描失败。")
                        messagebox.showerror("扫描失败", event[1], parent=self)
        except queue.Empty:
            pass
        if self.scanning:
            self.after(100, self._poll_scan)

    def _finish_scan(self, result: ScanResult) -> None:
        self.result = result
        self._end_scan()
        self.range_label.configure(text=self._range_text(result))
        self._populate_tree()
        self._populate_reminders()
        self._update_summary()
        if result.warnings:
            self.ocr_state.configure(text="；".join(result.warnings), foreground=COLORS["warn"])
        elif self.ocr_var.get():
            self.ocr_state.configure(text="OCR 就绪（本地）", foreground=COLORS["muted"])
        else:
            self.ocr_state.configure(text="OCR 已关闭", foreground=COLORS["muted"])
        error_count = sum(1 for r in result.reminders if r.level == "error")
        warn_count = sum(1 for r in result.reminders if r.level == "warn")
        self.status_var.set(
            f"扫描完成：{len(result.materials)} 份材料，{len(result.groups)} 个票据组，用时 {result.duration_seconds:.1f} 秒"
            + (f"；{error_count} 项必须补充，{warn_count} 项建议核对" if error_count or warn_count else "")
        )
        if result.total == 0 and result.materials:
            self.status_var.set("扫描完成，但未计算出金额，请检查识别结果。")

    def _end_scan(self) -> None:
        self.scanning = False
        self.scan_button.configure(state="normal")
        self.rescan_button.configure(state="normal")
        self.generate_button.configure(state="normal")
        self.progress.configure(value=0)

    def _range_text(self, result: ScanResult) -> str:
        if result.date_start and result.date_end:
            return f"{result.date_start.isoformat()} 至 {result.date_end.isoformat()}"
        if result.date_start:
            return result.date_start.isoformat()
        return ""

    def _populate_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.material_rows.clear()
        if not self.result:
            return
        result = self.result
        groups_by_section = {}
        for group in result.groups:
            groups_by_section.setdefault(group.section, []).append(group)
        for section in ("traffic", "hotel", "taxi", "other"):
            for group in groups_by_section.get(section, []):
                self._insert_group(group)
        # Payments are displayed as their own parent, in invoice order.
        if result.ordered_payments:
            unique_payments = [p for p in result.ordered_payments if not p.duplicate]
            parent = self.tree.insert(
                "",
                "end",
                iid="PAYMENTS",
                text="",
                values=(
                    "☑" if any(p.include for p in result.ordered_payments) else "☐",
                    "支付记录",
                    f"{len(unique_payments)} 笔" + (f" / {len(result.ordered_payments) - len(unique_payments)} 重复" if len(unique_payments) != len(result.ordered_payments) else ""),
                    "",
                    f"¥{sum(p.net_amount for p in unique_payments if p.include):,.2f}",
                    "",
                    "",
                    "",
                ),
                tags=("group",),
                open=True,
            )
            for payment in result.ordered_payments:
                self._insert_material(payment, parent)
        for group in groups_by_section.get("notice", []):
            self._insert_group(group)
        # Anything that still has no group is shown last so it is never lost.
        grouped = {m.uid for g in result.groups for m in g.materials}
        grouped.update(p.uid for p in result.ordered_payments)
        leftovers = [m for m in result.materials if m.uid not in grouped]
        if leftovers:
            parent = self.tree.insert("", "end", iid="UNGROUPED", text="", values=("", "未分组", "", "", "", "", "", "请核对"), tags=("group",), open=True)
            for material in leftovers:
                self._insert_material(material, parent)

    def _insert_group(self, group) -> None:
        if not group.materials:
            return
        any_included = any(m.include for m in group.materials)
        group_status = "；".join(group.flags[:2])
        if not group_status:
            if not group.has_payment and group.has_invoice:
                group_status = "缺支付记录"
            elif group.has_payment and not group.has_invoice:
                group_status = "缺发票"
        detail = ""
        if group.has_invoice and group.has_payment:
            detail = f"票{group.invoice_total:,.2f} / 付{group.payment_total:,.2f}"
        elif group.has_invoice:
            detail = f"票{group.invoice_total:,.2f}"
        if detail and group_status:
            detail += " · " + group_status
        values = (
            "☑" if any_included else "☐",
            group.section_name,
            group.short_title(34),
            group.sort_date.isoformat() if group.sort_date and group.sort_date != date.max else "",
            f"¥{group.effective_amount:,.2f}",
            "",
            "",
            detail,
        )
        self.tree.insert("", "end", iid=group.gid, text="", values=values, tags=("group",), open=True)
        for material in group.materials:
            if material.role == "payment":
                continue
            self._insert_material(material, group.gid)

    def _insert_material(self, material: Material, parent: str) -> None:
        tags = []
        status = ""
        if material.duplicate:
            tags.append("duplicate")
            status = "重复"
        if material.role == "payment":
            tags.append("payment")
            if not material.group_id:
                status = "未匹配"
        if material.role == "notice":
            tags.append("notice")
        if material.confidence == "低" and not material.duplicate:
            tags.append("low")
            status = status or "低置信度"
        if not material.include:
            tags.append("excluded")
        if material.error:
            status = material.error[:30]
        if not status and material.amount is None and material.role in {"invoice", "payment", "itinerary"}:
            status = "缺金额"
        values = (
            "☑" if material.include else "☐",
            material.section_name,
            material.subtype_name,
            material.display_date(),
            f"¥{material.amount:,.2f}" if material.amount is not None else "",
            material.name,
            material.confidence,
            status,
        )
        self.tree.insert(parent, "end", iid=material.uid, text="", values=values, tags=tuple(tags))
        self.material_rows[material.uid] = material

    def _show_reminder_view(self, has_items: bool) -> None:
        if has_items:
            self.reminder_empty.pack_forget()
            if not self.reminder_body.winfo_ismapped():
                self.reminder_body.pack(fill="both", expand=True)
        else:
            self.reminder_body.pack_forget()
            if not self.reminder_empty.winfo_ismapped():
                self.reminder_empty.pack(fill="both", expand=True, pady=(24, 0))

    def _populate_reminders(self) -> None:
        self.reminder_tree.delete(*self.reminder_tree.get_children())
        self.reminder_map.clear()
        if not self.result:
            self.reminder_title.set("")
            self._show_reminder_view(False)
            return
        reminders = self.result.reminders
        counts = {"error": 0, "warn": 0, "info": 0}
        for index, reminder in enumerate(reminders):
            counts[reminder.level] = counts.get(reminder.level, 0) + 1
            iid = f"R{index:03d}"
            short = reminder.message.split("（", 1)[0]
            if len(short) > 30:
                short = short[:29] + "…"
            self.reminder_tree.insert(
                "",
                "end",
                iid=iid,
                values=(LEVEL_LABEL.get(reminder.level, "提示"), short),
                tags=(reminder.level,),
            )
            self.reminder_map[iid] = (reminder.gid, reminder.uid)
        error_count = counts.get("error", 0)
        warn_count = counts.get("warn", 0)
        if reminders:
            self.reminder_title.set(f"{error_count} 必须补充 · {warn_count} 建议核对")
            self._show_reminder_view(True)
        else:
            self.reminder_title.set("0 项")
            self._show_reminder_view(False)

    def _update_summary(self) -> None:
        if not self.result:
            self.total_var.set("¥0.00")
            self.count_var.set("")
            if hasattr(self, "stat_total_var"):
                self.stat_total_var.set("¥0.00")
                self.stat_materials_var.set("0 份")
                self.stat_groups_var.set("0 组")
                self.stat_missing_var.set("0 项")
                self.stat_missing_label.configure(fg=COLORS["leaf"])
            return
        result = self.result
        self.total_var.set(f"¥{result.total:,.2f}")
        fallback = f" · {result.fallback_count} 组按发票兜底" if result.fallback_count else ""
        unmatched = f" · {result.unmatched_payment_count} 笔未匹配" if result.unmatched_payment_count else ""
        self.count_var.set(f"{len(result.materials)} 份材料 · {len(result.groups)} 组{fallback}{unmatched}")
        if hasattr(self, "stat_total_var"):
            errors = sum(1 for r in result.reminders if r.level == "error")
            warns = sum(1 for r in result.reminders if r.level == "warn")
            missing = errors + warns
            self.stat_total_var.set(f"¥{result.total:,.2f}")
            self.stat_materials_var.set(f"{len(result.materials)} 份")
            self.stat_groups_var.set(f"{len(result.groups)} 组")
            self.stat_missing_var.set(f"{missing} 项")
            self.stat_missing_label.configure(fg=COLORS["error"] if errors else (COLORS["warn"] if warns else COLORS["leaf"]))

    def _on_event_type_changed(self, _event=None) -> None:
        if self.result and not self.scanning:
            self._refresh_current()
            self.status_var.set(f"已切换为“{self.event_var.get()}”，缺失材料提醒已重新计算。")

    def _refresh_current(self) -> None:
        if not self.result:
            return
        selected = self._selected_material()
        selected_uid = selected.uid if selected else None
        self.result = recompute(self.result, self.event_var.get())
        self._populate_tree()
        self._populate_reminders()
        self._update_summary()
        if selected_uid and self.tree.exists(selected_uid):
            self.tree.selection_set(selected_uid)
            self.tree.focus(selected_uid)
            self.tree.see(selected_uid)

    def _selected_iid(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _selected_material(self) -> Material | None:
        iid = self._selected_iid()
        if not iid:
            return None
        return self.material_rows.get(iid)

    def _on_tree_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        column = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if region == "cell" and column == "#1" and row:
            self.tree.selection_set(row)
            self.toggle_selected()
            return "break"
        return None

    def _on_tree_select(self, _event=None) -> None:
        material = self._selected_material()
        if material:
            self._show_material(material)
            return
        iid = self._selected_iid()
        if iid and self.result:
            if iid == "PAYMENTS":
                self._set_detail("支付记录", "支付截图按前面发票组的顺序排列。未匹配到发票的记录会排在最后并标记。")
            elif iid == "UNGROUPED":
                self._set_detail("未分组", "这些材料没有归入票据组，请检查类型或手动编辑。")
            else:
                group = self.result.group_for(iid)
                if group:
                    lines = [
                        f"分区：{group.section_name}",
                        f"摘要：{group.title}",
                        f"日期：{group.sort_date.isoformat() if group.sort_date and group.sort_date != date.max else '未识别'}",
                        f"票面合计：¥{group.invoice_total:,.2f}",
                        f"支付合计：¥{group.payment_total:,.2f}",
                        f"计入金额：¥{group.effective_amount:,.2f}",
                    ]
                    if group.flags:
                        lines.append("提示：" + "；".join(group.flags))
                    self._set_detail("票据组", "\n".join(lines))

    def _on_tree_double_click(self, event):
        column = self.tree.identify_column(event.x)
        if column == "#1":
            return "break"
        if self._selected_material():
            self.edit_selected()
        return "break"

    def _show_material(self, material: Material) -> None:
        self.detail_info.configure(
            text=f"{material.method} · {human_size(material.size_bytes)} · {material.confidence}"
        )
        lines = [
            f"文件：{material.path}",
            f"分区：{material.section_name}",
            f"类型：{material.subtype_name} / {material.role_name}",
            f"业务日期：{material.business_date.isoformat() if material.business_date else '未识别'}",
            f"开票日期：{material.invoice_date.isoformat() if material.invoice_date else '未识别'}",
            f"金额：¥{material.amount:,.2f}" if material.amount is not None else "金额：未识别",
            f"订单金额：¥{material.order_amount:,.2f}" if material.order_amount is not None else "",
            f"发票号：{material.invoice_no}" if material.invoice_no else "",
            f"销售方/收款方：{material.seller or material.payee}" if material.seller or material.payee else "",
            f"备注：{material.note}" if material.note else "",
            "",
            "识别文本：",
            material.text[:6000] if material.text else "（无文本）",
        ]
        self._set_detail("材料信息", "\n".join(line for line in lines if line is not None))

    def _set_detail(self, title: str, text: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")
        self.detail_info.configure(text=title)

    def toggle_selected(self) -> None:
        material = self._selected_material()
        if material:
            material.include = not material.include
            self._refresh_current()
            return
        iid = self._selected_iid()
        if iid and iid not in {"PAYMENTS", "UNGROUPED"} and self.result:
            group = self.result.group_for(iid)
            if group:
                target = not any(m.include for m in group.materials)
                for item in group.materials:
                    item.include = target
                self._refresh_current()

    def set_all_included(self, include: bool) -> None:
        if not self.result:
            return
        for material in self.result.materials:
            material.include = include
        self._refresh_current()

    def edit_selected(self) -> None:
        material = self._selected_material()
        if not material:
            messagebox.showinfo("编辑材料", "请先在左侧选择一份材料。", parent=self)
            return
        dialog = EditDialog(self, material)
        self.wait_window(dialog)
        if dialog.saved:
            self._refresh_current()

    def open_selected_file(self) -> None:
        material = self._selected_material()
        if not material:
            return
        try:
            os.startfile(str(material.path))
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def open_folder(self) -> None:
        folder = self.folder_var.get().strip()
        if folder and Path(folder).exists():
            try:
                os.startfile(folder)
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc), parent=self)

    def _on_reminder_select(self, _event=None) -> None:
        if not self.result:
            return
        selection = self.reminder_tree.selection()
        if not selection:
            return
        try:
            index = int(selection[0][1:])
        except ValueError:
            return
        if 0 <= index < len(self.result.reminders):
            reminder = self.result.reminders[index]
            lines = [
                f"[{LEVEL_LABEL.get(reminder.level, '提示')}]",
                reminder.message,
            ]
            if reminder.detail:
                lines.extend(["", reminder.detail])
            if reminder.gid:
                group = self.result.group_for(reminder.gid)
                if group:
                    lines.extend(["", f"相关票据组：{group.title}", f"票面合计：¥{group.invoice_total:,.2f}", f"支付合计：¥{group.payment_total:,.2f}"])
            self._set_detail("待补提醒", "\n".join(lines))

    def _on_reminder_double_click(self, _event=None) -> None:
        iid = self.reminder_tree.selection()
        if not iid or not self.result:
            return
        gid, uid = self.reminder_map.get(iid[0], ("", ""))
        target = uid if uid and self.tree.exists(uid) else gid
        if target and self.tree.exists(target):
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)

    def export_reminders(self) -> None:
        if not self.result:
            messagebox.showinfo("导出提醒", "请先扫描材料。", parent=self)
            return
        if not self.result.reminders:
            messagebox.showinfo("导出提醒", "未发现明显缺失材料。", parent=self)
            return
        path = filedialog.asksaveasfilename(
            title="导出待补提醒",
            initialdir=self.folder_var.get() or str(Path.home()),
            initialfile="待补材料提醒.txt",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt")],
        )
        if not path:
            return
        labels = {"error": "必须补充", "warn": "建议核对", "info": "提示"}
        lines = [f"报销事项：{self.result.event_name}", f"合计：¥{self.result.total:,.2f}", ""]
        lines.extend(f"[{labels.get(r.level, '提示')}] {r.message}" for r in self.result.reminders)
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        messagebox.showinfo("导出完成", f"已导出：\n{path}", parent=self)

    def generate(self) -> None:
        if self.generating:
            return
        if not self.result:
            messagebox.showinfo("生成 Word", "请先扫描材料。", parent=self)
            return
        if not any(m.include for m in self.result.materials):
            messagebox.showinfo("生成 Word", "没有勾选任何材料。", parent=self)
            return
        errors = [r for r in self.result.reminders if r.level == "error"]
        warns = [r for r in self.result.reminders if r.level == "warn"]
        if errors or warns:
            lines = [f"发现 {len(errors)} 项必须补充、{len(warns)} 项建议核对：", ""]
            for reminder in (errors + warns)[:10]:
                lines.append(f"· {reminder.message}")
            if len(errors) + len(warns) > 10:
                lines.append("…")
            lines.append("")
            lines.append("仍然生成 Word 吗？")
            if not messagebox.askyesno("生成前检查", "\n".join(lines), parent=self, icon="warning"):
                return
        folder = Path(self.folder_var.get())
        initial = default_filename(self.result)
        path = filedialog.asksaveasfilename(
            title="保存报销材料 Word",
            initialdir=str(folder),
            initialfile=initial,
            defaultextension=".docx",
            filetypes=[("Word 文档", "*.docx")],
        )
        if not path:
            return
        self._start_generate(Path(path))

    def _start_generate(self, path: Path) -> None:
        self.generating = True
        self.generate_button.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.status_var.set("正在生成 Word…")
        result = self.result
        queue_: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                output = build_document(result, path)
                queue_.put(("done", output))
            except Exception as exc:
                queue_.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

        def poll() -> None:
            try:
                kind, payload = queue_.get_nowait()
            except queue.Empty:
                self.after(100, poll)
                return
            self.generating = False
            self.generate_button.configure(state="normal")
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            if kind == "done":
                self.current_output = payload
                self.status_var.set(f"Word 已生成：{payload}")
                if messagebox.askyesno("生成完成", f"已生成：\n{payload}\n\n是否打开文档？", parent=self):
                    try:
                        os.startfile(str(payload))
                    except Exception as exc:
                        messagebox.showerror("打开失败", str(exc), parent=self)
            else:
                self.status_var.set("Word 生成失败。")
                messagebox.showerror("生成失败", payload, parent=self)

        self.after(100, poll)

    def _on_close(self) -> None:
        self.settings.update(
            {
                "last_folder": self.folder_var.get(),
                "applicant": self.applicant_var.get(),
                "department": self.department_var.get(),
                "event_type": self.event_var.get(),
                "use_ocr": self.ocr_var.get(),
                "include_subfolders": self.recursive_var.get(),
                "window_geometry": self.geometry(),
            }
        )
        save_settings(self.settings)
        self.scan_token += 1
        self.destroy()
