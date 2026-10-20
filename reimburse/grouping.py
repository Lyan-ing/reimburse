from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .classify import normalize_merchant
from .models import (
    SECTION_ORDER,
    ExpenseGroup,
    Material,
    ScanResult,
)
from .parser import match_merchant
from .scanner import BatchInfo, natural_key
from .textutil import compact, similarity


def _date_diff(a: date | None, b: date | None) -> int:
    if not a or not b:
        return 99
    return abs((a - b).days)


def _close(a: float | None, b: float | None, tolerance: float = 0.01) -> bool:
    return a is not None and b is not None and abs(a - b) <= tolerance


def _make_title(material: Material) -> str:
    if material.role == "notice":
        for line in (material.text or "").splitlines():
            line = line.strip()
            if line:
                return line[:60]
        return material.stem
    if material.seller:
        return f"{material.subtype_name} {material.seller}"
    return f"{material.subtype_name} {material.stem}"


def _new_group(groups: list[ExpenseGroup], material: Material) -> ExpenseGroup:
    group = ExpenseGroup(
        gid=f"G{len(groups) + 1:03d}",
        section=material.section,
        subtype=material.subtype,
        date=material.business_date,
        title=_make_title(material),
        order=len(groups),
    )
    groups.append(group)
    _add_material(group, material)
    return group


def _add_material(group: ExpenseGroup, material: Material) -> None:
    if material.uid not in {m.uid for m in group.materials}:
        group.materials.append(material)
    material.group_id = group.gid
    if material.role == "invoice" and group.invoice is None:
        group.invoice = material
    elif material.role == "itinerary" and material not in group.itineraries:
        group.itineraries.append(material)
    elif material.role == "toll" and material not in group.tolls:
        group.tolls.append(material)
    elif material.role == "payment" and material not in group.payments:
        group.payments.append(material)
    elif material.role == "photo" and material not in group.photos:
        group.photos.append(material)
    elif material.role == "notice" and material not in group.notices:
        group.notices.append(material)


def _amount_variants(value: float | None) -> list[float]:
    if value is None:
        return []
    variants = [value, round(value / 10, 2), round(value / 100, 2)]
    result: list[float] = []
    for item in variants:
        if item > 0 and item not in result:
            result.append(item)
    return result


def _find_taxi_for_itinerary(groups: list[ExpenseGroup], material: Material) -> ExpenseGroup | None:
    candidates: list[tuple[float, ExpenseGroup, float | None]] = []
    for group in groups:
        if group.section != "taxi" or group.invoice is None or group.itineraries:
            continue
        score = 0.0
        matched_variant = None
        for variant in _amount_variants(material.amount):
            if _close(variant, group.invoice.amount, 0.02):
                matched_variant = variant
                score += 60
                break
            if _close(variant, group.invoice.amount, 2.0):
                matched_variant = variant
                score += 25
                break
        diff = _date_diff(material.business_date, group.invoice.business_date)
        if diff == 0:
            score += 30
        elif diff <= 2:
            score += 20
        elif diff <= 7:
            score += 5
        else:
            score -= 30
        if score > 20:
            candidates.append((score, group, matched_variant))
    if not candidates:
        return None
    _, group, matched_variant = max(candidates, key=lambda item: item[0])
    if matched_variant is not None:
        material.amount = matched_variant
    return group


