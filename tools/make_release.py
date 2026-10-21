"""一键准备发布（安装版 + 文件级增量更新）：

1) PyInstaller 打 onedir（--skip-build 可跳过，直接用已有 onedir）
2) 扫描 onedir 生成文件清单（相对路径 / sha256 / size）
3) 打 full.zip（整包）；带上一版清单时打 delta.zip（只含变化的文件 → 增量更新）
4) 生成 latest.json（客户端 updater 依据它检查/下载更新）

产物输出到 release_prep/：latest.json + full.zip + [delta.zip]，
把它们上传到 gitee/github release 即完成发布。

用法示例：
  python tools/make_release.py \
      --base-url "https://gitee.com/lyan110/reimburse/releases/download/v2.2.0" \
      --prev-manifest "release_prep/上一版latest.json" \
      --notes "1. xxx\n2. yyy"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from reimburse import __version__  # noqa: E402
from reimburse.update_protocol import APP_ID, INSTALL_MANIFEST, validate_files

APP = "报销材料整理助手"
DEFAULT_PY = r"C:\Users\LENOVO\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_manifest(onedir: Path) -> dict:
    files: dict[str, dict] = {}
    for p in sorted(onedir.rglob("*")):
        if p.is_file() and p.name != INSTALL_MANIFEST:
            rel = p.relative_to(onedir).as_posix()
            files[rel] = {"sha256": sha256_file(p), "size": p.stat().st_size}
    return files


def make_zip(src_dir: Path, zip_path: Path, only: set[str] | None = None) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(src_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(src_dir).as_posix()
                if only is None or rel in only:
                    z.write(p, rel)


def package_info(archive: Path, base_url: str, chunk_bytes: int) -> dict:
    info = {"url": f"{base_url}/{archive.name}", "size": archive.stat().st_size, "sha256": sha256_file(archive)}
    parts = []
    with archive.open("rb") as stream:
        index = 1
        while data := stream.read(chunk_bytes):
            path = archive.with_name(f"{archive.name}.part{index:03}")
            path.write_bytes(data)
            parts.append({"url": f"{base_url}/{path.name}", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            index += 1
    info["parts"] = parts
    return info


def build_installer(onedir: Path, out: Path, iscc: str, version: str) -> Path:
    subprocess.run([iscc, "/Qp", f"/DAppVersion={version}", f"/DSourceDir={onedir}", f"/DOutputDir={out}", f"/DAppIcon={ROOT / 'assets/app.ico'}", str(ROOT / "tools/installer.iss")], check=True)
    return out / f"reimburse-setup-v{version}.exe"


def build_onedir(py: str, output_root: Path | None = None) -> Path:
    output_root = output_root or ROOT
    (output_root / "build").mkdir(parents=True, exist_ok=True)
    version_file = output_root / "build" / "version-info.txt"
    numeric = tuple(int(part) for part in __version__.split(".")) + (0,)
    version_file.write_text(
        f"VSVersionInfo(ffi=FixedFileInfo(filevers={numeric!r}, prodvers={numeric!r}, mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0,0)), "
        f"kids=[StringFileInfo([StringTable('040904B0', [StringStruct('FileDescription', {APP!r}), StringStruct('FileVersion', {__version__!r}), StringStruct('ProductVersion', {__version__!r}), StringStruct('ProductName', {APP!r}), StringStruct('OriginalFilename', {APP+'.exe'!r})])]), VarFileInfo([VarStruct('Translation', [1033,1200])])])",
        encoding="utf-8",
    )
    subprocess.run(
        [
            py, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
            "--name", APP, "--icon", str(ROOT / "assets" / "app.ico"),
            "--version-file", str(version_file),
            "--add-data", f"{ROOT / 'reimburse' / 'ocr_win.ps1'};reimburse",
            "--add-data", f"{ROOT / 'assets'};assets",
            "--add-data", f"{ROOT / 'reimburse' / 'update_apply.ps1'};reimburse",
            "--collect-binaries", "pypdfium2", "--collect-submodules", "pypdfium2",
            "--collect-data", "docx", "--exclude-module", "numpy",
            "--exclude-module", "PIL.AvifImagePlugin", "--exclude-module", "PIL._avif",
            "--exclude-module", "lxml.objectify", "--optimize", "1",
            "--distpath", str(output_root / "dist"), "--workpath", str(output_root / "build"),
            "--specpath", str(output_root / "build"), str(ROOT / "app.py"),
        ],
        check=True,
    )
    return output_root / "dist" / APP


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=__version__)
    ap.add_argument("--notes", default="")
    ap.add_argument("--base-url", required=True, help="release 下载基址，如 https://gitee.com/lyan110/reimburse/releases/download/v2.2.0")
    ap.add_argument("--prev-manifest", default=None, help="上一版 latest.json（含 files 清单），用于算增量")
    ap.add_argument("--python", default=DEFAULT_PY)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--output-dir", type=Path, help="隔离的本地产物目录（不覆盖现有 dist/release_prep）")
    ap.add_argument("--chunk-mb", type=int, default=4, help="下载分片大小，默认 4 MiB")
    ap.add_argument("--installer", action="store_true", help="同时生成可卸载的每用户安装包")
    ap.add_argument("--iscc", default=shutil.which("ISCC.exe"), help="Inno Setup 编译器路径")
    args = ap.parse_args()
    if args.version != __version__:
        ap.error("版本号必须与软件源码一致，请先更新 reimburse/__init__.py")
    if not 1 <= args.chunk_mb <= 64:
        ap.error("分片大小应在 1 至 64 MiB 之间")
    if args.installer and (not args.iscc or not Path(args.iscc).is_file()):
        ap.error("生成安装包需要有效的 --iscc 编译器路径")

    output_root = args.output_dir.resolve() if args.output_dir else ROOT
    if not args.skip_build:
        print("== 打 onedir 包 ==")
        build_onedir(args.python, output_root)
    onedir = output_root / "dist" / APP
    if not onedir.exists():
        print(f"找不到 onedir：{onedir}")
        sys.exit(1)

    print("== 扫描文件清单 ==")
    files = scan_manifest(onedir)
    files = validate_files(files)
    (onedir / INSTALL_MANIFEST).write_text(json.dumps({"appId": APP_ID, "version": args.version, "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")
    out = output_root / "release_prep"
    out.mkdir(exist_ok=True)

    full_zip = out / f"reimburse-v{args.version}.zip"
    print("== 打整包 ==")
    make_zip(onedir, full_zip)
    latest = {
        "schema": 2,
        "appId": APP_ID,
        "version": args.version,
        "releaseDate": datetime.now(timezone.utc).isoformat(),
        "notes": args.notes,
        "full": package_info(full_zip, args.base_url, args.chunk_mb * 1024**2),
    }

    if args.prev_manifest:
        prev = json.loads(Path(args.prev_manifest).read_text(encoding="utf-8"))
        prev_files = validate_files(prev.get("files", {}))
        prev_ver = prev.get("version", "")
        changed = {rel for rel, info in files.items() if prev_files.get(rel, {}).get("sha256") != info["sha256"]}
        removed = sorted(set(prev_files) - set(files))
        if (changed or removed) and prev_ver and prev_ver != args.version:
            delta_zip = out / f"reimburse-delta-{prev_ver}-to-{args.version}.zip"
            print(f"== 打增量包（变化 {len(changed)} 个文件）==")
            make_zip(onedir, delta_zip, only=changed | {INSTALL_MANIFEST})
            delta = {
                "from": prev_ver,
                **package_info(delta_zip, args.base_url, args.chunk_mb * 1024**2),
                "changed": len(changed),
                "removed": removed,
                "base_files": prev_files,
            }
            latest["deltas"] = [delta]
            latest["delta"] = delta  # Older clients can still download the ZIP.
        else:
            print("（无变化文件或版本相同，未生成增量包，客户端将用整包）")

    latest["files"] = files
    if args.installer:
        setup = build_installer(onedir, out, args.iscc, args.version)
        latest["installer"] = {"url": f"{args.base_url}/{setup.name}", "size": setup.stat().st_size, "sha256": sha256_file(setup)}
    (out / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 56)
    print(f"版本 {args.version} · 文件 {len(files)} 个")
    print(f"整包：{full_zip.name}  {full_zip.stat().st_size / 1024 / 1024:.1f} MB")
    if "delta" in latest:
        d = latest["delta"]
        print(f"增量：{d['url'].split('/')[-1]}  变化 {d['changed']} 个  {d['size'] / 1024 / 1024:.2f} MB  (from {d['from']})")
    print(f"清单：{out / 'latest.json'}")
    print("本地准备完成，尚未上传或发布。将 latest.json、安装包、ZIP 及所有 .part 分片上传到同一正式 Release。")


if __name__ == "__main__":
    main()
