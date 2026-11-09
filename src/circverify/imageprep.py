"""Get a submitted picture into the same shape as a rendered official page.

Forwarded circulars rarely arrive as a clean page image. They are phone
screenshots with black bars above and below, a status bar, a chat
background around a smaller copy, or a slightly crooked scan. This module
converts any image mode to grayscale on white, finds the document inside
the picture, and straightens small skews, so that hashing and the pixel
comparison see the page and nothing else.
"""

from __future__ import annotations

import warnings

import numpy as np
from PIL import Image, ImageOps

# A 600 dpi A4 scan is about 35 MP; a phone screenshot about 3 MP.
MAX_PIXELS = 60_000_000


class ImageTooLarge(Exception):
    pass


def open_checked(img: Image.Image) -> Image.Image:
    """Refuse decompression bombs before any pixel data is decoded."""
    if img.width * img.height > MAX_PIXELS:
        raise ImageTooLarge(f"{img.width}x{img.height} pixels is larger than any document scan")
    return img


def to_gray(img: Image.Image) -> Image.Image:
    """Grayscale on a white background, whatever the input mode."""
    if img.mode in ("I;16", "I;16B", "I;16L", "I;16N", "I"):
        arr = np.asarray(img, dtype=np.float64)
        top = arr.max() or 1.0
        scale = 255.0 / (65535.0 if top > 255 else 255.0)
        return Image.fromarray(np.clip(arr * scale, 0, 255).astype(np.uint8), "L")
    if img.mode == "F":
        arr = np.asarray(img, dtype=np.float64)
        return Image.fromarray(np.clip(arr if arr.max() > 1.5 else arr * 255, 0, 255).astype(np.uint8), "L")
    if img.mode == "P" and "transparency" in img.info:
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        rgba = img.convert("RGBA")
        white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(white, rgba)
    return img.convert("L")


def _runs(flags: np.ndarray, max_gap: int) -> list[tuple[int, int]]:
    """Runs of True, merging runs separated by gaps of at most ``max_gap``."""
    runs: list[list[int]] = []
    start = None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(flags)])
    merged: list[list[int]] = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= max_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [(a, b) for a, b in merged]


def find_document(gray: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of the paper inside a screenshot, or None if the image is already just the page."""
    w, h = gray.size
    scale = 400 / w if w > 400 else 1.0
    small = gray.resize((max(1, round(w * scale)), max(1, round(h * scale)))) if scale != 1.0 else gray
    a = np.asarray(small, dtype=np.float32)
    paper = float(np.percentile(a, 95))
    if paper < 150:  # not a light document on a darker surround
        return None
    mask = a > paper - 28
    # Rows first, then columns measured only within those rows: tall black bars
    # above and below the page must not make every column look half empty.
    rows = _runs(mask.mean(axis=1) > 0.5, max_gap=max(2, int(a.shape[0] * 0.03)))
    if not rows:
        return None
    y0, y1 = max(rows, key=lambda r: r[1] - r[0])
    cols = _runs(mask[y0:y1].mean(axis=0) > 0.5, max_gap=max(2, int(a.shape[1] * 0.03)))
    if not cols:
        return None
    x0, x1 = max(cols, key=lambda r: r[1] - r[0])
    sh, sw = a.shape
    if (y1 - y0) * (x1 - x0) < 0.2 * sh * sw:
        return None
    if (y1 - y0) >= 0.97 * sh and (x1 - x0) >= 0.97 * sw:
        return None
    inv = 1 / scale
    pad_x, pad_y = int(0.004 * w), int(0.004 * h)
    return (max(0, int(x0 * inv) - pad_x), max(0, int(y0 * inv) - pad_y),
            min(w, int(x1 * inv) + pad_x), min(h, int(y1 * inv) + pad_y))


def estimate_skew(gray: Image.Image, limit: float = 4.0, step: float = 0.25) -> float:
    """Small rotation (degrees) that makes text lines horizontal, by maximising row-profile variance."""
    w, h = gray.size
    scale = 700 / w if w > 700 else 1.0
    small = gray.resize((max(1, round(w * scale)), max(1, round(h * scale)))) if scale != 1.0 else gray
    ink = small.point(lambda p: 255 if p < 160 else 0)
    if np.asarray(ink).mean() < 255 * 0.005:
        return 0.0
    best_angle, best_score, base = 0.0, -1.0, None
    for angle in np.arange(-limit, limit + 1e-9, step):
        rows = np.asarray(ink.rotate(float(angle), resample=Image.Resampling.NEAREST), dtype=np.float32).sum(axis=1)
        score = float(rows.var())
        if abs(angle) < 1e-9:
            base = score
        if score > best_score:
            best_angle, best_score = float(angle), score
    if base is None or best_score < base * 1.08:  # no clear improvement: leave the image alone
        return 0.0
    return best_angle


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Top-left, top-right, bottom-right, bottom-left."""
    s, d = pts.sum(axis=1), np.diff(pts, axis=1).ravel()
    return np.float32([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]])


