from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .docgen import build_document, default_filename, unique_output_path
from .models import ExpenseGroup, Material, Reminder, SECTION_ORDER, ScanResult
from .project_models import Asset, Project, Record, SLOT_LABELS, sanitize_filename


def _record_subtype(record: Record) -> str:
    if record.type == "traffic":
        blob = f"{record.title} {record.seller} {record.invoice_no}".lower()
        if any(word in blob for word in ("机票", "航空", "航班")):
            return "flight"
        if any(word in blob for word in ("火车", "铁路", "高铁", "12306")):
            return "train"
        return "traffic_other"
    if record.type == "hotel":
        return "hotel"
    if record.type == "taxi":
        return "taxi"
    if record.type == "purchase":
        return "purchase"
    if record.type == "conference":
        return "meeting_fee"
    return "other"


def _asset_material(
    project: Project,
    record: Record,
    asset: Asset,
    role: str,
    amount: float | None = None,
) -> Material:
    path = asset.path(project.folder)
    material = Material(path=path)
    material.section = record.section
    material.subtype = _record_subtype(record)
    material.role = role
    material.include = True
    material.amount = amount
    material.date = record.business_date
    material.invoice_date = record.business_date
    material.invoice_no = record.invoice_no if role == "invoice" else ""
    material.seller = record.seller if role in {"invoice", "toll"} else ""
    material.payee = record.seller if role == "payment" else ""
    material.confidence = "人工"
    material.method = "手动添加"
    material.note = record.note
    material.page_count = max(1, asset.page_count)
    return material


def to_scan_result(project: Project) -> ScanResult:
    project.sort_records()
    groups: list[ExpenseGroup] = []
    materials: list[Material] = []
    ordered_payments: list[Material] = []
    fallback_count = 0
    order = 0

    for record in project.records:
        subtype = _record_subtype(record)
        group = ExpenseGroup(
            gid=record.id,
            section=record.section,
            subtype=subtype,
            date=record.business_date,
            title=record.display_title,
            order=order,
        )
        order += 1
        first_invoice = True
        first_payment = True
        for key in ("invoice", "payment", "itinerary", "toll", "photo", "other"):
            assets = record.slot_assets(key)
            for asset in assets:
                if not asset.rel_path:
                    continue
                amount = None
                if key == "invoice":
                    amount = record.amount if first_invoice else 0.0
                    first_invoice = False
                elif key == "payment":
                    amount = record.payment_amount if first_payment else 0.0
                    first_payment = False
                role = key if key in {"invoice", "payment", "itinerary", "toll", "photo"} else "unknown"
                material = _asset_material(project, record, asset, role, amount)
                materials.append(material)
                group.materials.append(material)
                if role == "invoice" and group.invoice is None:
                    group.invoice = material
                elif role == "payment":
                    group.payments.append(material)
                    ordered_payments.append(material)
                elif role == "itinerary":
                    group.itineraries.append(material)
                elif role == "toll":
                    group.tolls.append(material)
                elif role == "photo":
                    group.photos.append(material)
        if group.materials:
            groups.append(group)
        if record.uses_invoice_fallback:
            fallback_count += 1

        # Conference acceptance notices are a separate section in the Word file.
        notice_assets = record.slot_assets("notice_zh") + record.slot_assets("notice_en")
        if notice_assets:
            notice_group = ExpenseGroup(
                gid=f"{record.id}_notice",
                section="notice",
                subtype="notice",
                date=record.business_date,
                title=f"{record.display_title} 录用通知",
                order=order,
            )
            order += 1
            for asset in notice_assets:
                if not asset.rel_path:
                    continue
                material = _asset_material(project, record, asset, "notice")
                material.section = "notice"
                materials.append(material)
                notice_group.materials.append(material)
                notice_group.notices.append(material)
            if notice_group.materials:
                groups.append(notice_group)

    groups.sort(key=lambda g: (SECTION_ORDER.get(g.section, 99), g.date or date.max, g.order))

    reminders = [
        Reminder(level=r.level, code=r.code, message=r.message, gid=r.record_id, uid="")
        for r in project.missing_reminders()
    ]
    result = ScanResult(
        folder=project.folder,
        event_type=project.type,
        event_name=project.name,
        date_start=_parse_date(project.date_start),
        date_end=_parse_date(project.date_end),
        materials=materials,
        groups=groups,
        reminders=reminders,
        ordered_payments=ordered_payments,
        total=project.total(),
        fallback_count=fallback_count,
        unmatched_payment_count=0,
        duplicate_count=0,
    )
    result.total_detail = "按支付金额合计" if fallback_count == 0 else f"{fallback_count} 条记录按发票金额兜底"
    return result


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def project_filename(project: Project) -> str:
    name = sanitize_filename(project.name, "报销项目")
    start = (project.date_start or "").replace("-", "")
    end = (project.date_end or "").replace("-", "")
    date_part = start
    if start and end and start != end:
        date_part = f"{start}-{end[-4:]}"
    parts = ["报销材料", name]
    if date_part:
        parts.append(date_part)
    parts.append(f"合计{project.total():,.2f}元")
    return "_".join(parts) + ".docx"


def build_project_document(project: Project, output_path: Path | None = None, *, save_metadata: bool = True) -> Path:
    project.ensure_dirs()
    result = to_scan_result(project)
    path = output_path or (project.output_dir / project_filename(project))
    path = unique_output_path(path)
    generated = build_document(result, path)
    if save_metadata:
        try:
            rel = generated.relative_to(project.folder).as_posix()
        except ValueError:
            rel = str(generated)
        if rel not in project.outputs:
            project.outputs.append(rel)
        project.save()
    return generated
