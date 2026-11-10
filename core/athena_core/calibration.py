"""Model coordinate calibration.

Vision models disagree on how they express positions: absolute pixels of the
image they were given (Qwen2.5-VL, UI-TARS), a 0-1000 grid (Qwen3-VL, many
grounding models), or 0-1 fractions. Calibration shows lettered targets at
known positions, asks the model where they are, picks the convention that fits
best and learns a small linear correction on top. Stored per model id.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import CALIBRATION_PATH

log = logging.getLogger("athena.calibration")

CONVENTIONS = ("pixels", "norm1000", "norm1")
TARGET_FRACTIONS = [(0.12, 0.16), (0.86, 0.18), (0.5, 0.5), (0.18, 0.84), (0.8, 0.8), (0.62, 0.3), (0.35, 0.62)]
LABELS = "ABCDEFG"


def to_fraction(conv: str, v: float, size: int) -> float:
    if conv == "pixels":
        return v / max(size, 1)
    if conv == "norm1000":
        return v / 1000.0
    return v


def guess_convention(model_id: str, x: float, y: float, w: int, h: int) -> str:
    mid = (model_id or "").lower().replace("_", "-")
    if 0 <= x <= 1.0 and 0 <= y <= 1.0:
        return "norm1"
    if re.search(r"qwen3[-.]?vl|qwen3-omni|glm-4\.[15]v|ui-?venus|holo", mid):
        return "norm1000"
    if (x > w or y > h) and x <= 1000 and y <= 1000:
        return "norm1000"
    return "pixels"


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least squares y = k*x + c."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var < 1e-9:
        return 1.0, my - mx
    k = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
    return k, my - k * mx


class CoordMapper:
    def __init__(self, path: Path = CALIBRATION_PATH):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def get(self, model_id: str) -> dict | None:
        return self.data.get(model_id)

    def describe(self, model_id: str, mode_setting: str) -> str:
        if mode_setting != "auto":
            return mode_setting
        c = self.get(model_id)
        return f"calibrated:{c['convention']}" if c else "auto"

    def map(self, model_id: str, x: float, y: float, w: int, h: int, mode_setting: str = "auto") -> tuple[float, float]:
        """Model output -> image pixel coordinates."""
        cal = self.get(model_id) if mode_setting == "auto" else None
        if cal:
            conv = cal["convention"]
            fx = to_fraction(conv, x, w) * cal["kx"] + cal["cx"]
            fy = to_fraction(conv, y, h) * cal["ky"] + cal["cy"]
        else:
            conv = mode_setting if mode_setting in CONVENTIONS else guess_convention(model_id, x, y, w, h)
            fx, fy = to_fraction(conv, x, w), to_fraction(conv, y, h)
        fx = min(max(fx, 0.0), 0.9999)
        fy = min(max(fy, 0.0), 0.9999)
        return fx * w, fy * h

    def fit(self, model_id: str, pred: list[tuple[float, float]], truth_px: list[tuple[float, float]],
            w: int, h: int) -> dict:
        """pred: model outputs; truth_px: true centres in image pixels."""
        if len(pred) < 3:
            raise ValueError("Need at least 3 located targets to calibrate")
        tfx = [t[0] / w for t in truth_px]
        tfy = [t[1] / h for t in truth_px]
        best = None
        for conv in CONVENTIONS:
            fx = [to_fraction(conv, p[0], w) for p in pred]
            fy = [to_fraction(conv, p[1], h) for p in pred]
            raw_err = sum(abs(a - b) * w + abs(c - d) * h for a, b, c, d in zip(fx, tfx, fy, tfy)) / len(pred)
            if best is None or raw_err < best[1]:
                best = (conv, raw_err, fx, fy)
        conv, raw_err, fx, fy = best
        kx, cx = _fit(fx, tfx)
        ky, cy = _fit(fy, tfy)
        # Only accept a correction that is a modest adjustment; otherwise keep identity.
        if not (0.8 < kx < 1.25 and 0.8 < ky < 1.25 and abs(cx) < 0.15 and abs(cy) < 0.15):
            kx, cx, ky, cy = 1.0, 0.0, 1.0, 0.0
        errs = []
        for a, b, c, d in zip(fx, tfx, fy, tfy):
            ex = (a * kx + cx - b) * w
            ey = (c * ky + cy - d) * h
            errs.append((ex * ex + ey * ey) ** 0.5)
        result = {"convention": conv, "kx": kx, "cx": cx, "ky": ky, "cy": cy,
                  "error_px": round(sum(errs) / len(errs), 1), "raw_error_px": round(raw_err, 1),
                  "image_size": [w, h], "points": len(pred)}
        self.data[model_id] = result
        self.save()
        return result

    def clear(self, model_id: str) -> None:
        self.data.pop(model_id, None)
        self.save()


def parse_points(obj, labels: str) -> dict[str, tuple[float, float]]:
    """Accept {"A":[x,y]}, {"A":{"x":..,"y":..}}, [{"label":"A","x":..,"y":..}], bbox [x1,y1,x2,y2]."""
    out: dict[str, tuple[float, float]] = {}

    def to_xy(v):
        if isinstance(v, dict):
            if "x" in v and "y" in v:
                return float(v["x"]), float(v["y"])
            for k in ("point", "center", "coordinates", "bbox", "box", "bbox_2d"):
                if k in v:
                    return to_xy(v[k])
            return None
        if isinstance(v, (list, tuple)):
            if len(v) == 2:
                return float(v[0]), float(v[1])
            if len(v) == 4:
                return (float(v[0]) + float(v[2])) / 2, (float(v[1]) + float(v[3])) / 2
        return None

    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).upper() in labels:
                xy = to_xy(v)
                if xy:
                    out[str(k).upper()] = xy
    elif isinstance(obj, list):
        for it in obj:
            if isinstance(it, dict):
                lab = str(it.get("label") or it.get("letter") or it.get("name") or "").upper()
                if lab in labels:
                    xy = to_xy(it)
                    if xy:
                        out[lab] = xy
    return out
