from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reimburse import __version__


def _enable_stdout_utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    _enable_stdout_utf8()
    parser = argparse.ArgumentParser(description="报销材料整理助手（离线）")
    parser.add_argument("--selftest", metavar="FOLDER", help="不打开界面，扫描文件夹并生成 Word")
    parser.add_argument("--output", metavar="DOCX", help="配合 --selftest 指定输出文件")
    parser.add_argument("--event-type", choices=["出差", "参会", "采购"], help="事项类型")
    parser.add_argument("--no-ocr", action="store_true", help="关闭图片 OCR")
    parser.add_argument("--scan-only", action="store_true", help="只扫描，不生成 Word")
    parser.add_argument("--legacy", action="store_true", help="打开旧版自动扫描界面")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    if args.selftest:
        from reimburse.docgen import build_document, default_filename
        from reimburse.pipeline import run_scan

        folder = Path(args.selftest).resolve()
        if not folder.exists() or not folder.is_dir():
            print(f"文件夹不存在：{folder}", file=sys.stderr)
            return 2
        result = run_scan(folder, event_type=args.event_type, use_ocr=not args.no_ocr)
        print(f"材料：{len(result.materials)} 份")
        print(f"分组：{len(result.groups)} 组")
        print(f"总额：¥{result.total:,.2f}")
        print(f"提醒：{len(result.reminders)} 项")
        for reminder in result.reminders:
            print(f"  [{reminder.level}] {reminder.message}")
        if args.scan_only:
            return 0
        output = Path(args.output) if args.output else folder / default_filename(result)
        output.parent.mkdir(parents=True, exist_ok=True)
        generated = build_document(result, output)
        print(f"Word：{generated}")
        return 0

    from reimburse.appearance import enable_dpi_awareness, set_app_identity

    set_app_identity()
    enable_dpi_awareness()
    if args.legacy:
        from reimburse.ui import App

        app = App()
    else:
        from reimburse.project_ui import ProjectApp

        app = ProjectApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
