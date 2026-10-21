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
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from reimburse import __version__  # noqa: E402

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
        if p.is_file():
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


def build_onedir(py: str, output_root: Path | None = None) -> Path:
    output_root = output_root or ROOT
    subprocess.run(
        [
            py, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
            "--name", APP, "--icon", str(ROOT / "assets" / "app.ico"),
            "--add-data", f"{ROOT / 'reimburse' / 'ocr_win.ps1'};reimburse",
            "--add-data", f"{ROOT / 'assets'};assets",
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
    args = ap.parse_args()

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
    out = output_root / "release_prep"
    out.mkdir(exist_ok=True)

    full_zip = out / f"reimburse-v{args.version}.zip"
    print("== 打整包 ==")
    make_zip(onedir, full_zip)
    latest = {
        "version": args.version,
        "releaseDate": datetime.now(timezone.utc).isoformat(),
        "notes": args.notes,
        "full": {"url": f"{args.base_url}/{full_zip.name}", "size": full_zip.stat().st_size, "sha256": sha256_file(full_zip)},
    }

    if args.prev_manifest:
        prev = json.loads(Path(args.prev_manifest).read_text(encoding="utf-8"))
        prev_files = prev.get("files", {})
        prev_ver = prev.get("version", "")
        changed = {rel for rel, info in files.items() if prev_files.get(rel, {}).get("sha256") != info["sha256"]}
        if changed and prev_ver and prev_ver != args.version:
            delta_zip = out / f"reimburse-delta-{prev_ver}-to-{args.version}.zip"
            print(f"== 打增量包（变化 {len(changed)} 个文件）==")
            make_zip(onedir, delta_zip, only=changed)
            latest["delta"] = {
                "from": prev_ver,
                "url": f"{args.base_url}/{delta_zip.name}",
                "size": delta_zip.stat().st_size,
                "sha256": sha256_file(delta_zip),
                "changed": len(changed),
            }
        else:
            print("（无变化文件或版本相同，未生成增量包，客户端将用整包）")

    latest["files"] = files
    (out / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 56)
    print(f"版本 {args.version} · 文件 {len(files)} 个")
    print(f"整包：{full_zip.name}  {full_zip.stat().st_size / 1024 / 1024:.1f} MB")
    if "delta" in latest:
        d = latest["delta"]
        print(f"增量：{d['url'].split('/')[-1]}  变化 {d['changed']} 个  {d['size'] / 1024 / 1024:.2f} MB  (from {d['from']})")
    print(f"清单：{out / 'latest.json'}")
    print("把 release_prep/ 里的 latest.json 与 zip 上传到 release 即完成发布。")


if __name__ == "__main__":
    main()
