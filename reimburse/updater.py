from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import quote
import zipfile
from pathlib import Path

from . import __version__

APP_EXE_NAME = "报销材料整理助手.exe"
NET_TIMEOUT = 30
CREATE_NO_WINDOW = 0x08000000

# latest.json 托管地址：发布到 gitee release 附件（固定 tag: latest）。
# 换平台只改这一处即可。
MANIFEST_URL = "https://gitee.com/lyan110/reimburse/releases/download/latest/latest.json"


def _is_installed_onedir() -> bool:
    """是否为“安装版”（PyInstaller onedir）。仅安装版支持自动更新。

    onefile 便携版 sys._MEIPASS 在系统临时目录，不适合就地更新，会返回 False。
    """
    if not getattr(sys, "frozen", False):
        return False
    internal = getattr(sys, "_MEIPASS", None)
    if not internal:
        return False
    try:
        exe_dir = Path(sys.executable).resolve().parent
        return Path(internal).resolve().parent == exe_dir
    except OSError:
        return False


def install_root() -> Path | None:
    """安装版的根目录（exe 所在目录）；非安装版返回 None。"""
    if not _is_installed_onedir():
        return None
    return Path(sys.executable).resolve().parent


def _get_json(url: str) -> dict:
    req = urllib.request.Request(quote(url, safe=":/?&=%"), headers={"User-Agent": "reimburse-updater"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download(url: str, dest: Path, on_progress=None) -> None:
    req = urllib.request.Request(quote(url, safe=":/?&=%"), headers={"User-Agent": "reimburse-updater"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if on_progress and total:
                on_progress(done, total)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_version(v: str) -> list[int]:
    return [int(x) for x in str(v).strip().split(".") if x.isdigit()]


def _version_gt(a: str, b: str) -> bool:
    pa, pb = _parse_version(a), _parse_version(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb


def check_update() -> dict | None:
    """Return a newer manifest or None; report network/manifest errors separately."""
    try:
        data = _get_json(MANIFEST_URL)
    except Exception as exc:
        raise RuntimeError("无法连接更新服务器") from exc
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        raise ValueError("更新清单格式不正确")
    remote = str(data.get("version", ""))
    if not remote or not _version_gt(remote, __version__):
        return None
    return data


def apply_update(manifest: dict, on_progress=None) -> None:
    """下载更新包 → 校验 → 解压 → 计划覆盖 → 重启。成功后本进程会退出。

    仅在安装版调用。优先用与当前版本匹配的增量包，否则用整包。
    """
    root = install_root()
    if root is None:
        raise RuntimeError("当前不是安装版，无法自动更新（请手动下载新版覆盖）")

    pkg: dict = {}
    delta = manifest.get("delta") or {}
    if delta.get("from") == __version__ and delta.get("url"):
        pkg = delta
    else:
        pkg = manifest.get("full") or {}
    url = pkg.get("url")
    expect = (pkg.get("sha256") or "").lower()
    if not url:
        raise RuntimeError("更新清单缺少下载地址")

    tmp = Path(tempfile.mkdtemp(prefix="reimburse_update_"))
    zip_path = tmp / "update.zip"
    _download(url, zip_path, on_progress)
    if len(expect) != 64 or any(c not in "0123456789abcdef" for c in expect):
        raise RuntimeError("更新清单缺少有效的 SHA256 校验值")
    if _sha256_file(zip_path) != expect:
        raise RuntimeError("更新包校验失败（SHA256 不匹配），已中止，未改动任何文件")

    staging = tmp / "staging"
    staging.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging)

    exe = root / APP_EXE_NAME
    bat = tmp / "apply_update.bat"
    # 批处理：等主程序退出 → 用 staging 覆盖安装目录（只含变化文件，即增量）→ 重启 → 清理
    lines = [
        "@echo off",
        ":wait",
        'tasklist /fi "im eq %s" | find /i "%s" >nul' % (APP_EXE_NAME, APP_EXE_NAME),
        "if not errorlevel 1 (timeout /t 1 /nobreak >nul & goto wait)",
        'xcopy /E /Y /I /Q "%s" "%s" >nul' % (str(staging), str(root)),
        'start "" "%s"' % str(exe),
        'rmdir /S /Q "%s"' % str(tmp),
    ]
    bat.write_text("\r\n".join(lines), encoding="gbk")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=CREATE_NO_WINDOW)
    os._exit(0)