def _find_taxi_for_invoice(groups: list[ExpenseGroup], material: Material) -> ExpenseGroup | None:
    candidates: list[tuple[float, ExpenseGroup]] = []
    for group in groups:
        if group.section != "taxi" or group.invoice is not None:
            continue
        score = 0.0
        diff = _date_diff(material.business_date, group.date)
        if diff <= 1:
            score += 25
        elif diff <= 3:
            score += 15
        for itinerary in group.itineraries:
            if _close(itinerary.amount, material.amount, 2.0):
                score += 30
        if score >= 20:
            candidates.append((score, group))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _find_taxi_for_toll(groups: list[ExpenseGroup], material: Material) -> ExpenseGroup | None:
    candidates: list[tuple[float, ExpenseGroup]] = []
    for group in groups:
        if group.section != "taxi" or group.invoice is None or group.tolls:
            continue
        diff = _date_diff(material.business_date, group.invoice.business_date)
        score = 0.0
        if diff == 0:
            score = 40
        elif diff <= 1:
            score = 30
        elif diff <= 3:
            score = 15
        elif diff <= 7:
            score = 5
        if score:
            candidates.append((score, group))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _find_purchase_group(groups: list[ExpenseGroup], material: Material) -> ExpenseGroup | None:
    candidates: list[tuple[float, ExpenseGroup]] = []
    for group in groups:
        if group.section != "other" or group.invoice is None or group.photos:
            continue
        if group.subtype != "purchase":
            continue
        if material.business_date is None:
            candidates.append((1, group))
        else:
            diff = _date_diff(material.business_date, group.invoice.business_date)
            if diff <= 14:
                candidates.append((14 - diff, group))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def build_groups(materials: list[Material]) -> list[ExpenseGroup]:
    groups: list[ExpenseGroup] = []
    invoices = [m for m in materials if m.role == "invoice"]
    itineraries = [m for m in materials if m.role == "itinerary"]
    tolls = [m for m in materials if m.role == "toll"]
    notices = [m for m in materials if m.role == "notice"]
    photos = [m for m in materials if m.role == "photo"]
    unknowns = [m for m in materials if m.role == "unknown"]

    for material in invoices:
        if material.duplicate:
            continue
        if material.section == "taxi":
            existing = _find_taxi_for_invoice(groups, material)
            if existing:
                _add_material(existing, material)
                existing.title = _make_title(material)
                existing.date = existing.date or material.business_date
                continue
        _new_group(groups, material)

    for material in itineraries:
        if material.duplicate:
            continue
        target = _find_taxi_for_itinerary(groups, material)
        if target:
            _add_material(target, material)
            target.date = min(
                (d for d in (target.date, material.business_date) if d is not None),
                default=None,
            )
        else:
            group = _new_group(groups, material)
            group.section = "taxi"
            group.subtype = "taxi"
            group.title = f"打车 {material.display_date()} {material.stem}"
            group.flags.append("缺少打车发票")

    for material in tolls:
        if material.duplicate:
            continue
        target = _find_taxi_for_toll(groups, material)
        if target:
            _add_material(target, material)
        else:
            group = _new_group(groups, material)
            group.section = "taxi"
            group.title = f"高速费 {material.display_date()} {material.stem}"
            group.flags.append("未匹配到打车发票")

    for material in photos:
        if material.duplicate:
            continue
        target = _find_purchase_group(groups, material)
        if target:
            _add_material(target, material)
        else:
            group = _new_group(groups, material)
            group.section = "other"
            group.subtype = "photo"
            group.title = f"实物照片 {material.stem}"

    for material in notices:
        if material.duplicate:
            continue
        group = _new_group(groups, material)
        group.section = "notice"
        group.subtype = "notice"
        group.title = _make_title(material)

    for material in unknowns:
        if material.duplicate:
            continue
        group = _new_group(groups, material)
        group.section = "other"
        group.subtype = "other"
        group.title = f"其他材料 {material.stem}"

    # Attach duplicate invoice/photo materials to the group of their original so
    # the review screen still shows them, but they never affect totals.
    by_uid = {m.uid: m for m in materials}
    group_by_uid = {m.uid: g for g in groups for m in g.materials}
    for material in materials:
        if not material.duplicate or material.role == "payment":
            continue
        original = by_uid.get(material.duplicate_of)
        if original:
            group = group_by_uid.get(original.uid)
            if group:
                _add_material(group, material)
                material.include = False

    return groups


def _payment_category_hint(payment: Material) -> str:
    text = compact(payment.text) + compact(payment.name)
    payee = compact(payment.payee)
    blob = text + payee
    if any(word in blob for word in ("铁路", "12306", "火车")):
        return "traffic"
    if any(word in blob for word in ("航空", "机票", "航班")):
        return "traffic"
    if any(word in blob for word in ("酒店", "宾馆", "住宿", "旅馆")):
        return "hotel"
    if any(word in blob for word in ("高德", "打车", "出行", "滴滴", "客运", "出租", "曹操", "首汽")):
        return "taxi"
    return ""


