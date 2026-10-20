from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SKIP_SUFFIXES = {
    ".tmp",
    ".temp",
    ".bak",
    ".lnk",
    ".db",
    ".ini",
    ".sys",
    ".dll",
    ".exe",
    ".msi",
}
SKIP_NAMES = {
    "thumbs.db",
    "desktop.ini",
    ".ds_store",
}
GENERATED_PREFIXES = (
    "报销材料_",
    "报销材料汇总",
)


@dataclass
class BatchInfo:
    name: str
    date_start: date | None
    date_end: date | None
    event_type: str


def natural_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def is_candidate(path: Path, output_dir: Path | None = None) -> bool:
    name = path.name
    lower = name.lower()
    if not path.is_file():
        return False
    if lower in SKIP_NAMES or lower.startswith("~$") or lower.startswith("."):
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if lower.startswith(tuple(p.lower() for p in GENERATED_PREFIXES)) and path.suffix.lower() == ".docx":
        return False
    if output_dir is not None:
        try:
            if path.parent == output_dir and lower.startswith("报销材料_"):
                return False
        except Exception:
            pass
    return True


def scan_folder(folder: Path, recursive: bool = True, output_dir: Path | None = None) -> list[Path]:
    folder = folder.resolve()
    if not folder.exists():
        return []
    iterator = folder.rglob("*") if recursive else folder.iterdir()
    files: list[Path] = []
    for path in iterator:
        if not is_candidate(path, output_dir):
            continue
        if any(part.startswith(".") or part in {"__pycache__", "node_modules"} for part in path.parts):
            continue
        files.append(path)
    files.sort(key=lambda p: natural_key(str(p.relative_to(folder))))
    return files


def sha1_file(path: Path, limit_mb: int = 80) -> str:
    try:
        if path.stat().st_size > limit_mb * 1024 * 1024:
            return ""
    except OSError:
        return ""
    digest = hashlib.sha1()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _parse_date_token(token: str) -> date | None:
    token = re.sub(r"\D", "", token)
    if len(token) == 8:
        try:
            return date(int(token[:4]), int(token[4:6]), int(token[6:8]))
        except ValueError:
            return None
    if len(token) == 6:
        try:
            return date(2000 + int(token[:2]), int(token[2:4]), int(token[4:6]))
        except ValueError:
            return None
    return None


def parse_batch_info(folder: Path) -> BatchInfo:
    name = folder.name
    start = end = None
    match = re.search(r"(\d{6,8})\s*[-—–~至]\s*(\d{2,8})", name)
    if match:
        start = _parse_date_token(match.group(1))
        raw_end = match.group(2)
        if len(raw_end) == 4 and start:
            end = _parse_date_token(f"{start.year}{raw_end}")
        else:
            end = _parse_date_token(raw_end)
        if start and end and end < start:
            end = date(start.year + 1, end.month, end.day)
    if start is None:
        matches = re.findall(r"(20\d{2})[年\-/.](\d{1,2})[年\-/.](\d{1,2})", name)
        if matches:
            try:
                start = date(int(matches[0][0]), int(matches[0][1]), int(matches[0][2]))
            except ValueError:
                start = None
    event_name = name
    event_name = re.sub(r"\d{6,8}\s*[-—–~至]\s*\d{2,8}", "", event_name)
    event_name = re.sub(r"^(0\d|1\d|2\d)[\-_ ]*", "", event_name)
    event_name = event_name.strip(" _-—–")
    if not event_name:
        event_name = name
    lower = name.lower()
    if any(word in name for word in ["会议", "论坛", "大会", "研讨", "参会", "培训", "学术"]) or any(
        word in lower for word in ["conference", "meeting", "symposium", "workshop", "cie", "radar"]
    ):
        event_type = "参会"
    elif any(word in name for word in ["采购", "购买", "订单", "电商"]):
        event_type = "采购"
    else:
        event_type = "出差"
    return BatchInfo(name=event_name, date_start=start, date_end=end, event_type=event_type)


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"
