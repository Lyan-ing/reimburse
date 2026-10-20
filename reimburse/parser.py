from __future__ import annotations

import re
from datetime import date

from .models import Material
from .textutil import (
    all_dates,
    chinese_amount_to_float,
    clean_company,
    compact,
    date_near,
    digit_fix,
    extract_buyer,
    extract_invoice_no,
    extract_order_no,
    extract_payee,
    extract_seller,
    money_to_float,
    similarity,
)


def _first_amount(patterns: list[str], text: str) -> float | None:
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = money_to_float(match.group(1))
            if value is not None:
                return value
    return None


def _max_amount(patterns: list[str], text: str) -> float | None:
    best: float | None = None
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = money_to_float(match.group(1))
            if value is not None and (best is None or value > best):
                best = value
    return best


def extract_invoice_amount(text: str) -> float | None:
    text = compact(text)
    high_confidence = [
        r"价税合计\(小写\)[:：]?¥?([0-9][0-9,.]{0,14})",
        r"价税合计.*?\(小写\).*?¥?([0-9][0-9,.]{0,14})",
        r"\(小写\)[:：]?¥?([0-9][0-9,.]{0,14})",
        r"小写[:：]?¥?([0-9][0-9,.]{0,14})",
        r"票价[:：]?¥?([0-9][0-9,.]{0,14})",
    ]
    value = _max_amount(high_confidence, text)
    if value is not None:
        return value
    # OCR often loses the numeric total but keeps the Chinese uppercase total.
    value = chinese_amount_to_float(text)
    if value is not None:
        return value
    return _max_amount(
        [
            r"价税合计.*?¥([0-9][0-9,.]{0,14})",
            r"合计[:：]?¥?([0-9][0-9,.]{0,14})",
            r"¥([0-9][0-9,.]{0,14})",
            r"([0-9][0-9,.]{0,14})元",
        ],
        text,
    )


def _business_date_for_invoice(material: Material, text: str) -> date | None:
    invoice_date = date_near(text, ("开票日期", "开票时间"))
    if material.section == "traffic":
        if material.subtype == "train":
            # 2026年06月11日10:49开
            for index, parsed in all_dates(text):
                tail = text[index : index + 40]
                if "开" in tail or "乘车" in tail or "出发" in tail:
                    return parsed
        if material.subtype == "flight":
            for label in ("乘机日期", "航班日期", "出发日期", "起飞日期"):
                value = date_near(text, (label,))
                if value:
                    return value
        if material.subtype == "refund":
            return invoice_date
    if material.section == "hotel":
        value = date_near(text, ("入住",), window=32, after=False)
        if value:
            return value
        value = date_near(text, ("入住日期", "入住时间"))
        if value:
            return value
    if material.section == "taxi":
        for label in ("出行日期", "行程日期", "乘车时间", "上车时间"):
            value = date_near(text, (label,))
            if value:
                return value
    return invoice_date


def _extract_dates_for_payment(text: str) -> date | None:
    for label in ("支付时间", "交易日期", "交易时间", "付款时间", "乘车时间", "创建时间"):
        value = date_near(text, (label,))
        if value:
            return value
    dates = all_dates(text)
    if dates:
        # Usually the most recent date in a payment screenshot is the transaction date.
        return dates[-1][1]
    return None


def _extract_payment_amounts(text: str) -> tuple[float | None, float | None, float | None]:
    text = compact(text)
    # The Windows OCR engine often reads the leading minus + 7 of a large
    # amount as "刁" (for example "刁7.03" for "-77.03").
    text = text.replace("刁", "-7")
    order_amount = None
    for pattern in (
        r"订单金额[:：]?¥?([0-9SBOIlZ][0-9SBOIlZ,.]{0,14})",
        r"订单金额[:：]?(-?[0-9SBOIlZ][0-9SBOIlZ,.]{0,14})",
    ):
        match = re.search(pattern, text)
        if match:
            order_amount = money_to_float(match.group(1))
            break

    actual = None
    for pattern in (
        r"(?:实付|实际支付|支付金额|付款金额)[:：]?¥?(-?[0-9SBOIlZ][0-9SBOIlZ,.]{0,14})",
        r"交易金额[:：]?¥?(-?[0-9SBOIlZ][0-9SBOIlZ,.]{0,14})",
    ):
        match = re.search(pattern, text)
        if match:
            actual = money_to_float(match.group(1))
            break

    if actual is None:
        negative_values: list[float] = []
        for match in re.finditer(r"-([0-9SBOIlZ][0-9SBOIlZ,.]{0,14}\.[0-9]{2})", text):
            value = money_to_float(match.group(1))
            if value is not None and value >= 1:
                negative_values.append(-value)
        if negative_values:
            actual = min(negative_values)

    if actual is None:
        values = []
        for match in re.finditer(r"¥\s*([0-9SBOIlZ][0-9SBOIlZ,.]{0,14})", text):
            value = money_to_float(match.group(1))
            if value is not None:
                values.append(value)
        if values:
            actual = max(values)

    refund = None
    for pattern in (
        r"已退款[:：]?\(?¥?([0-9][0-9,.]{0,14})\)?",
        r"退款金额[:：]?¥?([0-9][0-9,.]{0,14})",
        r"退款[:：]?¥?([0-9][0-9,.]{0,14})",
    ):
        match = re.search(pattern, text)
        if match:
            refund = money_to_float(match.group(1))
            break

    if actual is None and order_amount is not None:
        actual = order_amount
    if actual is not None:
        # Low-resolution screenshots often lose the decimal point ("8Z00" for
        # 87.00).  Ride-hailing payments are small, so a four-digit token with
        # a taxi merchant is almost certainly cents rather than yuan.
        taxi_hint = any(word in text for word in ("高德", "出行", "打车", "滴滴", "出租", "客运"))
        if taxi_hint and actual >= 1000 and actual < 100000 and float(actual).is_integer():
            actual = round(actual / 100, 2)
        # A payment screenshot normally shows "-633.55", which is an outgoing
        # payment.  Only a standalone refund should reduce the total.
        if refund is not None and actual < 0:
            actual = abs(actual)
        elif "退款成功" in text and "支付时间" not in text and "付款时间" not in text:
            actual = -abs(actual)
        else:
            actual = abs(actual)
    if refund is not None and actual is not None:
        # "已退款(54839)" is a lost decimal point; a refund cannot exceed the
        # original payment.
        if refund > abs(actual):
            if round(refund / 100, 2) <= abs(actual):
                refund = round(refund / 100, 2)
            else:
                refund = None
    return actual, order_amount, refund


