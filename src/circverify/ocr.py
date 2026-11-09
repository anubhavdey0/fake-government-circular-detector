"""Optional OCR through Tesseract.

Scanned PDFs are still matched without OCR (by file hash and by page image),
but OCR adds a text layer so that a retyped or reprinted copy of a scan can
be found, and so that photos/screenshots get a word-level diff.

Install Tesseract (with the Hindi pack if you want it) and ``pip install
circverify[ocr]``.
"""

from __future__ import annotations

import shutil
from typing import Callable

from PIL import Image

OcrFn = Callable[[Image.Image], str]


def tesseract_available() -> bool:
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def load_ocr(enabled: bool | None = None) -> OcrFn | None:
    """Return an OCR function, or None.

    ``enabled=None`` means "use it if installed"; ``True`` raises if missing.
    """
    if enabled is False:
        return None
    if not tesseract_available():
        if enabled:
            raise RuntimeError(
                "OCR requested but Tesseract is not available. Install the tesseract binary and "
                "`pip install circverify[ocr]`."
            )
        return None
    import pytesseract

    langs = set(pytesseract.get_languages(config=""))
    lang = "+".join(x for x in ("eng", "hin") if x in langs) or "eng"

    def run(image: Image.Image) -> str:
        return pytesseract.image_to_string(image, lang=lang)

    run.engine = f"tesseract:{lang}"  # type: ignore[attr-defined]
    return run
