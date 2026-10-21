"""GitHub/Gitee releases, resumable slices and verified update plans."""
from __future__ import annotations
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from . import __version__
from .update_protocol import APP_EXE, APP_ID, INSTALL_MANIFEST, SHA256, relative_name, safe_target, sha256_file, valid_url, validate_files, version_key

APP_EXE_NAME = APP_EXE
NET_TIMEOUT = 12
CREATE_NO_WINDOW = 0x08000000
SOURCES = {
    "github": {"name": "GitHub", "api": "https://api.github.com/repos/Lyan-ing/reimburse/releases?per_page=10", "page": "https://github.com/Lyan-ing/reimburse/releases"},
    "gitee": {"name": "Gitee", "api": "https://gitee.com/api/v5/repos/lyan110/reimburse/releases?per_page=10", "page": "https://gitee.com/lyan110/reimburse/releases"},
}

class ReleaseUnavailable(RuntimeError):
    """A reachable repository has no installable stable release."""

def _is_installed_onedir() -> bool:
    return bool(getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None) and Path(sys._MEIPASS).resolve().parent == Path(sys.executable).resolve().parent)

def install_root() -> Path | None:
    return Path(sys.executable).resolve().parent if _is_installed_onedir() else None

def run_mode() -> str:
    return "安装版 / 目录版" if install_root() is not None else ("单文件便携版" if getattr(sys, "frozen", False) else "源码开发版")

def _request(url, headers=None):
    return urllib.request.Request(quote(valid_url(url), safe=":/?&=%"), headers={"User-Agent": "ReimburseOrganizer-Updater/2", "Accept": "application/json", **(headers or {})})

def _get_json(url):
    with urllib.request.urlopen(_request(url), timeout=NET_TIMEOUT) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("更新清单过大")
    return json.loads(raw.decode("utf-8-sig"))

