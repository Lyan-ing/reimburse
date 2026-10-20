from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable


SECTION_ORDER: dict[str, int] = {
    "traffic": 10,
    "hotel": 20,
    "taxi": 30,
    "other": 40,
    "payment": 50,
    "notice": 60,
}

SECTION_NAMES: dict[str, str] = {
    "traffic": "交通票据",
    "hotel": "住宿票据",
    "taxi": "打车交通票据",
    "other": "其他票据",
    "payment": "支付记录",
    "notice": "会议通知/录用通知",
}

ROLE_NAMES: dict[str, str] = {
    "invoice": "发票",
    "itinerary": "行程单",
    "toll": "高速费",
    "payment": "支付记录",
    "notice": "通知/证明",
    "photo": "实物照片",
    "unknown": "其他材料",
}

SUBTYPE_NAMES: dict[str, str] = {
    "flight": "机票",
    "train": "火车票",
    "refund": "退票",
    "traffic_other": "交通",
    "hotel": "住宿",
    "taxi": "打车",
    "toll": "高速费",
    "meeting_fee": "会议费",
    "purchase": "采购",
    "parking": "停车费",
    "other": "其他",
    "payment": "支付",
    "payment_refund": "退款",
    "notice": "通知",
    "photo": "实物照片",
}

EVENT_TYPES = ["出差", "参会", "采购"]


@dataclass
class Material:
    path: Path

    section: str = "other"
    subtype: str = "other"
    role: str = "unknown"
    include: bool = True

    amount: float | None = None
    order_amount: float | None = None
    refund_amount: float | None = None
    date: date | None = None
    invoice_date: date | None = None
    end_date: date | None = None

    invoice_no: str = ""
    seller: str = ""
    buyer: str = ""
    payee: str = ""
    order_no: str = ""
    traveler: str = ""

    text: str = ""
    method: str = "未解析"
    confidence: str = "低"
    note: str = ""
    error: str = ""
    page_count: int = 0

    duplicate: bool = False
    duplicate_of: str = ""
    group_id: str = ""
    source_hash: str = ""
    manual_fields: set[str] = field(default_factory=set)
    preview_pages: list[str] = field(default_factory=list)

    size_bytes: int = 0
    mtime: float = 0.0

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def uid(self) -> str:
        return str(self.path)

    @property
    def section_rank(self) -> int:
        return SECTION_ORDER.get(self.section, 99)

    @property
    def section_name(self) -> str:
        return SECTION_NAMES.get(self.section, "其他")

    @property
    def role_name(self) -> str:
        return ROLE_NAMES.get(self.role, "其他")

    @property
    def subtype_name(self) -> str:
        return SUBTYPE_NAMES.get(self.subtype, self.subtype or "其他")

    @property
    def business_date(self) -> date | None:
        return self.date or self.invoice_date

    @property
    def net_amount(self) -> float:
        if self.amount is None:
            return 0.0
        if self.refund_amount:
            return round(abs(self.amount) - self.refund_amount, 2)
        return round(self.amount, 2)

    def display_amount(self) -> str:
        if self.amount is None:
            return ""
        return f"{self.amount:,.2f}"

    def display_date(self) -> str:
        d = self.business_date
        return d.isoformat() if d else ""

    def set_field(self, name: str, value) -> None:
        setattr(self, name, value)
        self.manual_fields.add(name)

    def summary(self) -> str:
        if self.role == "payment":
            bits = [self.payee or self.note or "支付记录"]
            if self.amount is not None:
                bits.append(f"¥{self.amount:,.2f}")
            return " ".join(bits)
        bits = []
        if self.subtype_name:
            bits.append(self.subtype_name)
        if self.seller:
            bits.append(self.seller)
        if self.amount is not None:
            bits.append(f"¥{self.amount:,.2f}")
        return " ".join(bits) or self.stem


@dataclass
class ExpenseGroup:
    gid: str
    section: str
    subtype: str
    date: date | None
    title: str
    materials: list[Material] = field(default_factory=list)
    invoice: Material | None = None
    itineraries: list[Material] = field(default_factory=list)
    tolls: list[Material] = field(default_factory=list)
    payments: list[Material] = field(default_factory=list)
    photos: list[Material] = field(default_factory=list)
    notices: list[Material] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    order: int = 0

    @property
    def section_rank(self) -> int:
        return SECTION_ORDER.get(self.section, 99)

    @property
    def section_name(self) -> str:
        return SECTION_NAMES.get(self.section, "其他")

    @property
    def invoices(self) -> list[Material]:
        return [m for m in self.materials if m.role in {"invoice", "toll"} and m.include]

    @property
    def invoice_total(self) -> float:
        return round(sum((m.amount or 0.0) for m in self.invoices), 2)

    @property
    def payment_total(self) -> float:
        seen: set[str] = set()
        total = 0.0
        for p in self.payments:
            if not p.include or p.duplicate:
                continue
            key = p.order_no or p.source_hash or p.uid
            if key in seen:
                continue
            seen.add(key)
            total += p.net_amount
        return round(total, 2)

    @property
    def effective_amount(self) -> float:
        if self.payments:
            return self.payment_total
        return self.invoice_total

    @property
    def has_invoice(self) -> bool:
        return bool(self.invoices)

    @property
    def has_payment(self) -> bool:
        return any(
            p.include and not p.duplicate and abs(p.net_amount) > 0.001
            for p in self.payments
        )

    @property
    def is_taxable_expense(self) -> bool:
        return self.section in {"traffic", "hotel", "taxi", "other"}

    @property
    def sort_date(self) -> date:
        if self.date:
            return self.date
        for m in self.materials:
            if m.business_date:
                return m.business_date
        return date.max

    @property
    def sort_key(self) -> tuple:
        return (self.section_rank, self.sort_date, self.order, self.title)

    def short_title(self, limit: int = 32) -> str:
        text = self.title
        if not text and self.invoice is not None:
            text = self.invoice.summary()
        text = " ".join((text or "").split())
        if len(text) > limit:
            return text[: limit - 1] + "…"
        return text

    def all_materials(self) -> list[Material]:
        seen: set[str] = set()
        result: list[Material] = []
        for m in self.materials:
            if m.uid not in seen:
                seen.add(m.uid)
                result.append(m)
        return result


@dataclass
class Reminder:
    level: str
    code: str
    message: str
    gid: str = ""
    uid: str = ""
    detail: str = ""


@dataclass
class ScanResult:
    folder: Path
    event_type: str
    event_name: str
    date_start: date | None = None
    date_end: date | None = None
    materials: list[Material] = field(default_factory=list)
    groups: list[ExpenseGroup] = field(default_factory=list)
    reminders: list[Reminder] = field(default_factory=list)
    ordered_payments: list[Material] = field(default_factory=list)
    total: float = 0.0
    fallback_count: int = 0
    unmatched_payment_count: int = 0
    duplicate_count: int = 0
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    total_detail: str = ""

    def group_for(self, gid: str) -> ExpenseGroup | None:
        for g in self.groups:
            if g.gid == gid:
                return g
        return None

    def material_for(self, uid: str) -> Material | None:
        for m in self.materials:
            if m.uid == uid:
                return m
        return None

    def included_materials(self) -> Iterable[Material]:
        return (m for m in self.materials if m.include)

    @property
    def payment_total(self) -> float:
        return round(sum(g.payment_total for g in self.groups if g.section != "payment"), 2)
