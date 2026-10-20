from __future__ import annotations

import re

from .models import Material
from .textutil import compact, normalize_for_match

PAYMENT_WORDS = (
    "账单详情",
    "交易详情",
    "交易成功",
    "支付成功",
    "付款成功",
    "自动扣款成功",
    "订单金额",
    "支付时间",
    "交易时间",
    "交易日期",
    "付款方式",
    "收款方全称",
    "已入账",
    "已退款",
)
INVOICE_WORDS = ("电子发票", "价税合计", "发票号码", "普通发票", "专用发票")
ITINERARY_WORDS = ("行程单", "高德地图", "amap", "行程时间", "上车时间", "行程人手机号")
NOTICE_WORDS = (
    "会议",
    "参会",
    "邀请",
    "录用",
    "通知",
    "议程",
    "论坛",
    "研讨",
    "conference",
    "meeting",
    "poster",
    "agenda",
    "invitation",
    "symposium",
)
TRAFFIC_WORDS = ("铁路", "火车", "航空", "机票", "航班", "客票", "12306", "登机牌", "高铁")
HOTEL_WORDS = ("住宿", "酒店", "宾馆", "旅馆", "民宿", "客房", "入住")
TAXI_WORDS = ("客运服务", "出租", "网约车", "打车", "出行", "高德", "滴滴", "曹操", "首汽", "行程")
TOLL_WORDS = ("通行费", "过路费", "高速费", "高速公路", "路桥费", "ETC")
PURCHASE_WORDS = ("采购", "商品", "货物", "电商", "订单", "办公用品", "设备", "材料", "耗材")
MEETING_FEE_WORDS = ("会议费", "注册费", "会务费", "会议注册")


def _text_blob(material: Material) -> str:
    return compact(material.text)


def _name_blob(material: Material) -> str:
    return compact(material.stem)


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(word.lower() in lower for word in words)


def _invoice_like(text: str) -> bool:
    return _has_any(text, INVOICE_WORDS) or "铁路电子客票" in text or "航空运输电子客票" in text


def classify(material: Material) -> Material:
    text = _text_blob(material)
    name = _name_blob(material)
    blob = text + "\n" + name
    suffix = material.suffix

    # Payment screenshots are usually tall and contain strong UI labels.
    if _has_any(text, PAYMENT_WORDS) and not _invoice_like(text) and not _has_any(text, ("行程单",)):
        material.section = "payment"
        material.role = "payment"
        material.subtype = "payment_refund" if _has_any(text, ("退款", "退票", "退货")) else "payment"
        material.method = material.method or "图片OCR"
        return material

    if _has_any(text, ITINERARY_WORDS) and not _invoice_like(text):
        material.section = "taxi"
        material.role = "itinerary"
        material.subtype = "taxi"
        return material

    if _invoice_like(text) or _has_any(name, ("发票", "电子发票", "铁路客票", "机票")):
        material.role = "invoice"
        if _has_any(blob, TOLL_WORDS):
            material.section = "taxi"
            material.subtype = "toll"
            material.role = "toll"
        elif _has_any(blob, HOTEL_WORDS):
            material.section = "hotel"
            material.subtype = "hotel"
        elif _has_any(blob, ("退票", "退票费")):
            material.section = "traffic"
            material.subtype = "refund"
        elif _has_any(blob, TRAFFIC_WORDS) and not _has_any(blob, ("客运服务", "出租", "网约车")):
            material.section = "traffic"
            if _has_any(blob, ("航空", "机票", "航班")):
                material.subtype = "flight"
            elif _has_any(blob, ("铁路", "火车", "12306", "高铁")):
                material.subtype = "train"
            else:
                material.subtype = "traffic_other"
        elif _has_any(blob, TAXI_WORDS):
            material.section = "taxi"
            material.subtype = "taxi"
        elif _has_any(blob, MEETING_FEE_WORDS):
            material.section = "other"
            material.subtype = "meeting_fee"
        elif _has_any(blob, ("停车", "停车费")):
            material.section = "other"
            material.subtype = "parking"
        elif _has_any(blob, PURCHASE_WORDS):
            material.section = "other"
            material.subtype = "purchase"
        else:
            material.section = "other"
            material.subtype = "other"
        return material

    # Email / meeting evidence: text-heavy, no invoice markers.
    email_markers = ("from:", "发件人", "收件人", "主题:", "subject:", "date:")
    if _has_any(blob, NOTICE_WORDS) and (
        _has_any(text, email_markers) or "录用" in text or "邀请" in text or "通知" in text
    ):
        material.section = "notice"
        material.role = "notice"
        material.subtype = "notice"
        return material

    # A plain image without recognizable content is probably a physical-item photo.
    if suffix in {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp", ".tif", ".tiff"}:
        if len(text) < 40 or _has_any(blob, ("实物", "照片", "开箱", "物品", "设备", "验收")):
            material.section = "other"
            material.role = "photo"
            material.subtype = "photo"
            return material

    # Text-heavy documents without invoice markers can still be notices.
    if _has_any(blob, NOTICE_WORDS) and len(text) >= 20:
        material.section = "notice"
        material.role = "notice"
        material.subtype = "notice"
        return material

    material.section = "other"
    material.role = "unknown"
    material.subtype = "other"
    return material


def infer_purchase(text: str) -> bool:
    return _has_any(compact(text), PURCHASE_WORDS)


def is_photo_candidate(material: Material) -> bool:
    return material.role == "photo" or (
        material.suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
        and len(compact(material.text)) < 40
    )


def normalize_merchant(value: str) -> str:
    return normalize_for_match(value)
