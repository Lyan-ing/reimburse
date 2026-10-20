from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher


def compact(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", "", text)
    text = (
        text.replace("，", ",")
        .replace("。", ".")
        .replace("·", ".")
        .replace("．", ".")
        .replace("：", ":")
        .replace("（", "(")
        .replace("）", ")")
        .replace("￥", "¥")
        .replace("／", "/")
    )
    # Common OCR confusions in Chinese payment screenshots and invoices.
    text = text.replace("订单全额", "订单金额").replace("讠丁", "订").replace("失路", "铁路")
    text = text.replace("交成功", "交易成功").replace("收 款 方 全 称", "收款方全称")
    text = text.replace("兀", "元").replace("℃", ".").replace("〇", "0")
    text = re.sub(r"(?<![\dA-Za-z])[一—–−](?=\d)", "-", text)
    text = re.sub(r"(?<=\d)[—–一~](?=\d)", "-", text)
    text = re.sub(r"(?<=\d)[Oo](?=\d)", "0", text)
    return text


def normalize_for_match(text: str) -> str:
    text = compact(text)
    return re.sub(
        r"(有限责任公司|股份有限公司|有限公司|公司|分公司|集团|支付宝-|微信支付-|\(消费\)|消费|商户)",
        "",
        text,
    )


def digit_fix(token: str) -> str:
    table = str.maketrans(
        {
            "O": "0",
            "o": "0",
            "D": "0",
            "I": "1",
            "l": "1",
            "|": "1",
            "S": "5",
            "B": "8",
            "Z": "7",
            "z": "2",
            "g": "9",
            "q": "9",
        }
    )
    return token.translate(table)


def clean_company(value: str) -> str:
    value = value.strip(" :：,，;；()（）[]【】")
    value = re.sub(r"(纳税人识别号|统一社会信用代码|地址|开户行|电话|银行账号).*$", "", value)
    value = value.strip(" :：,，;；")
    if len(value) < 2:
        return ""
    if value in {"名称", "信息", "购买方", "销售方"}:
        return ""
    return value[:60]


def money_to_float(token: str) -> float | None:
    if not token:
        return None
    token = token.strip().replace(" ", "")
    token = token.replace("¥", "").replace("￥", "").replace("元", "")
    token = token.replace("·", ".").replace("．", ".")
    token = digit_fix(token)
    token = re.sub(r"[^\d,.\-]", "", token)
    if not token or token in {"-", ".", ","}:
        return None
    negative = token.startswith("-")
    token = token.lstrip("-")
    if "," in token and "." in token:
        token = token.replace(",", "")
    elif "," in token:
        parts = token.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            token = parts[0] + "." + parts[1]
        else:
            token = "".join(parts)
    if token.count(".") > 1:
        head, _, tail = token.rpartition(".")
        token = head.replace(".", "") + "." + tail
    try:
        value = float(token)
    except ValueError:
        return None
    if value <= 0 or value > 10_000_000:
        return None
    return round(-value if negative else value, 2)


def money_tokens(text: str) -> list[float]:
    values: list[float] = []
    for match in re.finditer(r"¥?\s*(-?[0-9][0-9,.]{0,14})\s*(?:元)?", text):
        raw = match.group(1)
        if re.fullmatch(r"20\d{2}", raw) or re.fullmatch(r"20\d{6}", raw):
            continue
        value = money_to_float(raw)
        if value is not None:
            values.append(value)
    return values


def _parse_date_parts(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def all_dates(text: str) -> list[tuple[int, date]]:
    text = compact(text)
    found: list[tuple[int, date]] = []
    seen: set[tuple[int, date]] = set()
    patterns = [
        r"(20\d{2})[年\-/.](\d{1,2})[月\-/.](\d{1,2})日?",
        r"(20\d{2})(\d{2})(\d{2})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            parsed = _parse_date_parts(match.group(1), match.group(2), match.group(3))
            if parsed and (match.start(), parsed) not in seen:
                seen.add((match.start(), parsed))
                found.append((match.start(), parsed))
    found.sort(key=lambda item: item[0])
    return found


def date_near(text: str, labels: tuple[str, ...], window: int = 45, after: bool = True) -> date | None:
    text = compact(text)
    best: date | None = None
    for label in labels:
        start = 0
        while True:
            index = text.find(label, start)
            if index < 0:
                break
            start = index + len(label)
            if after:
                segment = text[index : index + window]
            else:
                segment = text[max(0, index - window) : index + len(label)]
            dates = all_dates(segment)
            if dates:
                return dates[0][1]
    return best


def first_date(text: str) -> date | None:
    dates = all_dates(text)
    return dates[0][1] if dates else None


def extract_amount_by_patterns(text: str, patterns: list[str]) -> float | None:
    text = compact(text)
    for pattern in patterns:
        matches = list(re.finditer(pattern, text))
        values = [money_to_float(m.group(1)) for m in matches]
        values = [v for v in values if v is not None]
        if values:
            return max(values)
    return None


def extract_invoice_no(text: str) -> str:
    text = compact(text)
    patterns = [
        r"发票号码:?([0-9OoDIl|SBZgq]{8,24})",
        r"发票号:?([0-9OoDIl|SBZgq]{8,24})",
        r"票据号码:?([0-9OoDIl|SBZgq]{8,24})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            token = digit_fix(match.group(1))
            digits = re.sub(r"\D", "", token)
            if 8 <= len(digits) <= 24:
                return digits
    if "发票" in text:
        for match in re.finditer(r"(?<!\d)(\d{20})(?!\d)", text):
            return match.group(1)
    return ""


def extract_seller(text: str) -> str:
    text = compact(text)
    patterns = [
        r"销售方名称:?(.{2,50}?)(?=纳税人|统一社会|地址|开户行|电话|银行账号|购买方|购方名称|买方名称|$)",
        r"销售方:?(.{2,50}?)(?=纳税人|统一社会|地址|开户行|电话|银行账号|购买方|购方名称|买方名称|$)",
        r"销方名称:?(.{2,50}?)(?=纳税人|统一社会|地址|开户行|电话|银行账号|购买方|购方名称|买方名称|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = clean_company(match.group(1))
            if value:
                return value
    return ""


def extract_buyer(text: str) -> str:
    text = compact(text)
    patterns = [
        r"购买方名称:?(.{2,50}?)(?=纳税人|统一社会|地址|开户行|电话|银行账号|$)",
        r"购方名称:?(.{2,50}?)(?=纳税人|统一社会|地址|开户行|电话|银行账号|$)",
        r"买方名称:?(.{2,50}?)(?=纳税人|统一社会|地址|开户行|电话|银行账号|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = clean_company(match.group(1))
            if value:
                return value
    return ""


def extract_payee(text: str) -> str:
    text = compact(text)
    patterns = [
        r"收款方全称:?(.{2,50}?)(?=订单号|商家订单号|收单机构|清算机构|$)",
        r"收款方:?(.{2,50}?)(?=订单号|商家订单号|$)",
        r"支付宝-(.{2,50}?)(?=¥|交易|订单|$)",
        r"微信支付-(.{2,50}?)(?=¥|交易|订单|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = clean_company(match.group(1))
            if value:
                return value
    return ""


def extract_order_no(text: str) -> str:
    text = compact(text)
    patterns = [
        r"(?<!商家)订单号[：:.]?([0-9A-Za-z]{10,40})",
        r"交易单号[：:.]?([0-9A-Za-z]{10,40})",
        r"商户订单号[：:.]?([0-9A-Za-z]{10,40})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            token = match.group(1)
            if not token.startswith(("M202", "0003N")):
                return token
    return ""


def similarity(left: str, right: str) -> float:
    a = normalize_for_match(left)
    b = normalize_for_match(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class AmountCandidate:
    value: float
    label: str
    priority: int
    position: int


def amount_candidates(text: str) -> list[AmountCandidate]:
    text = compact(text)
    labels = [
        ("价税合计", 100),
        ("小写", 95),
        ("票价", 90),
        ("实付", 88),
        ("实际支付", 88),
        ("交易金额", 84),
        ("订单金额", 80),
        ("合计", 70),
        ("金额", 50),
        ("¥", 40),
    ]
    found: list[AmountCandidate] = []
    seen: set[tuple[float, int]] = set()
    for label, priority in labels:
        for match in re.finditer(re.escape(label), text):
            segment = text[match.end() : match.end() + 45]
            money = re.search(r"¥?\s*(-?[0-9][0-9,.]{0,14})", segment)
            if not money:
                continue
            value = money_to_float(money.group(1))
            if value is None:
                continue
            key = (abs(value), match.start())
            if key in seen:
                continue
            seen.add(key)
            found.append(AmountCandidate(abs(value), label, priority, match.start()))
    return found


CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "壹": 1,
    "贰": 2,
    "叁": 3,
    "肆": 4,
    "伍": 5,
    "陆": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
    "两": 2,
}
CN_UNITS = {"拾": 10, "佰": 100, "仟": 1000}
CN_LARGE = {"万": 10000, "亿": 100000000}


def chinese_amount_to_float(text: str) -> float | None:
    text = compact(text)
    if not any(char in text for char in CN_DIGITS):
        return None
    # Prefer the segment after the uppercase amount label, but OCR often puts
    # the Chinese amount before the label, so fall back to the whole text.
    candidates: list[str] = []
    for label in ("价税合计(大写)", "价税合计大写", "(大写)", "大写"):
        index = text.find(label)
        if index >= 0:
            candidates.append(text[index + len(label) :])
            break
    candidates.append(text)
    segment = ""
    for candidate in candidates:
        match = re.search(r"([零〇壹贰叁肆伍陆柒捌玖两拾佰仟万亿圆元角分整]{2,30})", candidate)
        if match and ("圆" in match.group(1) or "元" in match.group(1)):
            segment = match.group(1).replace("人民币", "")
            break
    if not segment:
        return None
    integer_text, decimal_text = segment, ""
    for separator in ("圆", "元"):
        if separator in integer_text:
            integer_text, decimal_text = integer_text.split(separator, 1)
            break
    else:
        # No yuan marker: only accept a plausible integer amount with units.
        if not any(unit in segment for unit in ("拾", "佰", "仟", "万")):
            return None
        integer_text, decimal_text = segment, ""
    total = 0
    section = 0
    number = 0
    for char in integer_text:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
        elif char in CN_UNITS:
            if number == 0:
                number = 1
            section += number * CN_UNITS[char]
            number = 0
        elif char in CN_LARGE:
            section = (section + number) * CN_LARGE[char]
            total += section
            section = 0
            number = 0
    integer_value = total + section + number
    if integer_value <= 0:
        return None
    decimal_value = 0.0
    decimal_text = decimal_text.replace("整", "")
    if "角" in decimal_text:
        jiao, decimal_text = decimal_text.split("角", 1)
        if jiao and jiao[-1] in CN_DIGITS:
            decimal_value += CN_DIGITS[jiao[-1]] * 0.1
    if "分" in decimal_text:
        fen, _ = decimal_text.split("分", 1)
        if fen and fen[-1] in CN_DIGITS:
            decimal_value += CN_DIGITS[fen[-1]] * 0.01
    value = round(integer_value + decimal_value, 2)
    if value <= 0 or value > 10_000_000:
        return None
    return value


def format_money(value: float) -> str:
    return f"{value:,.2f}"
