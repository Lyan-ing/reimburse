"""Lightweight shared appearance and Windows application identity."""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

COLORS = {
    "bg": "#F3F7F8", "panel": "#FFFFFF", "ink": "#24373F",
    "muted": "#607780", "accent": "#287F89", "accent_hover": "#206873",
    "sky": "#4A9BD4", "leaf": "#559A65", "clay": "#D9A15F",
    "line": "#D9E5E8", "error": "#B23A3A", "warn": "#A76C17",
    "info": "#3B6E8F", "group": "#E8F4F8", "soft": "#EAF2F4",
    "chip": "#E3F1F1",
}


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def set_app_identity() -> None:
    """Call before creating Tk windows so Python launches get their own taskbar group."""
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ReimburseOrganizer.Desktop")


def asset_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / name


def apply_window_icon(window) -> None:
    """Set both Windows ICO and Tk's inherited icon for dialogs."""
    import tkinter as tk

    icon = asset_path("app.ico")
    png = asset_path("app.png")
    if icon.exists():
        window.iconbitmap(default=str(icon))
    if png.exists():
        # Decode and scale the bundled icon with Tk, without importing Pillow.
        source = tk.PhotoImage(file=str(png), master=window)
        factor = max(1, (max(source.width(), source.height()) + 63) // 64)
        window._app_icon = source.subsample(factor, factor)
        window.iconphoto(True, window._app_icon)
