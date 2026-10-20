from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import time
import tkinter as tk
from typing import Callable

from PIL import Image, ImageGrab


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


def _same_image(a: Image.Image | None, b: Image.Image | None) -> bool:
    """Cheap inequality test used to detect a *new* clipboard image."""
    if a is b:
        return True
    if a is None or b is None:
        return False
    if a.size != b.size or a.mode != b.mode:
        return False
    try:
        def sig(im: Image.Image) -> bytes:
            return hashlib.md5(im.resize((32, 32)).tobytes()).digest()

        return sig(a) == sig(b)
    except Exception:
        return False


def _press_snip_hotkey() -> None:
    """Fire the OS "Win + Shift + S" snipping shortcut as a last resort.

    This is only used when the portable triggers below fail.  Because the
    shortcut can be remapped by the user, it must not be the primary path.
    """
    try:
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_EXTENDED = 0x0001
        # Left Windows, Left Shift, S  ->  down in order, up in reverse.
        # The Windows key is an extended key, so flag it as such.
        user32.keybd_event(0x5B, 0, KEYEVENTF_EXTENDED, 0)   # LWIN down
        user32.keybd_event(0xA0, 0, 0, 0)                    # LSHIFT down
        user32.keybd_event(0x53, 0, 0, 0)                    # S down
        user32.keybd_event(0x53, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(0xA0, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(0x5B, 0, KEYEVENTF_EXTENDED | KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def _launch_system_snip() -> None:
    """Open the operating system's snipping overlay.

    We deliberately do NOT simulate the user's personal hotkey (it may be
    remapped on a different PC).  Instead we use the OS's own ``ms-screenclip:``
    protocol -- the same capture experience Win+Shift+S triggers -- which is
    present on every default Windows 10/11 install and covers all monitors at
    the correct DPI.  The captured image is copied to the clipboard, which the
    caller then picks up.
    """
    create_no_window = 0x08000000
    methods = [
        # 1) The ms-screenclip: URI opens the snip overlay directly.
        lambda: os.startfile("ms-screenclip:"),  # type: ignore[attr-defined]
        # 2) Same URI via explorer (handy if ShellExecute of the bare scheme
        #    is blocked in some environments).
        lambda: subprocess.run(
            ["explorer", "ms-screenclip:"], check=False, creationflags=create_no_window
        ),
        # 3) Launch the Snipping Tool / Snip & Sketch app directly; the user
        #    then clicks "New" in the app window.
        lambda: subprocess.run(
            ["explorer", r"shell:AppsFolder\Microsoft.ScreenSketch_8wekyb3d8bbwe!App"],
            check=False,
            creationflags=create_no_window,
        ),
        # 4) Last resort: the default Win+Shift+S shortcut.
        _press_snip_hotkey,
    ]
    for method in methods:
        try:
            method()
            return
        except Exception:
            continue


class ScreenCapturer:
    """Delegate screenshotting to the operating system.

    Clicking "截图" triggers the native snipping tool and then watches the
    clipboard.  As soon as a *new* image lands there (the one the user just
    snipped), it is handed back through the callback and pasted into the slot.
    This supports every monitor and every DPI setting for free, because the OS
    owns the capture.  We deliberately keep our own window visible: the OS
    overlay covers it, and pressing ESC to cancel simply dismisses the overlay
    so our window is immediately back -- no hidden window left waiting on a
    polling timeout (which used to look like a hang and stranded the console).
    """

    # How long we keep watching the clipboard after the shortcut is pressed.
    TIMEOUT_SECONDS = 30.0
    POLL_MS = 150

    def __init__(self, root: tk.Misc, callback: Callable[[Image.Image | None], None]):
        self.root = root
        self.callback = callback
        self.deadline = 0.0
        self.before: Image.Image | None = None

    def start(self) -> None:
        try:
            # 不隐藏主窗口：系统截图会弹出全屏覆盖层盖住我们，用户按 ESC 取消后
            # 覆盖层消失、主窗口自然恢复可见。若在此 withdraw，取消后要等满轮询
            # 超时（30s）才 deiconify，期间像卡死，还会剩下一个孤零零的 cmd 终端。
            self.root.update_idletasks()
            time.sleep(0.15)
            # Remember whatever is already on the clipboard so we only accept a
            # genuinely new image (and ignore a stale one left from before).
            self.before = clipboard_image()
            _launch_system_snip()
            self.deadline = time.time() + self.TIMEOUT_SECONDS
            self.root.after(self.POLL_MS, self._poll)
        except Exception:
            self._finish(None)

    def _poll(self) -> None:
        try:
            if time.time() > self.deadline:
                self._finish(None)
                return
            image = clipboard_image()
            if image is not None and not _same_image(image, self.before):
                self._finish(image)
                return
            self.root.after(self.POLL_MS, self._poll)
        except Exception:
            self._finish(None)

    def _finish(self, image: Image.Image | None) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass
        self.callback(image)