def _payment_score(payment: Material, group: ExpenseGroup) -> float:
    invoice_total = group.invoice_total
    expected = round(invoice_total, 2)
    amount = abs(payment.net_amount) if payment.amount is not None else None
    order_amount = payment.order_amount
    if amount is None and order_amount is None:
        return -100.0
    score = 0.0

    if expected > 0 and amount is not None:
        diff = abs(amount - expected)
        if diff <= 0.01:
            score += 60
        elif amount < expected and diff <= max(1.0, expected * 0.02):
            score += 42
        elif group.section == "taxi" and amount > expected and diff <= 100:
            score += 36
        elif group.section == "taxi" and amount > expected and diff <= 300:
            score += 15
        else:
            score -= 35
    elif order_amount is not None and expected > 0:
        diff = abs(order_amount - expected)
        if diff <= 0.01:
            score += 55
        elif order_amount > expected and group.section == "taxi" and diff <= 100:
            score += 30
        else:
            score -= 25

    diff_days = _date_diff(payment.business_date, group.sort_date)
    if diff_days == 0:
        score += 24
    elif diff_days == 1:
        score += 18
    elif diff_days <= 3:
        score += 12
    elif diff_days <= 7:
        score += 4
    else:
        score -= 18

    if group.invoice and payment.payee:
        ratio = match_merchant(payment.payee, group.invoice.seller)
        if ratio >= 0.55:
            score += 28
        elif ratio >= 0.35:
            score += 14
        elif group.section == _payment_category_hint(payment):
            score += 12
    hint = _payment_category_hint(payment)
    if hint and hint == group.section:
        score += 16
    if hint and hint != group.section:
        score -= 4

    if payment.order_no:
        for material in group.materials:
            if material.order_no and material.order_no == payment.order_no:
                score += 35
                break

    if group.payments:
        current = sum(p.net_amount for p in group.payments)
        if expected > 0 and abs(current - expected) <= 0.5:
            score -= 30
        else:
            score -= 4
    return score


def match_payments(groups: list[ExpenseGroup], payments: list[Material]) -> None:
    candidates: list[tuple[float, int, int, Material, ExpenseGroup]] = []
    for pi, payment in enumerate(payments):
        if payment.duplicate or not payment.include:
            continue
        for gi, group in enumerate(groups):
            if group.section == "notice" or not group.invoices:
                continue
            score = _payment_score(payment, group)
            if score >= 28:
                candidates.append((score, pi, gi, payment, group))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    assigned: set[str] = set()
    for score, _, _, payment, group in candidates:
        if payment.uid in assigned:
            continue
        if group.payments:
            # One payment is normally enough.  Allow a second only when the
            # group has a separate toll invoice and the two payments add up to
            # the group total (base fare + highway fee).
            if len(group.payments) >= 2 or not group.tolls:
                continue
            current = sum(p.net_amount for p in group.payments)
            proposed = round(current + payment.net_amount, 2)
            expected = group.invoice_total
            if not (proposed <= expected * 1.02 + 0.5 and abs(proposed - expected) <= max(2.0, expected * 0.05)):
                continue
        _add_material(group, payment)
        assigned.add(payment.uid)

    for payment in payments:
        if payment.uid not in assigned:
            payment.group_id = ""

    for group in groups:
        group.payments.sort(key=lambda p: (p.business_date or date.max, p.uid))


def sort_groups(groups: list[ExpenseGroup]) -> list[ExpenseGroup]:
    def key(group: ExpenseGroup) -> tuple:
        return (group.section_rank, group.sort_date, group.order, group.title)

    groups.sort(key=key)
    for section in SECTION_ORDER:
        index = 0
        for group in groups:
            if group.section != section:
                continue
            group.order = index
            index += 1
    groups.sort(key=key)
    return groups


def order_payments(payments: list[Material], groups: list[ExpenseGroup]) -> list[Material]:
    group_by_id = {g.gid: g for g in groups}
    group_order = {g.gid: (g.section_rank, g.sort_date, g.order) for g in groups}
    unmatched = (SECTION_ORDER["payment"], date.max, 0)

    def key(payment: Material) -> tuple:
        base = group_order.get(payment.group_id, unmatched)
        return (base, payment.business_date or date.max, natural_key(payment.name))

    return sorted(payments, key=key)


