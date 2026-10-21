"""Shared validation for release files and local update plans."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

APP_ID = "ReimburseOrganizer.Desktop"
APP_EXE = "报销材料整理助手.exe"
INSTALL_MANIFEST = "install-manifest.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def version_key(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", str(value).strip())
    if match is None:
        raise ValueError(f"版本号格式不正确：{value}")
    return tuple(int(part or 0) for part in match.groups())


def relative_name(value: str) -> str:
    name = str(value).replace("\\", "/")
    parts = name.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if not name or PurePosixPath(name).is_absolute() or any(
        part in {"", ".", ".."} or part.endswith((".", " "))
        or any(ord(c) < 32 or c in ':<>"|?*' for c in part)
        or part.split(".")[0].upper() in reserved for part in parts
    ):
        raise ValueError(f"更新包路径不安全：{value}")
    return name


def safe_target(root: Path, name: str) -> Path:
    root = root.resolve()
    path = root / relative_name(name)
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"更新路径超出安装目录：{name}")
    return path


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def valid_url(value: str) -> str:
    parsed = urlsplit(str(value))
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("更新下载地址格式不正确")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
        raise ValueError("更新下载必须使用 HTTPS")
    return str(value)


def validate_files(files: dict) -> dict:
    if not isinstance(files, dict) or not files or len(files) > 20000:
        raise ValueError("更新清单缺少有效的文件列表")
    normalized = {}
    seen = set()
    for name, info in files.items():
        name = relative_name(name)
        if name.casefold() in seen:
            raise ValueError("更新清单包含重名路径")
        seen.add(name.casefold())
        if not isinstance(info, dict) or not SHA256.fullmatch(str(info.get("sha256", ""))):
            raise ValueError(f"文件校验值不正确：{name}")
        size = info.get("size")
        if type(size) is not int or not 0 <= size <= 1024**3:
            raise ValueError(f"文件大小不正确：{name}")
        if name == INSTALL_MANIFEST:
            raise ValueError("安装清单不能包含自身")
        normalized[name] = {"sha256": info["sha256"], "size": size}
    if APP_EXE not in normalized:
        raise ValueError("更新清单缺少主程序")
    return normalized
