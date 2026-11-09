"""Turn a PDF or image into pages of text plus a rendered grayscale picture."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Callable, Iterator

import pypdfium2 as pdfium
from PIL import Image, ImageOps

from .imageprep import ImageTooLarge, open_checked, prepare_photo, to_gray

# Pages with fewer extractable characters than this are treated as scans.
MIN_TEXT_CHARS = 25
# Width used for fingerprinting; visual diffs render separately at higher resolution.
HASH_WIDTH = 512
OCR_WIDTH = 2400

Image.MAX_IMAGE_PIXELS = 200_000_000

OcrFn = Callable[[Image.Image], str]


class UnsupportedFile(Exception):
    pass


_WORDLIKE = re.compile(r"^[A-Za-z][a-z]{1,}[.,;:)]?$|^[A-Z]{2,}[.,;:)]?$")
_VOWEL = re.compile(r"[aeiouAEIOU]")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
GARBLED_BELOW = 0.2


def text_quality(text: str) -> float | None:
    """Share of word-like tokens, or None for too little text to judge.

    Some PDFs carry a text layer that cannot be read: fonts without a
    character map, or legacy Hindi encodings such as Kruti Dev that put Latin
    letters where Devanagari should be. Measured on UGC's files, 2.6% of text
    layers score below 0.2 and every one of them is unreadable; tables and
    lists score 0.3 and up.
    """
    toks = [t for t in text.split() if re.search(r"[A-Za-z\u0900-\u097F]", t)]
    if len(toks) < 30:
        return None
    if sum(bool(_DEVANAGARI.search(t)) for t in toks) > 0.3 * len(toks):
        return 1.0
    good = sum(bool(_WORDLIKE.match(t)) and bool(_VOWEL.search(t)) for t in toks)
    return good / len(toks)


def sniff_kind(head: bytes) -> str | None:
    """Identify a supported file type from its first bytes."""
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    return None


@dataclass
class PageData:
    number: int  # 1-based
    text: str
    text_source: str  # "layer" | "ocr" | "none"
    image: Image.Image | None = None  # grayscale ("L")
    prep: dict = field(default_factory=dict)  # what was done to a submitted picture (crop, deskew)


@dataclass
class DocumentInfo:
    kind: str
    n_pages: int
    metadata: dict = field(default_factory=dict)


def _render(page: "pdfium.PdfPage", width: int, *, grayscale: bool = True) -> Image.Image:
    w = page.get_width() or 612
    h = page.get_height() or 792
    scale = width / w
    if h * scale > 6000:  # extremely tall pages: cap memory use
        scale = 6000 / h
    bitmap = page.render(scale=scale, grayscale=grayscale)
    try:
        return bitmap.to_pil().convert("L" if grayscale else "RGB")
    finally:
        bitmap.close()


def _page_text(page: "pdfium.PdfPage") -> str:
    # A /Rotate flag changes the order PDFium reads characters in; extract as if
    # unrotated so a rotated copy of a page yields exactly the same text.
    rotation = page.get_rotation()
    if rotation:
        page.set_rotation(0)
    textpage = page.get_textpage()
    try:
        text = textpage.get_text_bounded() or ""
    finally:
        textpage.close()
        if rotation:
            page.set_rotation(rotation)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def open_pdf(data: bytes) -> "pdfium.PdfDocument":
    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        msg = str(exc)
        if "password" in msg.lower():
            raise UnsupportedFile("the PDF is password-protected") from exc
        raise UnsupportedFile("the PDF is damaged or incomplete") from exc
    if len(doc) == 0:
        doc.close()
        raise UnsupportedFile("the PDF has no pages")
    return doc


def pdf_info(data: bytes) -> DocumentInfo:
    doc = open_pdf(data)
    try:
        try:
            meta = doc.get_metadata_dict(skip_empty=True)
        except Exception:  # noqa: BLE001 - metadata is best effort
            meta = {}
        return DocumentInfo("pdf", len(doc), meta)
    finally:
        doc.close()


def iter_pages(
    data: bytes,
    *,
    kind: str | None = None,
    render_width: int | None = HASH_WIDTH,
    max_pages: int | None = None,
    max_render_pages: int | None = None,
    ocr: OcrFn | None = None,
    photo: bool = False,
    ocr_pages: int | None = None,
) -> Iterator[PageData]:
    """Yield pages one at a time so that large documents never sit in memory at once.

    ``photo=True`` treats an image as a submission: find the page inside it
    and straighten it (see imageprep). Official images are used as they are.
    """
    kind = kind or sniff_kind(data[:2048])
    if kind is None:
        raise UnsupportedFile("not a PDF or a supported image")
    if kind != "pdf":
        yield _image_page(data, render_width, ocr, photo=photo)
        return

    doc = open_pdf(data)
    try:
        total = len(doc)
        limit = total if max_pages is None else min(total, max_pages)
        for i in range(limit):
            page = doc[i]
            try:
                text = _page_text(page)
                source = "layer" if len(text.strip()) >= MIN_TEXT_CHARS else "none"
                if source == "layer":
                    q = text_quality(text)
                    if q is not None and q < GARBLED_BELOW:
                        source = "garbled"  # a text layer that cannot be read: treat as a scan
                if source != "layer" and ocr is not None and (ocr_pages is None or i < ocr_pages):
                    ocr_text = ocr(_render(page, OCR_WIDTH))
                    if ocr_text.strip():
                        text, source = ocr_text, "ocr"
                image = None
                if render_width and (max_render_pages is None or i < max_render_pages):
                    image = _render(page, render_width)
                yield PageData(i + 1, text if source in ("layer", "ocr") else "", source, image)
            finally:
                page.close()
    finally:
        doc.close()


def _image_page(data: bytes, render_width: int | None, ocr: OcrFn | None, *, photo: bool = False) -> PageData:
    prep: dict = {}
    try:
        img = Image.open(io.BytesIO(data))
        open_checked(img)
        img.load()
        if photo:
            gray, prep = prepare_photo(img)
        else:
            gray = to_gray(ImageOps.exif_transpose(img))
    except ImageTooLarge as exc:
        raise UnsupportedFile(f"the image is too large ({exc})") from exc
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types
        raise UnsupportedFile(f"the image could not be opened ({exc})") from exc
    text, source = "", "none"
    if ocr is not None:
        big = gray if gray.width >= 1600 else gray.resize(
            (1600, round(gray.height * 1600 / gray.width)), Image.Resampling.LANCZOS)
        text = ocr(big)
        source = "ocr" if text.strip() else "none"
    image = gray
    if render_width and gray.width != render_width:
        image = gray.resize((render_width, max(1, round(gray.height * render_width / gray.width))),
                            Image.Resampling.LANCZOS)
    return PageData(1, text, source, image, prep)


def render_pdf_page(data: bytes, page_no: int, width: int, *, grayscale: bool = True) -> Image.Image:
    doc = open_pdf(data)
    try:
        page = doc[page_no - 1]
        try:
            return _render(page, width, grayscale=grayscale)
        finally:
            page.close()
    finally:
        doc.close()


def load_image(data: bytes, width: int) -> Image.Image:
    return _image_page(data, width, None).image  # type: ignore[return-value]
