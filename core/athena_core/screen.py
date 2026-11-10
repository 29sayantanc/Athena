"""Screen capture, downscaling, element marks and coordinate mapping."""
from __future__ import annotations

import base64
import io
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import win32

log = logging.getLogger("athena.screen")


@dataclass
class Element:
    id: int
    role: str
    name: str
    rect: tuple[int, int, int, int]  # physical screen px: left, top, right, bottom
    value: str = ""

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2


@dataclass
class Frame:
    """One screenshot as the model sees it."""
    image: Image.Image                      # the downscaled image sent to the model
    monitor: tuple[int, int, int, int]      # left, top, width, height (physical px)
    full_size: tuple[int, int]              # original capture size
    taken_at: float = field(default_factory=time.time)
    path: Path | None = None
    elements: list[Element] = field(default_factory=list)
    window: dict | None = None
    cursor: tuple[int, int] | None = None

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """Image pixel -> physical screen pixel."""
        mx, my, mw, mh = self.monitor
        iw, ih = self.image.size
        sx = mx + x * mw / iw
        sy = my + y * mh / ih
        return int(round(min(max(sx, mx), mx + mw - 1))), int(round(min(max(sy, my), my + mh - 1)))

    def to_image(self, sx: float, sy: float) -> tuple[int, int]:
        mx, my, mw, mh = self.monitor
        iw, ih = self.image.size
        return int(round((sx - mx) * iw / mw)), int(round((sy - my) * ih / mh))

    def contains_screen(self, sx: int, sy: int) -> bool:
        mx, my, mw, mh = self.monitor
        return mx <= sx < mx + mw and my <= sy < my + mh

    def encode(self, quality: int = 82) -> str:
        buf = io.BytesIO()
        self.image.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _target_size(w: int, h: int, max_w: int, round_to: int) -> tuple[int, int]:
    scale = min(1.0, max_w / w)
    tw, th = w * scale, h * scale
    if round_to and round_to > 1:
        tw = max(round_to, int(round(tw / round_to)) * round_to)
        th = max(round_to, int(round(th / round_to)) * round_to)
    return int(tw), int(th)


def monitors() -> list[tuple[int, int, int, int]]:
    if not win32.IS_WINDOWS:
        return [(0, 0, 1920, 1080)]
    import mss
    with mss.mss() as sct:
        return [(m["left"], m["top"], m["width"], m["height"]) for m in sct.monitors[1:]]


def monitor_for_point(x: int, y: int) -> tuple[int, int, int, int]:
    mons = monitors()
    for m in mons:
        if m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3]:
            return m
    return mons[0]


def grab(monitor: tuple[int, int, int, int] | None = None, scope: str = "cursor_monitor") -> Image.Image:
    """Raw capture at full physical resolution."""
    if not win32.IS_WINDOWS:
        img = Image.new("RGB", (1920, 1080), (245, 245, 245))
        d = ImageDraw.Draw(img)
        d.rectangle((100, 100, 400, 160), fill=(30, 110, 230))
        d.text((120, 120), "Reply", fill="white")
        return img
    import mss
    with mss.mss() as sct:
        if monitor is None:
            region = sct.monitors[0] if scope == "all_monitors" else None
            if region is None:
                cx, cy = win32.cursor_pos()
                l, t, w, h = monitor_for_point(cx, cy)
                region = {"left": l, "top": t, "width": w, "height": h}
        else:
            l, t, w, h = monitor
            region = {"left": l, "top": t, "width": w, "height": h}
        shot = sct.grab(region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def capture(max_w: int = 1344, round_to: int = 28, scope: str = "cursor_monitor",
            monitor: tuple[int, int, int, int] | None = None) -> Frame:
    cx, cy = win32.cursor_pos()
    if monitor is None:
        if scope == "all_monitors" and win32.IS_WINDOWS:
            vx, vy, vw, vh = win32.virtual_screen()
            monitor = (vx, vy, vw, vh)
        else:
            monitor = monitor_for_point(cx, cy)
    raw = grab(monitor)
    tw, th = _target_size(raw.width, raw.height, max_w, round_to)
    img = raw.resize((tw, th), Image.LANCZOS) if (tw, th) != raw.size else raw
    frame = Frame(image=img, monitor=monitor, full_size=raw.size, cursor=(cx, cy))
    frame._raw = raw  # kept for zoom; not serialised
    return frame


def zoom(frame: Frame, box: tuple[float, float, float, float], max_w: int = 1344) -> Frame:
    """Crop a region (in frame image coords) from the full-resolution capture."""
    raw = getattr(frame, "_raw", None)
    if raw is None:
        raw = frame.image
    x1, y1, x2, y2 = box
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    s1 = frame.to_screen(x1, y1)
    s2 = frame.to_screen(x2, y2)
    mx, my, mw, mh = frame.monitor
    rx = raw.width / mw
    ry = raw.height / mh
    crop_box = (int((s1[0] - mx) * rx), int((s1[1] - my) * ry), int((s2[0] - mx) * rx), int((s2[1] - my) * ry))
    if crop_box[2] - crop_box[0] < 8 or crop_box[3] - crop_box[1] < 8:
        raise ValueError("zoom region too small")
    crop = raw.crop(crop_box)
    scale = min(3.0, max_w / crop.width, 1000 / crop.height)
    img = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))), Image.LANCZOS)
    sub_monitor = (s1[0], s1[1], s2[0] - s1[0], s2[1] - s1[1])
    z = Frame(image=img, monitor=sub_monitor, full_size=crop.size)
    z._raw = crop
    return z


def _font(size: int):
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_marks(frame: Frame, elements: list[Element]) -> Image.Image:
    """Return a copy of the frame image with small numbered tags on elements (Set-of-Marks)."""
    img = frame.image.convert("RGB").copy()
    d = ImageDraw.Draw(img, "RGBA")
    font = _font(max(10, img.width // 110))
    for el in elements:
        l, t = frame.to_image(el.rect[0], el.rect[1])
        r, b = frame.to_image(el.rect[2], el.rect[3])
        if r - l < 3 or b - t < 3:
            continue
        d.rectangle((l, t, r, b), outline=(255, 64, 160, 170), width=1)
        label = str(el.id)
        tb = d.textbbox((0, 0), label, font=font)
        tw, th = tb[2] - tb[0] + 4, tb[3] - tb[1] + 3
        bx, by = l, max(0, t - th)
        d.rectangle((bx, by, bx + tw, by + th), fill=(255, 40, 150, 215))
        d.text((bx + 2, by + 1 - tb[1]), label, fill=(255, 255, 255, 255), font=font)
    return img


def save(frame: Frame, folder: Path, name: str, quality: int = 78) -> Path:
    """Saves exactly what the model saw (downscaled, with element marks)."""
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.jpg"
    frame.image.convert("RGB").save(p, "JPEG", quality=quality, optimize=True)
    frame.path = p
    return p
