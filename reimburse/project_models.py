from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

APP_VERSION = "2.0"

STATUSES: dict[str, dict[str, str]] = {
    "draft": {"label": "整理中", "color": "#B7791F", "bg": "#FDF3E1"},
    "materials_ready": {"label": "已整理好材料", "color": "#3E9AA8", "bg": "#E3F1F4"},
    "printed": {"label": "已打印提交", "color": "#4A6FA5", "bg": "#E8EEF9"},
    "completed": {"label": "已报销完成", "color": "#5B8C5A", "bg": "#EAF5E8"},
}
STATUS_ORDER = ["draft", "materials_ready", "printed", "completed"]

SLOT_LABELS = {
    "invoice": "发票",
    "payment": "支付记录",
    "itinerary": "行程单",
    "toll": "高速费发票",
    "photo": "实物照片",
    "notice_zh": "录用通知（中文）",
    "notice_en": "录用通知（英文）",
    "other": "其他材料",
}

SLOT_ICONS = {
    "invoice": "🧾",
    "payment": "💳",
    "itinerary": "🗺",
    "toll": "🛣",
    "photo": "📷",
    "notice_zh": "📄",
    "notice_en": "📄",
    "other": "📎",
}

RECORD_TEMPLATES: dict[str, dict] = {
    "traffic": {
        "label": "交通费",
        "icon": "🚄",
        "section": "traffic",
        "subtypes": [("flight", "机票"), ("train", "火车票"), ("traffic_other", "其他交通")],
        "slots": ["invoice", "payment", "itinerary", "other"],
        "required": ["invoice", "payment"],
    },
    "hotel": {
        "label": "住宿费",
        "icon": "🏨",
        "section": "hotel",
        "subtypes": [("hotel", "住宿")],
        "slots": ["invoice", "payment", "other"],
        "required": ["invoice", "payment"],
    },
    "taxi": {
        "label": "打车费",
        "icon": "🚕",
        "section": "taxi",
        "subtypes": [("taxi", "打车")],
        "slots": ["invoice", "payment", "itinerary", "toll", "other"],
        "required": ["invoice", "payment", "itinerary"],
    },
    "purchase": {
        "label": "采购",
        "icon": "📦",
        "section": "other",
        "subtypes": [("purchase", "采购")],
        "slots": ["invoice", "payment", "photo", "other"],
        "required": ["invoice", "payment"],
    },
    "conference": {
        "label": "参会",
        "icon": "🎓",
        "section": "other",
        "subtypes": [("meeting_fee", "会议注册费")],
        "slots": ["invoice", "payment", "notice_zh", "notice_en", "other"],
        "required": ["invoice", "payment", "notice_zh", "notice_en"],
    },
    "other": {
        "label": "其他费用",
        "icon": "🧩",
        "section": "other",
        "subtypes": [("other", "其他")],
        "slots": ["invoice", "payment", "other"],
        "required": ["invoice", "payment"],
    },
}

PROJECT_TYPES = ["出差", "参会", "采购", "其他"]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _today_iso() -> str:
    return date.today().isoformat()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sanitize_filename(name: str, fallback: str = "未命名") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "")
    name = re.sub(r"\s+", " ", name).strip(" ._-")
    return name[:60] or fallback


@dataclass
class Asset:
    id: str
    rel_path: str
    name: str
    source: str = "file"
    added_at: str = field(default_factory=_now_iso)
    page_count: int = 0
    amount: float | None = None
    refund_amount: float | None = None
    amount_manual: bool = False
    note: str = ""

    def path(self, project_folder: Path) -> Path:
        return project_folder / self.rel_path

    @property
    def display_name(self) -> str:
        return re.sub(r"^\d{8}_\d{6}_", "", self.name or "素材")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "rel_path": self.rel_path,
            "name": self.name,
            "source": self.source,
            "added_at": self.added_at,
            "page_count": self.page_count,
            "amount": self.amount,
            "refund_amount": self.refund_amount,
            "amount_manual": self.amount_manual,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Asset":
        return cls(
            id=data.get("id") or new_id(),
            rel_path=data.get("rel_path", ""),
            name=data.get("name", ""),
            source=data.get("source", "file"),
            added_at=data.get("added_at", _now_iso()),
            page_count=int(data.get("page_count", 0) or 0),
            amount=_float_or_none(data.get("amount")),
            refund_amount=_float_or_none(data.get("refund_amount")),
            amount_manual=bool(data.get("amount_manual", False)),
            note=data.get("note", ""),
        )