def _extract_itinerary_amount(text: str) -> float | None:
    text = compact(text)
    patterns = [
        r"合计([0-9][0-9,.]{0,14})元",
        r"共计[0-9一二三四五六七八九十]+单行程[,，]?合计([0-9][0-9,.]{0,14})元",
        r"订单金额[:：]?¥?([0-9][0-9,.]{0,14})",
        r"金额[:：]?¥?([0-9][0-9,.]{0,14})",
        r"¥([0-9][0-9,.]{0,14})",
    ]
    value = _first_amount(patterns, text)
    if value is not None:
        return value
    # Last column of the itinerary table is usually the total.
    values = [money_to_float(m.group(1)) for m in re.finditer(r"([0-9][0-9,.]{0,14})元", text)]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _extract_itinerary_date(text: str) -> date | None:
    for label in ("行程时间", "上车时间", "出发时间", "用车时间"):
        value = date_near(text, (label,))
        if value:
            return value
    dates = all_dates(text)
    return dates[0][1] if dates else None


def _extract_payee_from_text(text: str) -> str:
    value = extract_payee(text)
    if value:
        return value
    match = re.search(r"商品说明[:：]?(.{2,40}?)(?=收单机构|清算机构|收款方|订单号|$)", compact(text))
    if match:
        value = clean_company(match.group(1))
        if value:
            return value
    return ""


def _confidence_for_invoice(material: Material) -> str:
    score = 0
    if material.invoice_no:
        score += 2
    if material.amount:
        score += 2
    if material.business_date:
        score += 1
    if material.seller:
        score += 1
    if score >= 5:
        return "高"
    if score >= 3:
        return "中"
    return "低"


def _confidence_for_payment(material: Material) -> str:
    score = 0
    if material.amount is not None:
        score += 2
    if material.business_date:
        score += 1
    if material.payee:
        score += 1
    if material.order_no:
        score += 1
    if score >= 4:
        return "高"
    if score >= 2:
        return "中"
    return "低"


def parse_material(material: Material) -> Material:
    text = compact(material.text)
    name = compact(material.stem)
    blob = text + "\n" + name

    if material.role in {"invoice", "toll"}:
        material.amount = extract_invoice_amount(text)
        if material.amount is None:
            material.amount = extract_invoice_amount(name)
        material.invoice_no = extract_invoice_no(text) or extract_invoice_no(name)
        material.invoice_date = date_near(text, ("开票日期", "开票时间"))
        material.date = _business_date_for_invoice(material, text) or material.invoice_date
        material.seller = extract_seller(text)
        material.buyer = extract_buyer(text)
        if not material.seller:
            material.seller = extract_payee(text)
        # A hotel invoice's remark often contains check-in/check-out dates.
        if material.section == "hotel":
            for label in ("入住", "离店"):
                if label in text:
                    value = date_near(text, (label,), window=20, after=False)
                    if value and not material.date:
                        material.date = value
        material.confidence = _confidence_for_invoice(material)
        if material.amount is None:
            material.note = "未识别到金额"
        return material

    if material.role == "itinerary":
        material.amount = _extract_itinerary_amount(text)
        material.date = _extract_itinerary_date(text)
        material.order_no = extract_order_no(text)
        material.payee = _extract_payee_from_text(text)
        material.confidence = "中" if material.amount else "低"
        if material.amount is None:
            material.note = "未识别到行程合计"
        return material

    if material.role == "payment":
        actual, order_amount, refund = _extract_payment_amounts(text)
        material.amount = actual
        material.order_amount = order_amount
        material.refund_amount = refund
        material.date = _extract_dates_for_payment(text)
        material.payee = _extract_payee_from_text(text)
        material.order_no = extract_order_no(text)
        material.confidence = _confidence_for_payment(material)
        if material.amount is None:
            material.note = "未识别到支付金额"
        elif material.refund_amount:
            material.note = f"含退款 ¥{material.refund_amount:,.2f}"
        return material

    if material.role == "notice":
        material.date = date_near(text, ("Date:", "日期:", "时间:", "会议时间", "会议日期"))
        material.confidence = "中" if material.date else "低"
        return material

    if material.role == "photo":
        material.date = _date_from_filename(material.name)
        if material.date is None and material.mtime:
            from datetime import datetime

            try:
                material.date = datetime.fromtimestamp(material.mtime).date()
            except (OSError, OverflowError, ValueError):
                pass
        material.confidence = "中"
        return material

    # Unknown files still get a best-effort date/amount for the review table.
    material.date = _date_from_filename(material.name) or first_date(text)
    if material.amount is None:
        material.amount = extract_invoice_amount(text)
    return material


def first_date(text: str) -> date | None:
    values = all_dates(text)
    return values[0][1] if values else None


def _date_from_filename(name: str) -> date | None:
    match = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", name)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", name)
    if match:
        try:
            return date(2000 + int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def match_merchant(left: str, right: str) -> float:
    return similarity(left, right)
