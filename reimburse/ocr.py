from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
OCR_TIMEOUT_SECONDS = 120


def _resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def find_ocr_script() -> Path | None:
    candidates = [
        Path(__file__).resolve().parent / "ocr_win.ps1",
        _resource_root() / "reimburse" / "ocr_win.ps1",
        _resource_root() / "ocr_win.ps1",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _powershell() -> str | None:
    found = shutil.which("powershell.exe") or shutil.which("powershell")
    if found:
        return found
    fallback = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    if fallback.exists():
        return str(fallback)
    return None


class OcrEngine:
    def __init__(self) -> None:
        self.script = find_ocr_script()
        self.powershell = _powershell()
        self.cache: dict[tuple[str, int, int], tuple[str, str]] = {}
        self.available = bool(self.script and self.powershell)
        self.last_error = ""
        if not self.available:
            self.last_error = "找不到 Windows OCR 组件"

    def recognize_many(self, paths: list[Path]) -> dict[str, tuple[str, str]]:
        """Return original path string -> (text, error)."""
        if not paths:
            return {}
        result: dict[str, tuple[str, str]] = {}
        resolved_map: dict[str, Path] = {}
        pending: list[Path] = []
        for original in paths:
            path = original.expanduser().resolve()
            resolved_map[str(original)] = path
            try:
                stat = path.stat()
            except OSError:
                result[str(original)] = ("", "文件无法访问")
                continue
            key = (str(path), stat.st_mtime_ns, stat.st_size)
            if key in self.cache:
                result[str(original)] = self.cache[key]
            else:
                pending.append(path)
        if pending:
            fresh = self._run(pending)
            for original, path in resolved_map.items():
                if str(path) in fresh:
                    result[original] = fresh[str(path)]
            for path in pending:
                try:
                    stat = path.stat()
                    key = (str(path), stat.st_mtime_ns, stat.st_size)
                    if str(path) in fresh:
                        self.cache[key] = fresh[str(path)]
                except OSError:
                    pass
        return result

    def recognize(self, path: Path) -> tuple[str, str]:
        return self.recognize_many([path]).get(str(path), ("", "OCR 失败"))

    def _run(self, paths: list[Path]) -> dict[str, tuple[str, str]]:
        if not self.available:
            return {str(p): ("", self.last_error) for p in paths}
        temp_dir = Path(tempfile.mkdtemp(prefix="reimburse_ocr_"))
        list_file = temp_dir / "paths.txt"
        out_file = temp_dir / "result.json"
        try:
            list_file.write_text("\n".join(str(p) for p in paths), encoding="utf-8")
            args = [
                self.powershell or "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.script),
                "-ListFile",
                str(list_file),
                "-OutFile",
                str(out_file),
            ]
            creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=OCR_TIMEOUT_SECONDS,
                creationflags=creationflags,
            )
            if not out_file.exists():
                message = (proc.stderr or proc.stdout or "Windows OCR 未返回结果").strip()
                self.last_error = message[:500]
                return {str(p): ("", self.last_error) for p in paths}
            payload = json.loads(out_file.read_text(encoding="utf-8-sig"))
            if isinstance(payload, dict):
                payload = [payload]
            output: dict[str, tuple[str, str]] = {}
            for item in payload:
                path = str(item.get("path", ""))
                if not path:
                    continue
                text = (item.get("text") or "").strip()
                error = (item.get("error") or "").strip()
                output[path] = (text, error)
            for p in paths:
                output.setdefault(str(p), ("", "OCR 未返回该文件"))
            return output
        except subprocess.TimeoutExpired:
            self.last_error = "OCR 超时"
            return {str(p): ("", self.last_error) for p in paths}
        except Exception as exc:
            self.last_error = f"OCR 调用失败: {exc}"
            return {str(p): ("", self.last_error) for p in paths}
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