@dataclass
class Record:
    id: str
    type: str
    title: str = ""
    date: str = field(default_factory=_today_iso)
    amount: float | None = None
    payment_amount: float | None = None
    invoice_no: str = ""
    seller: str = ""
    note: str = ""
    date_manual: bool = False
    date_ocr: bool = False
    payment_manual: bool = False
    slots: dict[str, list[Asset]] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def template(self) -> dict:
        return RECORD_TEMPLATES.get(self.type, RECORD_TEMPLATES["other"])

    @property
    def label(self) -> str:
        return self.template()["label"]

    @property
    def icon(self) -> str:
        return self.template()["icon"]

    @property
    def section(self) -> str:
        return self.template()["section"]

    @property
    def display_title(self) -> str:
        if self.title.strip():
            return self.title.strip()
        if self.seller.strip():
            return self.seller.strip()
        if self.invoice_no:
            return f"{self.label} {self.invoice_no[-6:]}"
        return self.label

    @property
    def business_date(self) -> date | None:
        try:
            return date.fromisoformat(self.date)
        except ValueError:
            return None

    def slot_assets(self, key: str) -> list[Asset]:
        return self.slots.setdefault(key, [])

    def has_slot(self, key: str) -> bool:
        return any(a.rel_path for a in self.slot_assets(key))

    def missing_slots(self) -> list[tuple[str, str, str]]:
        """Return (slot_key, label, level)."""
        missing: list[tuple[str, str, str]] = []
        required = list(self.template().get("required", []))
        if self.type == "purchase" and (self.amount or 0) > 500:
            if "photo" not in required:
                required.append("photo")
        for key in required:
            if not self.has_slot(key):
                missing.append((key, SLOT_LABELS.get(key, key), "error"))
        if self.amount is None or self.amount <= 0:
            if self.has_slot("invoice"):
                missing.append(("amount", "发票金额", "warn"))
        if self.payment_amount is None or self.payment_amount < 0:
            if self.has_slot("payment"):
                missing.append(("payment_amount", "支付金额", "warn"))
        return missing

    def payment_asset_sum(self) -> float:
        total = 0.0
        for asset in self.slot_assets("payment"):
            if asset.amount is None or asset.amount < 0:
                continue
            total += asset.amount - (asset.refund_amount or 0.0)
        return round(total, 2)

    def refresh_payment_total(self) -> None:
        if self.payment_manual:
            return
        # Unknown amounts and a confirmed zero net payment have different meanings.
        known = any(a.amount is not None for a in self.slot_assets("payment"))
        self.payment_amount = self.payment_asset_sum() if known else None

    def apply_ocr_date(self, value: str) -> bool:
        if not value or self.date_manual:
            return False
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return False
        current = self.business_date
        if current is None or not self.date_ocr:
            new_value = parsed.isoformat()
        elif parsed < current:
            # Multiple payments for one combined taxi invoice: use the earliest
            # actual ride/payment date.
            new_value = parsed.isoformat()
        else:
            return False
        if new_value == self.date:
            self.date_ocr = True
            return False
        self.date = new_value
        self.date_ocr = True
        return True

    @property
    def effective_amount(self) -> float:
        if self.has_slot("payment") and self.payment_amount is not None:
            return round(self.payment_amount, 2)
        if self.amount is not None:
            return round(self.amount, 2)
        return 0.0

    @property
    def uses_invoice_fallback(self) -> bool:
        return (not self.has_slot("payment") or self.payment_amount is None) and (self.amount or 0) > 0

    @property
    def all_assets(self) -> list[Asset]:
        result: list[Asset] = []
        for assets in self.slots.values():
            result.extend(assets)
        return result

    def asset_count(self) -> int:
        return len(self.all_assets)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "date": self.date,
            "amount": self.amount,
            "payment_amount": self.payment_amount,
            "invoice_no": self.invoice_no,
            "seller": self.seller,
            "note": self.note,
            "date_manual": self.date_manual,
            "date_ocr": self.date_ocr,
            "payment_manual": self.payment_manual,
            "slots": {key: [a.to_dict() for a in assets] for key, assets in self.slots.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        slots = {
            key: [Asset.from_dict(a) for a in values]
            for key, values in (data.get("slots") or {}).items()
        }
        return cls(
            id=data.get("id") or new_id(),
            type=data.get("type", "other"),
            title=data.get("title", ""),
            date=data.get("date", _today_iso()),
            amount=_float_or_none(data.get("amount")),
            payment_amount=_float_or_none(data.get("payment_amount")),
            invoice_no=data.get("invoice_no", ""),
            seller=data.get("seller", ""),
            note=data.get("note", ""),
            date_manual=bool(data.get("date_manual", False)),
            date_ocr=bool(data.get("date_ocr", False)),
            payment_manual=bool(data.get("payment_manual", False)),
            slots=slots,
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
        )


def _float_or_none(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


@dataclass
class ProjectReminder:
    level: str
    code: str
    message: str
    record_id: str = ""
    slot: str = ""


@dataclass
class Project:
    folder: Path
    id: str
    name: str
    type: str = "出差"
    applicant: str = ""
    department: str = ""
    date_start: str = field(default_factory=_today_iso)
    date_end: str = field(default_factory=_today_iso)
    status: str = "draft"
    notes: str = ""
    records: list[Record] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    outputs: list[str] = field(default_factory=list)

    @property
    def meta_path(self) -> Path:
        return self.folder / "project.json"

    @property
    def assets_dir(self) -> Path:
        return self.folder / "assets"

    @property
    def output_dir(self) -> Path:
        return self.folder / "output"

    @property
    def status_label(self) -> str:
        return STATUSES.get(self.status, STATUSES["draft"])["label"]

    @property
    def status_color(self) -> str:
        return STATUSES.get(self.status, STATUSES["draft"])["color"]

    @property
    def status_bg(self) -> str:
        return STATUSES.get(self.status, STATUSES["draft"])["bg"]

    @property
    def date_range_text(self) -> str:
        if self.date_start and self.date_end:
            if self.date_start == self.date_end:
                return self.date_start
            return f"{self.date_start} 至 {self.date_end}"
        return self.date_start or self.date_end or "未设置日期"

    def ensure_dirs(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def total(self) -> float:
        return round(sum(record.effective_amount for record in self.records), 2)

    def fallback_count(self) -> int:
        return sum(
            1
            for record in self.records
            if record.uses_invoice_fallback
        )

    def missing_reminders(self) -> list[ProjectReminder]:
        reminders: list[ProjectReminder] = []
        if not self.records:
            reminders.append(ProjectReminder("warn", "no_records", "这个项目还没有费用记录"))
        seen_invoice: dict[str, str] = {}
        for record in self.records:
            for slot, label, level in record.missing_slots():
                code = f"missing_{slot}"
                if slot == "photo":
                    message = f"{record.display_title}：金额超过 500 元，缺实物照片"
                elif slot in {"notice_zh", "notice_en"}:
                    message = f"{record.display_title}：缺{SLOT_LABELS[slot]}"
                elif slot == "itinerary":
                    message = f"{record.display_title}：缺行程单"
                elif slot in {"amount", "payment_amount"}:
                    message = f"{record.display_title}：未填写{label}"
                else:
                    message = f"{record.display_title}：缺{label}"
                reminders.append(ProjectReminder(level, code, message, record.id, slot))
            if record.invoice_no:
                key = record.invoice_no.strip()
                if key in seen_invoice:
                    reminders.append(
                        ProjectReminder(
                            "warn",
                            "duplicate_invoice",
                            f"发票号重复：{record.display_title} 与 {seen_invoice[key]}",
                            record.id,
                        )
                    )
                else:
                    seen_invoice[key] = record.display_title
            if (
                record.has_slot("payment")
                and record.amount is not None
                and record.payment_amount is not None
                and record.amount > 0
                and record.payment_amount > 0
            ):
                diff = round(record.amount - record.payment_amount, 2)
                if abs(diff) > max(1.0, record.amount * 0.02):
                    text = f"支付比发票少 {diff:.2f} 元" if diff > 0 else f"支付比发票多 {abs(diff):.2f} 元"
                    reminders.append(
                        ProjectReminder(
                            "warn",
                            "amount_mismatch",
                            f"{record.display_title}：{text}，请核对",
                            record.id,
                        )
                    )
            if record.type == "taxi" and record.has_slot("payment") and not record.has_slot("toll"):
                if record.payment_amount and record.amount and record.payment_amount > record.amount + 1:
                    reminders.append(
                        ProjectReminder(
                            "warn",
                            "possible_toll",
                            f"{record.display_title}：支付高于发票，可能含高速费或纸质票据",
                            record.id,
                        )
                    )
        if self.total() <= 0 and self.records:
            reminders.append(ProjectReminder("error", "zero_total", "项目总额为 0，请检查金额"))
        order = {"error": 0, "warn": 1, "info": 2}
        reminders.sort(key=lambda r: (order.get(r.level, 9), r.message))
        return reminders

    def missing_count(self) -> int:
        return sum(1 for r in self.missing_reminders() if r.level in {"error", "warn"})

    def add_record(self, record_type: str, record_date: str | None = None) -> Record:
        record = Record(
            id=new_id(),
            type=record_type,
            date=record_date or self.date_start or _today_iso(),
        )
        self.records.append(record)
        self.touch()
        return record

    def remove_record(self, record_id: str, delete_assets: bool = True) -> None:
        record = self.find_record(record_id)
        if not record:
            return
        if delete_assets:
            for asset in record.all_assets:
                try:
                    asset.path(self.folder).unlink(missing_ok=True)
                except OSError:
                    pass
        self.records = [r for r in self.records if r.id != record_id]
        self.touch()

    def find_record(self, record_id: str) -> Record | None:
        for record in self.records:
            if record.id == record_id:
                return record
        return None

    def sort_records(self) -> None:
        section_order = {"traffic": 10, "hotel": 20, "taxi": 30, "other": 40, "notice": 60}
        self.records.sort(key=lambda r: (section_order.get(r.section, 99), r.date, r.created_at))

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def save(self) -> None:
        self.ensure_dirs()
        self.touch()
        payload = self.to_dict()
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.meta_path)

    def to_dict(self) -> dict:
        return {
            "version": 2,
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "applicant": self.applicant,
            "department": self.department,
            "date_start": self.date_start,
            "date_end": self.date_end,
            "status": self.status,
            "notes": self.notes,
            "records": [r.to_dict() for r in self.records],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "outputs": self.outputs,
        }

    @classmethod
    def load(cls, folder: Path) -> "Project":
        meta_path = folder / "project.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        project = cls(
            folder=folder,
            id=data.get("id") or new_id(),
            name=data.get("name") or folder.name,
            type=data.get("type", "出差"),
            applicant=data.get("applicant", ""),
            department=data.get("department", ""),
            date_start=data.get("date_start", _today_iso()),
            date_end=data.get("date_end", _today_iso()),
            status=data.get("status", "draft"),
            notes=data.get("notes", ""),
            records=[Record.from_dict(r) for r in data.get("records", [])],
            created_at=data.get("created_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
            outputs=data.get("outputs", []),
        )
        project.ensure_dirs()
        return project

    def import_asset(self, source: Path, source_kind: str = "file") -> Asset:
        self.ensure_dirs()
        if not source.exists():
            raise FileNotFoundError(source)
        suffix = source.suffix.lower()
        stem = sanitize_filename(source.stem, "素材")
        prefix = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = self.assets_dir / f"{prefix}_{stem}{suffix}"
        counter = 2
        while target.exists():
            target = self.assets_dir / f"{prefix}_{stem}_{counter}{suffix}"
            counter += 1
        shutil.copy2(source, target)
        asset = Asset(id=new_id(), rel_path=str(target.relative_to(self.folder)).replace("\\", "/"), name=target.name, source=source_kind)
        return asset

    def save_image_asset(self, image, source_kind: str = "screenshot") -> Asset:
        self.ensure_dirs()
        prefix = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = self.assets_dir / f"{prefix}_{source_kind}.png"
        counter = 2
        while target.exists():
            target = self.assets_dir / f"{prefix}_{source_kind}_{counter}.png"
            counter += 1
        image.save(target, "PNG")
        return Asset(id=new_id(), rel_path=str(target.relative_to(self.folder)).replace("\\", "/"), name=target.name, source=source_kind)


def create_project(
    root: Path,
    name: str,
    project_type: str,
    date_start: str,
    date_end: str,
    applicant: str = "",
    department: str = "",
) -> Project:
    root.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(name)
    start = date_start.replace("-", "") if date_start else date.today().strftime("%Y%m%d")
    end = date_end.replace("-", "") if date_end else start
    date_part = start if start == end else f"{start}-{end[-4:]}"
    folder_name = f"{safe_name}_{date_part}"
    folder = root / folder_name
    counter = 2
    while folder.exists():
        folder = root / f"{folder_name}_{counter}"
        counter += 1
    project = Project(
        folder=folder,
        id=new_id(),
        name=name,
        type=project_type,
        applicant=applicant,
        department=department,
        date_start=date_start,
        date_end=date_end,
        status="draft",
    )
    project.ensure_dirs()
    project.save()
    return project


def infer_name_from_folder(folder: Path) -> tuple[str, str, str]:
    name = folder.name
    date_start = date.today().isoformat()
    date_end = date_start
    match = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", name)
    if match:
        try:
            d = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            date_start = date_end = d.isoformat()
            name = name[: match.start()].strip(" _-")
        except ValueError:
            pass
    if not name:
        name = folder.name
    return name, date_start, date_end