def _unique_payment_key(payment: Material) -> str:
    if payment.order_no:
        return "order:" + payment.order_no
    if payment.source_hash:
        return "hash:" + payment.source_hash
    return "id:" + payment.uid


def mark_duplicates(materials: list[Material]) -> None:
    seen_hash: dict[str, Material] = {}
    seen_invoice: dict[str, Material] = {}
    seen_payment: dict[str, Material] = {}
    for material in materials:
        if material.source_hash:
            original = seen_hash.get(material.source_hash)
            if original and original.uid != material.uid:
                material.duplicate = True
                material.duplicate_of = original.uid
                material.note = f"与 {original.name} 完全相同"
            else:
                seen_hash[material.source_hash] = material
        if material.role == "invoice" and material.invoice_no:
            original = seen_invoice.get(material.invoice_no)
            if original and original.uid != material.uid:
                material.duplicate = True
                material.duplicate_of = original.uid
                material.note = f"发票号码与 {original.name} 重复"
            else:
                seen_invoice[material.invoice_no] = material
        if material.role == "payment":
            key = _unique_payment_key(material)
            original = seen_payment.get(key)
            if original and original.uid != material.uid:
                material.duplicate = True
                material.duplicate_of = original.uid
                material.note = f"与 {original.name} 重复的支付记录"
            else:
                seen_payment[key] = material
    # A second pass compares payments that have no order number by amount/payee/time.
    unkeyed: list[Material] = []
    for material in materials:
        if material.role != "payment" or material.duplicate:
            continue
        if material.order_no:
            continue
        for original in unkeyed:
            if (
                _close(material.net_amount, original.net_amount, 0.01)
                and material.business_date == original.business_date
                and normalize_merchant(material.payee) == normalize_merchant(original.payee)
            ):
                material.duplicate = True
                material.duplicate_of = original.uid
                material.note = f"与 {original.name} 重复的支付记录"
                break
        if not material.duplicate:
            unkeyed.append(material)


def calculate_total(result: ScanResult) -> None:
    seen: set[str] = set()
    total = 0.0
    fallback_count = 0
    for group in result.groups:
        if not any(m.include for m in group.materials):
            continue
        if group.section == "notice":
            continue
        if group.has_payment:
            for payment in group.payments:
                if not payment.include or payment.duplicate:
                    continue
                key = _unique_payment_key(payment)
                if key in seen:
                    continue
                seen.add(key)
                total += payment.net_amount
        elif group.invoice_total > 0:
            fallback_count += 1
            total += group.invoice_total
    # Payments that could not be linked to an invoice still belong to this batch.
    for payment in result.ordered_payments:
        if payment.group_id or not payment.include or payment.duplicate:
            continue
        key = _unique_payment_key(payment)
        if key in seen:
            continue
        seen.add(key)
        total += payment.net_amount
    result.total = round(total, 2)
    result.fallback_count = fallback_count
    result.unmatched_payment_count = sum(
        1 for p in result.ordered_payments if not p.group_id and p.include and not p.duplicate
    )
    result.duplicate_count = sum(1 for m in result.materials if m.duplicate)
    if fallback_count:
        result.total_detail = f"{fallback_count} 个票据组没有支付记录，已按发票金额兜底"
    else:
        result.total_detail = "按实际支付金额合计"


def build_result(
    folder: Path,
    batch: BatchInfo,
    materials: list[Material],
    event_type: str,
) -> ScanResult:
    mark_duplicates(materials)
    groups = build_groups(materials)
    payments = [m for m in materials if m.role == "payment"]
    match_payments(groups, payments)
    sort_groups(groups)
    ordered_payments = order_payments(payments, groups)
    result = ScanResult(
        folder=folder,
        event_type=event_type,
        event_name=batch.name,
        date_start=batch.date_start,
        date_end=batch.date_end,
        materials=materials,
        groups=groups,
        ordered_payments=ordered_payments,
    )
    calculate_total(result)
    return result