def rectify_document(gray: Image.Image) -> Image.Image | None:
    """A photographed sheet warped flat, as document-scanner apps do; None if no sheet outline is visible.

    Looks for the largest bright four-cornered shape that does not fill the
    whole frame, then maps it onto an upright rectangle.
    """
    try:
        import cv2
    except ImportError:
        return None
    w, h = gray.size
    scale = 900 / max(w, h) if max(w, h) > 900 else 1.0
    small = np.asarray(gray.resize((max(1, round(w * scale)), max(1, round(h * scale)))) if scale != 1 else gray)
    blur = cv2.GaussianBlur(small, (5, 5), 0)
    _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    sheet = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(sheet)
    frame = small.shape[0] * small.shape[1]
    if not (0.25 * frame <= area <= 0.97 * frame):
        return None
    quad = cv2.approxPolyDP(sheet, 0.02 * cv2.arcLength(sheet, True), True)
    if len(quad) != 4 or not cv2.isContourConvex(quad):
        return None
    corners = _order_corners(quad.reshape(4, 2).astype(np.float32) / scale)
    (tl, tr, br, bl) = corners
    out_w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    out_h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    if out_w < 200 or out_h < 200:
        return None
    target = np.float32([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]])
    warped = cv2.warpPerspective(np.asarray(gray), cv2.getPerspectiveTransform(corners, target), (out_w, out_h),
                                 flags=cv2.INTER_CUBIC, borderValue=255)
    return Image.fromarray(warped)


def flatten_lighting(gray: Image.Image) -> Image.Image:
    """Divide out uneven lighting (a phone photo under a tube light) so paper is white everywhere."""
    try:
        import cv2
    except ImportError:
        return gray
    a = np.asarray(gray)
    k = max(15, (min(a.shape) // 25) | 1)
    background = cv2.morphologyEx(a, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    background = cv2.GaussianBlur(background, (0, 0), k / 3)
    flat = np.clip(a.astype(np.float32) / np.maximum(background, 1).astype(np.float32) * 255.0, 0, 255)
    return Image.fromarray(flat.astype(np.uint8))


def prepare_photo(img: Image.Image) -> tuple[Image.Image, dict]:
    """Grayscale, cropped to the document, flattened if photographed, and straightened."""
    open_checked(img)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        gray = to_gray(ImageOps.exif_transpose(img))
    info: dict = {}
    sheet = rectify_document(gray)
    if sheet is not None:
        info["rectified"] = True
        return flatten_lighting(sheet), info
    box = find_document(gray)
    if box:
        gray = gray.crop(box)
        info["cropped"] = box
    angle = estimate_skew(gray)
    if angle:
        gray = gray.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
        info["deskewed"] = angle
        box = find_document(gray)
        if box:
            gray = gray.crop(box)
    return gray, info
