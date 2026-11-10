"""OCR engines, used where a page has no text layer.

Two engines are supported; the first one available is used unless
``CIRCVERIFY_OCR_ENGINE`` (``tesseract`` or ``rapidocr``) says otherwise:

* **Tesseract** (``pip install circverify[ocr]`` plus the Tesseract binary,
  with the ``hin`` pack for Hindi). Fast and the usual choice.
* **RapidOCR** (``pip install rapidocr onnxruntime``). Pure pip, no system
  install; English plus Devanagari models. Much slower on some CPUs.

Scanned PDFs are matched by file hash and page image even without OCR; OCR
adds text so that cropped screenshots, photos of printouts and retyped
copies can be found by their words, and so that the diff can quote them.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Callable

import numpy as np
from PIL import Image

OcrFn = Callable[[Image.Image], str]


class OcrText(str):
    """OCR output: the text, plus the engine's mean confidence (0 to 1) when it reports one."""

    confidence: float | None

    def __new__(cls, text: str, confidence: float | None = None):
        obj = super().__new__(cls, text)
        obj.confidence = None if confidence is None else round(float(confidence), 3)
        return obj


def confidence(text: str) -> float | None:
    return getattr(text, "confidence", None)


def tesseract_available() -> bool:
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def _tesseract() -> OcrFn | None:
    if not tesseract_available():
        return None
    import pytesseract

    langs = set(pytesseract.get_languages(config=""))
    lang = "+".join(x for x in ("eng", "hin") if x in langs) or "eng"

    def run(image: Image.Image) -> str:
        data = pytesseract.image_to_data(image, lang=lang, output_type=pytesseract.Output.DICT)
        lines: dict[tuple, list[str]] = {}
        confs = []
        for i, word in enumerate(data["text"]):
            if not word.strip():
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append(word)
            c = float(data["conf"][i])
            if c >= 0:
                confs.append(c / 100)
        text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
        return OcrText(text, sum(confs) / len(confs) if confs else None)

    run.engine = f"tesseract:{lang}"  # type: ignore[attr-defined]
    return run


def _rapidocr() -> OcrFn | None:
    try:
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
    except ImportError:
        return None
    logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    langs = os.environ.get("CIRCVERIFY_OCR_LANGS", "en").split(",")
    threads = int(os.environ.get("CIRCVERIFY_OCR_THREADS", "2"))
    common = {"Det.ocr_version": OCRVersion.PPOCRV5, "Det.model_type": ModelType.MOBILE,
              "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE, "Global.use_cls": False,
              "EngineConfig.onnxruntime.intra_op_num_threads": threads}
    engines = []
    if "en" in langs:
        engines.append(RapidOCR(params={**common, "Rec.lang_type": LangRec.EN}))
    if "hi" in langs:
        engines.append(RapidOCR(params={**common, "Rec.lang_type": LangRec.DEVANAGARI}))

    def run(image: Image.Image) -> str:
        arr = np.asarray(image.convert("RGB"))
        best, best_conf = "", None
        for eng in engines:
            out = eng(arr)
            text = "\n".join(out.txts or [])
            if len(text) > len(best):
                scores = list(out.scores or [])
                best, best_conf = text, (sum(scores) / len(scores) if scores else None)
        return OcrText(best, best_conf)

    run.engine = f"rapidocr:{'+'.join(langs)}"  # type: ignore[attr-defined]
    return run


_ENGINES = {"tesseract": _tesseract, "rapidocr": _rapidocr}


def available_engine() -> str | None:
    """Name of the engine ``load_ocr`` would use, without loading any model."""
    choice = os.environ.get("CIRCVERIFY_OCR_ENGINE", "auto")
    if choice in ("tesseract", "auto") and tesseract_available():
        return "tesseract"
    if choice in ("rapidocr", "auto"):
        try:
            import rapidocr  # noqa: F401
            return "rapidocr"
        except ImportError:
            return None
    return None


def load_ocr(enabled: bool | None = None, engine: str | None = None) -> OcrFn | None:
    """An OCR function, or None.

    ``enabled=None`` means "use it if installed"; ``True`` raises if no engine is available.
    """
    if enabled is False:
        return None
    choice = engine or os.environ.get("CIRCVERIFY_OCR_ENGINE", "auto")
    if choice == "auto":
        # Automatic use is limited to the fast engine; RapidOCR can take tens of
        # seconds a page on some CPUs, so it runs only when asked for by name or
        # when OCR is explicitly requested and nothing faster is installed.
        order = ["tesseract", "rapidocr"] if enabled else ["tesseract"]
    else:
        order = [choice]
    for name in order:
        loader = _ENGINES.get(name)
        fn = loader() if loader else None
        if fn is not None:
            return fn
    if enabled:
        raise RuntimeError(
            "OCR requested but no engine is available. Install Tesseract and `pip install circverify[ocr]`, "
            "or `pip install rapidocr onnxruntime`."
        )
    return None
