"""A tiny PDF writer for tests: Helvetica text lines, one list of lines per page."""

from __future__ import annotations


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]], *, producer: str = "pdfgen", font_size: int = 12,
             leading: int = 18, top: int = 760, left: int = 72,
             white_boxes: dict[int, list[tuple[float, float, float, float]]] | None = None) -> bytes:
    """``white_boxes`` maps a page index to (x, y, w, h) rectangles in points, painted over the text."""
    objs: list[bytes] = [b"", b""]  # 1: catalog, 2: page tree (filled in below)
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    font_id = len(objs)
    page_ids = []
    for page_index, lines in enumerate(pages):
        ops = f"BT /F1 {font_size} Tf {leading} TL {left} {top} Td " + "".join(f"({_esc(l)}) Tj T* " for l in lines) + "ET"
        for x, y, w, h in (white_boxes or {}).get(page_index, []):
            ops += f" q 1 g {x} {y} {w} {h} re f Q"
        stream = ops.encode("latin-1")
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_id = len(objs)
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        page_ids.append(len(objs))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(page_ids)} >>".encode()
    objs.append(f"<< /Producer ({_esc(producer)}) >>".encode())
    info_id = len(objs)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {info_id} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def line_box(line_index: int, width_px: int, *, top: int = 760, leading: int = 18, font_size: int = 12,
             left: int = 72, right: int = 560) -> tuple[int, int, int, int]:
    """Pixel box around a text line when the 612x792 pt page is rendered ``width_px`` wide."""
    s = width_px / 612
    baseline_from_top = 792 - (top - line_index * leading)
    return (int(left * s) - 2, int((baseline_from_top - font_size) * s) - 2,
            int(right * s), int((baseline_from_top + 4) * s))
