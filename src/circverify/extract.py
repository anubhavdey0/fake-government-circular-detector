"""Turn a PDF or image into pages of text plus a rendered grayscale picture."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Callable, Iterator

import pypdfium2 as pdfium
from PIL import Image, ImageOps

# Pages with fewer extractable characters than this are treated as scans.
MIN_TEXT_CHARS = 25
# Width used for fingerprinting; visual diffs render separately at higher resolution.
HASH_WIDTH = 512
OCR_WIDTH = 2400

Image.MAX_IMAGE_PIXELS = 200_000_000

OcrFn = Callable[[Image.Image], str]


class UnsupportedFile(Exception):
    pass


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
    textpage = page.get_textpage()
    try:
        text = textpage.get_text_bounded() or ""
    finally:
        textpage.close()
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def open_pdf(data: bytes) -> "pdfium.PdfDocument":
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise UnsupportedFile(f"cannot open PDF: {exc}") from exc


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
) -> Iterator[PageData]:
    """Yield pages one at a time so that large documents never sit in memory at once."""
    kind = kind or sniff_kind(data[:2048])
    if kind is None:
        raise UnsupportedFile("not a PDF or a supported image")
    if kind != "pdf":
        yield _image_page(data, render_width, ocr)
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
                if source == "none" and ocr is not None:
                    ocr_text = ocr(_render(page, OCR_WIDTH))
                    if ocr_text.strip():
                        text, source = ocr_text, "ocr"
                image = None
                if render_width and (max_render_pages is None or i < max_render_pages):
                    image = _render(page, render_width)
                yield PageData(i + 1, text if source != "none" else "", source, image)
            finally:
                page.close()
    finally:
        doc.close()


def _image_page(data: bytes, render_width: int | None, ocr: OcrFn | None) -> PageData:
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        img.load()
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types
        raise UnsupportedFile(f"cannot open image: {exc}") from exc
    gray = img.convert("L")
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
    return PageData(1, text, source, image)


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
