from __future__ import annotations

from datetime import date

from .models import Material, Reminder, ScanResult
from .textutil import compact, format_money


def _add(reminders: list[Reminder], level: str, code: str, message: str, gid: str = "", uid: str = "", detail: str = "") -> None:
    if any(r.code == code and r.gid == gid and r.uid == uid and r.message == message for r in reminders):
        return
    reminders.append(Reminder(level=level, code=code, message=message, gid=gid, uid=uid, detail=detail))


def _trip_range(result: ScanResult) -> tuple[date | None, date | None]:
    start, end = result.date_start, result.date_end
    if start and end:
        return start, end
    dates = [m.business_date for m in result.materials if m.business_date]
    if not dates:
        return start, end
    return min(dates), max(dates)


def build_reminders(result: ScanResult) -> list[Reminder]:
    reminders: list[Reminder] = []
    groups = result.groups
    event_type = result.event_type

    for group in groups:
        if not any(m.include for m in group.materials):
            continue
        name = group.short_title(36)
        if group.section != "notice":
            if group.has_invoice and not group.has_payment:
                _add(
                    reminders,
                    "warn",
                    "missing_payment",
                    f"缺支付记录：{name}（¥{format_money(group.invoice_total)}）",
                    gid=group.gid,
                )
            if group.has_payment and not group.has_invoice:
                _add(
                    reminders,
                    "error",
                    "missing_invoice",
                    f"缺发票：{name}（支付 ¥{format_money(group.payment_total)}）",
                    gid=group.gid,
                )
        if group.section == "taxi":
            if group.has_invoice and not group.itineraries:
                _add(reminders, "warn", "missing_itinerary", f"缺行程单：{name}", gid=group.gid)
            if group.has_invoice and not group.tolls and group.has_payment:
                extra = round(group.payment_total - group.invoice_total, 2)
                if extra > 1.0:
                    _add(
                        reminders,
                        "warn",
                        "possible_toll",
                        f"支付高于发票 {format_money(extra)} 元：{name}，可能含高速费或纸质票据",
                        gid=group.gid,
                    )
            if group.tolls and not group.has_payment:
                _add(reminders, "warn", "toll_missing_payment", f"高速费缺少支付记录：{name}", gid=group.gid)
        if group.section == "other" and group.subtype == "purchase" and group.invoice:
            if (group.invoice.amount or 0) > 500 and not group.photos:
                _add(
                    reminders,
                    "error",
                    "missing_item_photo",
                    f"采购金额超过 500 元，缺实物照片：{name}",
                    gid=group.gid,
                )
        if group.has_invoice and group.invoice_total <= 0:
            _add(reminders, "warn", "missing_invoice_amount", f"未识别到发票金额：{name}", gid=group.gid)
        if group.has_payment and group.has_invoice:
            invoice_total = group.invoice_total
            payment_total = group.payment_total
            diff = round(invoice_total - payment_total, 2)
            if diff > max(1.0, invoice_total * 0.02):
                _add(
                    reminders,
                    "warn",
                    "payment_lower",
                    f"支付低于发票 {format_money(diff)} 元：{name}，请确认是否有优惠或未附凭证",
                    gid=group.gid,
                )
            elif 0 < diff <= max(1.0, invoice_total * 0.02):
                _add(
                    reminders,
                    "info",
                    "payment_discount",
                    f"支付比发票少 {format_money(diff)} 元：{name}，按支付金额计入",
                    gid=group.gid,
                )

    for material in result.materials:
        if material.duplicate:
            _add(
                reminders,
                "warn",
                "duplicate",
                f"重复材料：{material.name}" + (f"（{material.note}）" if material.note else ""),
                uid=material.uid,
            )
        if not material.include:
            continue
        if material.role == "payment" and not material.group_id and not material.duplicate:
            _add(
                reminders,
                "error",
                "unmatched_payment",
                f"支付记录未匹配到发票：{material.name}（¥{format_money(abs(material.net_amount))}）",
                uid=material.uid,
            )
        if material.role == "unknown":
            _add(reminders, "warn", "unknown_material", f"未识别材料：{material.name}", uid=material.uid)
        if material.error and material.error not in {"", "文件无法访问"}:
            _add(
                reminders,
                "warn",
                "extract_error",
                f"无法读取内容：{material.name}（{material.error}）",
                uid=material.uid,
            )

    start, end = _trip_range(result)
    if event_type == "参会":
        if not any(g.section == "notice" for g in groups):
            _add(reminders, "error", "missing_notice", "缺少会议通知、录用通知或邀请函")
    if event_type in {"出差", "参会"}:
        traffic_count = sum(1 for g in groups if g.section == "traffic" and g.has_invoice)
        if traffic_count == 0:
            _add(reminders, "error", "missing_traffic", "未发现机票或火车票等交通票据")
        elif traffic_count == 1:
            _add(reminders, "warn", "single_traffic", "只有一张交通票据，请确认是否缺少返程票")
        if start and end and (end - start).days >= 1:
            has_hotel = any(g.section == "hotel" and (g.has_invoice or g.has_payment) for g in groups)
            if not has_hotel:
                _add(reminders, "warn", "missing_hotel", "行程跨夜但未发现住宿票据，请确认是否需要住宿")
    if event_type == "采购":
        if not any(g.section == "other" and g.subtype == "purchase" for g in groups):
            _add(reminders, "error", "missing_purchase_invoice", "未发现采购发票")

    if start and end:
        for material in result.materials:
            if material.role == "notice" or not material.business_date:
                continue
            if material.business_date < start or material.business_date > end:
                # Invoices may be issued after the trip, so only flag clearly out-of-period
                # business dates.
                if material.business_date > end:
                    days = (material.business_date - end).days
                else:
                    days = (start - material.business_date).days
                if days > 7:
                    _add(
                        reminders,
                        "warn",
                        "out_of_period",
                        f"日期可能不属于本次事项：{material.name}（{material.business_date.isoformat()}）",
                        uid=material.uid,
                    )

    if result.fallback_count:
        _add(
            reminders,
            "info",
            "total_fallback",
            f"{result.fallback_count} 个票据组缺支付记录，总额暂按发票金额兜底",
        )
    if result.unmatched_payment_count:
        _add(
            reminders,
            "warn",
            "unmatched_payment_summary",
            f"{result.unmatched_payment_count} 笔支付未匹配到发票，已按支付金额计入总额",
        )
    if result.total == 0 and result.materials:
        _add(reminders, "error", "zero_total", "总金额为 0，请检查金额识别或补充支付记录")

    order = {"error": 0, "warn": 1, "info": 2}
    reminders.sort(key=lambda r: (order.get(r.level, 9), r.code, r.message))
    return reminders


def reminder_text(reminders: list[Reminder]) -> str:
    if not reminders:
        return "未发现明显缺失材料。"
    lines = []
    labels = {"error": "必须补充", "warn": "建议核对", "info": "提示"}
    for item in reminders:
        lines.append(f"[{labels.get(item.level, '提示')}] {item.message}")
    return "\n".join(lines)


def is_blocking(reminders: list[Reminder]) -> bool:
    return any(r.level == "error" for r in reminders)
