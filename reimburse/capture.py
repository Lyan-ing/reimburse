from __future__ import annotations

import ctypes
import time
import tkinter as tk
from typing import Callable

from PIL import Image, ImageGrab


def virtual_screen() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    left = user32.GetSystemMetrics(76)
    top = user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78)
    height = user32.GetSystemMetrics(79)
    if width <= 0 or height <= 0:
        width = user32.GetSystemMetrics(0)
        height = user32.GetSystemMetrics(1)
        left = top = 0
    return left, top, width, height


def _geometry(x: int, y: int, w: int, h: int) -> str:
    sx = f"+{x}" if x >= 0 else str(x)
    sy = f"+{y}" if y >= 0 else str(y)
    return f"{w}x{h}{sx}{sy}"


def clipboard_image() -> Image.Image | None:
    try:
        value = ImageGrab.grabclipboard()
    except Exception:
        return None
    if isinstance(value, Image.Image):
        return value
    return None


def clipboard_files() -> list[str]:
    try:
        value = ImageGrab.grabclipboard()
    except Exception:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    return []


class ScreenCapturer:
    """A live, translucent selection overlay.

    The previous implementation froze a screenshot into a huge Tk image.  On
    multi-monitor / mixed-DPI machines that image could be a different size
    from the overlay, producing black bands and a scrambled looking screen.
    This version keeps the live desktop underneath the overlay and only grabs
    the selected rectangle after the overlay is destroyed.
    """

    def __init__(self, root: tk.Misc, callback: Callable[[Image.Image | None], None]):
        self.root = root
        self.callback = callback
        self.overlay: tk.Toplevel | None = None
        self.canvas: tk.Canvas | None = None
        self.start_point: tuple[int, int] | None = None
        self.rect_id: int | None = None
        self.origin = (0, 0)
        self.screen_size = (0, 0)

    def start(self) -> None:
        try:
            self.root.withdraw()
            self.root.update_idletasks()
            time.sleep(0.15)
            # Use the primary display exactly as Tk sees it.  The Windows
            # virtual-screen metrics can differ from the DPI-scaled desktop and
            # produced black bands / a stretched overlay on mixed-DPI setups.
            width = self.root.winfo_screenwidth()
            height = self.root.winfo_screenheight()
            left, top = 0, 0
            self.origin = (left, top)
            self.screen_size = (width, height)
            self.overlay = tk.Toplevel(self.root)
            self.overlay.overrideredirect(True)
            self.overlay.attributes("-topmost", True)
            self.overlay.attributes("-alpha", 0.34)
            self.overlay.geometry(_geometry(left, top, width, height))
            self.overlay.configure(bg="#081018")
            self.canvas = tk.Canvas(
                self.overlay,
                highlightthickness=0,
                bd=0,
                bg="#081018",
                cursor="crosshair",
            )
            self.canvas.pack(fill="both", expand=True)
            self.canvas.create_rectangle(0, 0, width, 58, fill="#0E1A22", outline="")
            self.canvas.create_text(
                width // 2,
                28,
                text="拖动鼠标选择截图区域   ·   Esc 取消",
                fill="#FFFFFF",
                font=("Microsoft YaHei UI", 14, "bold"),
            )
            self.canvas.bind("<ButtonPress-1>", self._on_press)
            self.canvas.bind("<B1-Motion>", self._on_drag)
            self.canvas.bind("<ButtonRelease-1>", self._on_release)
            self.overlay.bind("<Escape>", lambda _event: self._cancel())
            self.overlay.update_idletasks()
            self.overlay.update()
            self.overlay.focus_force()
        except Exception:
            self._finish(None)

    def _on_press(self, event) -> None:
        self.start_point = (event.x, event.y)
        if self.canvas is not None:
            if self.rect_id is not None:
                self.canvas.delete(self.rect_id)
            self.rect_id = self.canvas.create_rectangle(
                event.x,
                event.y,
                event.x + 1,
                event.y + 1,
                outline="#5BE0E8",
                width=2,
            )

    def _on_drag(self, event) -> None:
        if not self.start_point or self.canvas is None or self.rect_id is None:
            return
        x1, y1 = self.start_point
        self.canvas.coords(self.rect_id, x1, y1, event.x, event.y)

    def _on_release(self, event) -> None:
        if not self.start_point:
            return
        x1, y1 = self.start_point
        x2, y2 = event.x, event.y
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        if right - left < 12 or bottom - top < 12:
            return
        self._capture(left, top, right, bottom)

    def _capture(self, left: int, top: int, right: int, bottom: int) -> None:
        if self.overlay is not None:
            try:
                self.overlay.destroy()
            except Exception:
                pass
            self.overlay = None
        try:
            self.root.update()
            time.sleep(0.08)
            screen = ImageGrab.grab()
            vw, vh = self.screen_size
            scale_x = screen.width / vw if vw else 1.0
            scale_y = screen.height / vh if vh else 1.0
            crop_box = (
                max(0, min(screen.width, int(left * scale_x))),
                max(0, min(screen.height, int(top * scale_y))),
                max(0, min(screen.width, int(right * scale_x))),
                max(0, min(screen.height, int(bottom * scale_y))),
            )
            crop = screen.crop(crop_box)
            self._finish(crop)
        except Exception:
            self._finish(None)

    def _cancel(self) -> None:
        self._finish(None)

    def _finish(self, image: Image.Image | None) -> None:
        if self.overlay is not None:
            try:
                self.overlay.destroy()
            except Exception:
                pass
            self.overlay = None
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass
        self.callback(image)