def _download(url, dest, on_progress=None, *, expected_size=None):
    partial = dest.with_name(dest.name + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    if expected_size is not None and offset >= expected_size:
        partial.unlink()
        offset = 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with urllib.request.urlopen(_request(url, headers), timeout=NET_TIMEOUT) as response:
        resumed = response.status == 206 and offset > 0
        if resumed and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
            raise ValueError("服务器返回的续传范围不正确")
        if not resumed:
            offset = 0
        total = expected_size or offset + int(response.headers.get("Content-Length") or 0)
        done = offset
        with partial.open("ab" if resumed else "wb") as stream:
            while chunk := response.read(256 * 1024):
                stream.write(chunk)
                done += len(chunk)
                if expected_size is not None and done > expected_size:
                    raise ValueError("下载文件超出清单声明大小")
                if on_progress:
                    on_progress(done, total)
    if expected_size is not None and done != expected_size:
        raise ValueError("下载未完成，已保留断点供重试")
    os.replace(partial, dest)

def _validate_package(package):
    if not isinstance(package, dict) or not SHA256.fullmatch(str(package.get("sha256", ""))):
        raise ValueError("更新包缺少有效的 SHA256 校验值")
    if type(package.get("size")) is not int or not 0 < package["size"] <= 2 * 1024**3:
        raise ValueError("更新包大小不正确")
    if package.get("parts"):
        if not isinstance(package["parts"], list) or len(package["parts"]) > 1024:
            raise ValueError("更新分片列表不正确")
        for part in package["parts"]:
            _validate_package(part)
            if part.get("parts"):
                raise ValueError("更新分片不能嵌套")
        if sum(p["size"] for p in package["parts"]) != package["size"]:
            raise ValueError("更新分片大小合计不正确")
    else:
        valid_url(package.get("url", ""))

def validate_manifest(value):
    if not isinstance(value, dict):
        raise ValueError("更新清单格式不正确")
    result = copy.deepcopy(value)
    version_key(result.get("version", ""))
    if result.get("appId", APP_ID) != APP_ID:
        raise ValueError("更新清单属于其他软件")
    result["files"] = validate_files(result.get("files"))
    _validate_package(result.get("full"))
    if result.get("installer"):
        _validate_package(result["installer"])
    deltas = result.get("deltas", []) or ([result["delta"]] if result.get("delta") else [])
    for delta in deltas:
        version_key(delta.get("from", ""))
        _validate_package(delta)
        if delta.get("base_files"):
            delta["base_files"] = validate_files(delta["base_files"])
    result["deltas"] = deltas
    return result

def _release_assets(release):
    assets = release.get("assets", [])
    if isinstance(assets, dict):
        assets = assets.get("links", [])
    return {a["name"]: a.get("browser_download_url") or a.get("url") for a in assets if a.get("name") and (a.get("browser_download_url") or a.get("url"))}

def _source_manifest(source):
    releases = _get_json(SOURCES[source]["api"])
    if not isinstance(releases, list):
        raise ValueError("发布服务器返回了异常响应")
    found, invalid = [], []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        assets = _release_assets(release)
        if "latest.json" not in assets:
            continue
        try:
            manifest = validate_manifest(_get_json(assets["latest.json"]))
            manifest.update(_source=source, _asset_urls=assets, _release_url=release.get("html_url") or SOURCES[source]["page"])
            found.append(manifest)
        except Exception as exc:
            invalid.append(str(exc))
    if found:
        return max(found, key=lambda m: version_key(m["version"]))
    if invalid:
        raise ValueError("已找到发布，但更新清单不可用：" + invalid[0])
    raise ReleaseUnavailable(f"{SOURCES[source]['name']} 暂无带更新附件的正式 Release")

def check_latest(source="auto"):
    names = list(SOURCES) if source == "auto" else [source]
    if any(name not in SOURCES for name in names):
        raise ValueError("未知更新源")
    found, errors, empty = [], [], []
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        tasks = {pool.submit(_source_manifest, name): name for name in names}
        for task in as_completed(tasks):
            name = tasks[task]
            try:
                found.append(task.result())
            except ReleaseUnavailable as exc:
                empty.append(str(exc))
            except Exception as exc:
                errors.append(f"{SOURCES[name]['name']}：{exc}")
    if not found:
        message = "\n".join(empty + errors)
        if empty:
            raise ReleaseUnavailable(message + "\n源码提交不等同于可安装的软件版本。")
        raise RuntimeError("暂时无法连接更新服务器\n" + message)
    latest = max(found, key=lambda m: version_key(m["version"]))
    latest["_mirrors"] = [m.get("_asset_urls", {}) for m in found if m is not latest and m["version"] == latest["version"] and m["files"] == latest["files"]]
    latest["_warnings"] = errors
    return latest

def _version_gt(a, b):
    return version_key(a) > version_key(b)

def check_update(source="auto"):
    latest = check_latest(source)
    return latest if _version_gt(latest["version"], __version__) else None

def read_install_manifest(root):
    try:
        return json.loads((root / INSTALL_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def select_package(manifest, root=None, *, verify_base=True):
    root = root if root is not None else install_root()
    if root is not None:
        local = read_install_manifest(root)
        for delta in manifest.get("deltas", []):
            if delta["from"] != local.get("version") or not delta.get("base_files"):
                continue
            if verify_base and any(not (path := safe_target(root, name)).is_file() or path.stat().st_size != info["size"] or sha256_file(path) != info["sha256"] for name, info in delta["base_files"].items()):
                continue
            return delta, "增量更新"
    return manifest["full"], "完整更新"

def _urls(part, manifest):
    url = part["url"]
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    candidates = [manifest.get("_asset_urls", {}).get(name), url]
    candidates += [mirror.get(name) for mirror in manifest.get("_mirrors", [])]
    return list(dict.fromkeys(valid_url(candidate) for candidate in candidates if candidate))

def _download_package(package, manifest, cache, on_progress=None):
    cache.mkdir(parents=True, exist_ok=True)
    complete = cache / (package["sha256"] + ".zip")
    if complete.is_file() and complete.stat().st_size == package["size"] and sha256_file(complete) == package["sha256"]:
        if on_progress:
            on_progress(package["size"], package["size"])
        return complete
    pieces, completed = [], 0
    for part in package.get("parts") or [package]:
        target = cache / (part["sha256"] + ".piece")
        if not (target.is_file() and target.stat().st_size == part["size"] and sha256_file(target) == part["sha256"]):
            error = None
            for url in _urls(part, manifest):
                try:
                    _download(url, target, (lambda done, total, base=completed: on_progress(base + done, package["size"])) if on_progress else None, expected_size=part["size"])
                    if sha256_file(target) != part["sha256"]:
                        target.unlink(missing_ok=True)
                        target.with_name(target.name + ".part").unlink(missing_ok=True)
                        raise ValueError("更新分片 SHA256 校验失败")
                    error = None
                    break
                except Exception as exc:
                    error = exc
            if error is not None:
                raise RuntimeError(f"更新分片下载失败，已保留可续传数据：{error}") from error
        pieces.append(target)
        completed += part["size"]
        if on_progress:
            on_progress(completed, package["size"])
    temporary = complete.with_suffix(".assembling")
    with temporary.open("wb") as output:
        for piece in pieces:
            with piece.open("rb") as stream:
                shutil.copyfileobj(stream, output)
    if sha256_file(temporary) != package["sha256"]:
        temporary.unlink(missing_ok=True)
        raise ValueError("更新包 SHA256 校验失败，安装文件未改动")
    os.replace(temporary, complete)
    return complete

def _extract_verified(archive, staging, manifest, root):
    changes, seen = [], set()
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            if entry.is_dir():
                continue
            name = relative_name(entry.filename)
            if name.casefold() in seen or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("更新包含重名或符号链接")
            seen.add(name.casefold())
            if name == INSTALL_MANIFEST:
                continue
            info = manifest["files"].get(name)
            if info is None or entry.file_size != info["size"]:
                raise ValueError(f"更新包文件不在清单中或大小不正确：{name}")
            target = safe_target(staging, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(entry) as stream, target.open("wb") as output:
                shutil.copyfileobj(stream, output)
            if sha256_file(target) != info["sha256"]:
                raise ValueError(f"更新文件校验失败：{name}")
            changes.append(name)
    changed = set(changes)
    for name, info in manifest["files"].items():
        path = safe_target(staging if name in changed else root, name)
        if not path.is_file() or path.stat().st_size != info["size"] or sha256_file(path) != info["sha256"]:
            raise ValueError(f"安装后的完整文件清单无法匹配：{name}")
    return changes

def prepare_update(manifest, on_progress=None, on_status=None, *, root=None, cache_dir=None):
    manifest = {**validate_manifest(manifest), **{k: v for k, v in manifest.items() if k.startswith("_")}}
    root = root if root is not None else install_root()
    if root is None:
        raise RuntimeError("开发版/单文件便携版可检测更新；自动安装请使用安装版")
    if not _version_gt(manifest["version"], read_install_manifest(root).get("version", __version__)):
        raise ValueError("目标版本不高于本地版本，无需安装")
    cache = cache_dir or Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "ReimburseOrganizer" / "updates"
    job = cache / ("session-" + uuid.uuid4().hex)
    staging = job / "staging"
    staging.mkdir(parents=True)
    if on_status:
        on_status("正在核对本地安装文件…")
    package, mode = select_package(manifest, root)
    if on_status:
        on_status(f"{mode}：下载分片并校验（中断后可续传）…")
    archive = _download_package(package, manifest, cache / "chunks", on_progress)
    try:
        changes = _extract_verified(archive, staging, manifest, root)
    except (ValueError, OSError, zipfile.BadZipFile):
        if mode != "增量更新":
            raise
        staging = job / "full-staging"
        staging.mkdir()
        if on_status:
            on_status("增量基线不匹配，改用完整更新…")
        archive = _download_package(manifest["full"], manifest, cache / "chunks", on_progress)
        changes = _extract_verified(archive, staging, manifest, root)
        mode = "完整更新"
    previous = read_install_manifest(root)
    removed = [relative_name(name) for name in previous.get("files", {}) if name not in manifest["files"]]
    for name in changes + removed:
        safe_target(root, name)
    metadata = {"appId": APP_ID, "version": manifest["version"], "files": manifest["files"]}
    (job / "next-manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(Path(__file__).with_name("update_apply.ps1"), job / "update_apply.ps1")
    status_file = Path(os.environ.get("APPDATA", str(Path.home()))) / "ReimburseOrganizer" / "update-result.json"
    plan = {"appId": APP_ID, "root": str(root.resolve()), "staging": str(staging.resolve()), "backup": str((job/"backup").resolve()), "manifest": str((job/"next-manifest.json").resolve()), "changes": changes, "removed": removed, "files": manifest["files"], "pid": os.getpid(), "exe": APP_EXE, "version": manifest["version"], "mode": mode, "status": str(status_file)}
    plan_path = job / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if on_status:
        on_status("下载和校验完成，即将退出并安装；失败会恢复旧版。")
    return plan_path

def launch_prepared_update(plan):
    subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(plan.with_name("update_apply.ps1")), "-PlanPath", str(plan)], creationflags=CREATE_NO_WINDOW)

def apply_update(manifest, on_progress=None):
    return prepare_update(manifest, on_progress)

def read_update_result():
    path = Path(os.environ.get("APPDATA", str(Path.home()))) / "ReimburseOrganizer" / "update-result.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8-sig"))
        if result.get("seen"):
            return None
        result["seen"] = True
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    except (OSError, ValueError):
        return None
