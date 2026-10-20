from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from .checks import build_reminders
from .classify import classify
from .extract import Extractor
from .grouping import build_result
from .models import ScanResult
from .parser import parse_material
from .scanner import natural_key, parse_batch_info, scan_folder


class ScanCancelled(Exception):
    pass


ProgressCallback = Callable[[int, int, str, str], None]


def run_scan(
    folder: Path,
    event_type: str | None = None,
    use_ocr: bool = True,
    recursive: bool = True,
    progress: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ScanResult:
    started = time.time()
    folder = folder.resolve()
    batch = parse_batch_info(folder)
    if event_type:
        batch.event_type = event_type
    paths = scan_folder(folder, recursive=recursive, output_dir=folder)
    extractor = Extractor(use_ocr=use_ocr)
    materials = []
    warnings: list[str] = []
    if use_ocr and not extractor.ocr.available:
        warnings.append(f"OCR 不可用：{extractor.ocr.last_error}")
    total = len(paths)
    try:
        for index, path in enumerate(paths, start=1):
            if cancel_check and cancel_check():
                raise ScanCancelled()
            if progress:
                progress(index, total, path.name, "读取内容")
            try:
                material = extractor.extract(path)
                classify(material)
                parse_material(material)
            except Exception as exc:
                from .models import Material

                material = Material(path=path)
                material.error = str(exc)[:300]
                material.method = "提取失败"
            materials.append(material)
        if progress:
            progress(total, total, "", "整理分组")
        result = build_result(folder, batch, materials, batch.event_type)
        result.warnings = warnings
        result.reminders = build_reminders(result)
        result.duration_seconds = round(time.time() - started, 2)
        return result
    finally:
        extractor.cleanup()


def recompute(result: ScanResult, event_type: str | None = None) -> ScanResult:
    """Rebuild groups, payment matching, totals and reminders after manual edits."""
    batch = parse_batch_info(result.folder)
    if event_type:
        batch.event_type = event_type
    for material in result.materials:
        was_duplicate = material.duplicate
        material.group_id = ""
        material.duplicate = False
        material.duplicate_of = ""
        if was_duplicate and material.note and material.note.startswith(("与 ", "发票号码与 ")):
            material.note = ""
    new_result = build_result(result.folder, batch, result.materials, batch.event_type)
    new_result.warnings = list(result.warnings)
    new_result.reminders = build_reminders(new_result)
    new_result.duration_seconds = result.duration_seconds
    return new_result


def scan_summary(result: ScanResult) -> str:
    return (
        f"{len(result.materials)} 份材料，{len(result.groups)} 个票据组，"
        f"总额 ¥{result.total:,.2f}"
    )
