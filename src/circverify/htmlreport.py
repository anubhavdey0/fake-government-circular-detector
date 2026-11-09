"""HTML for the web page, the search page and the standalone report.

One renderer for all three, so a downloaded report looks exactly like the
page. The web page loads ``static/app.css`` and ``static/app.js``; the report
inlines them (fonts included) so it opens offline. Nothing is ever loaded
from another site, and a checked document never leaves the machine.
"""

from __future__ import annotations

import base64
import io
import re
from datetime import datetime, timezone
from functools import lru_cache
from html import escape
from importlib.resources import files
from typing import Callable

from PIL import Image

from .config import PROJECT_URL
from .icons import icon
from .matcher import ALTERED, KNOWN_FAKE, NO_MATCH, UNREADABLE, VERIFIED_CONTENT, VERIFIED_EXACT, VerificationResult
from .signatures import describe as describe_signature

VERDICTS = {
    VERIFIED_EXACT: ("Verified", "Exact copy of an official file", "ok", "seal-check"),
    VERIFIED_CONTENT: ("Verified", "Same content as an official file", "ok", "seal-check"),
    ALTERED: ("Altered", "Differs from the official file", "bad", "seal-warning"),
    KNOWN_FAKE: ("Known fake", "Publicly debunked as fake", "bad", "warning-octagon"),
    NO_MATCH: ("Not found", "No official document matches", "warn", "file-dashed"),
    UNREADABLE: ("Unreadable", "Could not open the file", "neutral", "file-x"),
}
ISSUER_NAMES = {"ugc": "University Grants Commission"}
SOURCE_NAMES = {"notices": "Notices", "circulars": "Circulars", "press-releases": "Press releases",
                "regulations": "Regulations", "guidelines": "Guidelines", "archive": "Archive (no longer listed)"}
DISCLAIMER = ("Unofficial tool, not affiliated with the University Grants Commission. It says \u201cfake\u201d "
              "only when a document matches a public debunk.")
SEARCH_SUGGESTIONS = ["fake universities", "fee refund", "anti-ragging", "fellowship", "academic calendar",
                      "Ph.D. regulations"]

ThumbUrl = Callable[[str, int], str]


# -- assets -----------------------------------------------------------------

def _static(name: str) -> bytes:
    return (files("circverify") / "static" / name).read_bytes()


@lru_cache(maxsize=None)
def _inline_css() -> str:
    css = _static("app.css").decode("utf-8")
    for font in ("Geist-Variable.woff2", "GeistMono-Variable.woff2"):
        data = base64.b64encode(_static(f"fonts/{font}")).decode()
        css = css.replace(f'url("fonts/{font}")', f'url("data:font/woff2;base64,{data}")')
    return css


@lru_cache(maxsize=None)
def _inline_js() -> str:
    return _static("app.js").decode("utf-8")


# -- small helpers ------------------------------------------------------------

def _png_data_url(img: Image.Image, width: int = 1000) -> str:
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert("L").save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def human_date(iso: str | None) -> str:
    if not iso:
        return "date not listed"
    try:
        return datetime.fromisoformat(iso[:19]).strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return iso[:10]


def index_status(info: dict) -> str:
    n = info.get("files_indexed", 0)
    if not n:
        return "Index is empty"
    return f"{n:,} documents, synced {human_date(info.get('last_listing_sync'))}"


def _padded(box, size, pad: int = 10):
    w, h = size
    x0, y0, x1, y1 = box
    return max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)


def _pct_style(box, size) -> str:
    w, h = size
    x0, y0, x1, y1 = box
    return f"left:{x0 / w:.3%};top:{y0 / h:.3%};width:{(x1 - x0) / w:.3%};height:{(y1 - y0) / h:.3%}"


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


# -- result -----------------------------------------------------------------

def _diffs(changes) -> str:
    items = []
    for c in changes[:200]:
        before = " ".join(c.context_before.split()[-8:])
        after = " ".join(c.context_after.split()[:8])

        def row(who: str, text: str, cls: str, missing: str) -> str:
            middle = f'<mark class="{cls}">{escape(text)}</mark>' if text else f'<span class="gap">{missing}</span>'
            return (f'<div class="diff-row"><span class="diff-who mono">{who}</span><p class="diff-text">'
                    f'<span class="ctx">…{escape(before)}</span> {middle} <span class="ctx">{escape(after)}…</span>'
                    f"</p></div>")

        why = "".join(f'<span class="why high">{escape(r)}</span>' for r in c.reasons) \
            or '<span class="why">wording</span>'
        items.append(
            '<li class="diff">'
            + row("Official", c.official, "hl-off", "nothing here")
            + row("Submission", c.submitted, "hl-sub", "removed")
            + f'<p class="diff-why">{why}</p></li>'
        )
    more = len(changes) - 200
    extra = f'<p class="results-meta mono">{more} more in the JSON output.</p>' if more > 0 else ""
    return (f'<section aria-labelledby="h-text"><h3 id="h-text">What changed in the text'
            f'<span class="count">{len(changes)}</span></h3><ol class="diffs">{"".join(items)}</ol>{extra}</section>')


def _viewer(v) -> str:
    w, h = v.official_size
    boxes = v.boxes if v.same_document else []
    marks = "".join(f'<span class="v-mark" style="{_pct_style(_padded(b, v.official_size), v.official_size)}"></span>'
                    for b in boxes)
    if not v.same_document:
        caption = "This page does not line up with the official page, so it was not compared region by region."
    elif boxes:
        caption = (f"{_plural(len(boxes), 'changed region')} marked in red, covering {v.changed_ratio:.1%} of the "
                   "text on the page. Drag across the page to compare.")
    else:
        caption = "No visible differences from the official page. Drag across the page to compare."
    name = f"mode-{v.query_page}-{v.official_page}"
    return f"""
<figure class="viewer" data-mode="swipe" style="--pos:50%">
  <div class="viewer-labels mono"><span class="lab-off">Official, page {v.official_page}</span>
    <span class="lab-sub">Submission, page {v.query_page}</span></div>
  <div class="viewer-stage">
    <img class="v-off" src="{_png_data_url(v.official_image)}" alt="Official page {v.official_page}" width="{w}" height="{h}">
    <img class="v-sub" src="{_png_data_url(v.query_image)}" alt="Submitted page {v.query_page}, aligned to the official page" width="{w}" height="{h}">
    {marks}<span class="v-handle" aria-hidden="true"></span>
  </div>
  <div class="viewer-controls">
    <div class="seg" role="radiogroup" aria-label="Show">
      <label><input type="radio" name="{name}" value="swipe" checked><span>Swipe</span></label>
      <label><input type="radio" name="{name}" value="official"><span>Official</span></label>
      <label><input type="radio" name="{name}" value="submission"><span>Submission</span></label>
    </div>
    <label class="v-range"><span class="mono" aria-hidden="true">Official</span>
      <input type="range" min="0" max="100" value="50" aria-label="Swipe position">
      <span class="mono" aria-hidden="true">Submission</span></label>
  </div>
  <figcaption>{escape(caption)}</figcaption>
</figure>"""


def _crop_url(img: Image.Image, box, size) -> tuple[str, int, int]:
    w, h = size
    sx, sy = img.width / w, img.height / h
    x0, y0, x1, y1 = box
    cx, cy = max(180, x1 - x0), max(36, y1 - y0)
    crop = (max(0, int((x0 - cx) * sx)), max(0, int((y0 - cy) * sy)),
            min(img.width, int((x1 + cx) * sx)), min(img.height, int((y1 + cy) * sy)))
    piece = img.crop(crop)
    piece = piece.resize((720, max(1, round(piece.height * 720 / piece.width))), Image.Resampling.LANCZOS)
    return _png_data_url(piece, 720), piece.width, piece.height


def _zooms(visual) -> str:
    figs = []
    regions = [(v, b) for v in visual if v.same_document and v.official_image is not None for b in v.boxes][:6]
    for i, (v, box) in enumerate(regions, 1):
        tag = f" {i}" if len(regions) > 1 else ""
        for who, img, alt in (("Official", v.official_image, "Official"), ("Submission", v.query_image, "Submitted")):
            url, w, h = _crop_url(img, box, v.official_size)
            figs.append(f'<figure><figcaption class="mono">{who}{tag}, page {v.official_page}</figcaption>'
                        f'<img src="{url}" alt="{alt} page {v.official_page}, region{tag}, enlarged" width="{w}" '
                        f'height="{h}"></figure>')
    if not figs:
        return ""
    return (f'<section aria-labelledby="h-zoom"><h3 id="h-zoom">Changed {"regions" if len(regions) > 1 else "region"}, '
            f'enlarged</h3><div class="zooms">{"".join(figs)}</div></section>')


def _case_actions(interactive: bool, token: str | None = None) -> str:
    if not interactive:
        return ""
    receipt = ""
    if token:
        receipt = (f'<button class="btn btn-quiet js-only" type="button" data-action="receipt" data-token="{token}">'
                   f'{icon("share-network")}<span class="btn-label">Share a receipt</span></button>'
                   '<p class="receipt-link mono" data-receipt-out aria-live="polite"></p>')
    return (f'<div class="case-actions"><a class="btn btn-primary" href="/" data-action="reset">'
            f'{icon("arrow-counter-clockwise")}Check another</a>'
            f'<button class="btn btn-quiet js-only" type="button" data-action="report">{icon("download-simple")}'
            f'<span class="btn-label">Download report</span></button>{receipt}</div>')


def _source_panel(res: VerificationResult, interactive: bool, token: str | None = None) -> str:
    m = res.match
    p = m.primary
    facts = [("Published", f'<span class="mono">{escape(human_date(p.published))}</span>'),
             ("Listed under", escape(SOURCE_NAMES.get(p.source, p.source)))]
    if p.label:
        facts.append(("File", escape(p.label)))
    if m.n_pages:
        facts.append(("Pages", f'<span class="mono">{m.n_pages}</span>'))
    if len(m.provenance) > 1:
        facts.append(("Also listed", _plural(len(m.provenance) - 1, "more entry", "more entries")))
    if res.withdrawn:
        facts.append(("Withdrawn", f'<span class="mono">{escape(human_date(res.withdrawn))}</span>'))
    sha = m.sha256
    return f"""
<aside class="case-source" aria-label="Official source">
  <p class="label">Official source</p>
  <p class="issuer">{icon("seal-check")}{escape(ISSUER_NAMES.get(p.issuer, p.issuer.upper()))}</p>
  <h3 class="source-title">{escape(p.title)}</h3>
  <dl class="facts">{"".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)}</dl>
  <div class="source-actions">
    <a class="btn btn-secondary" href="{escape(p.url)}" target="_blank" rel="noopener noreferrer">Open the official file{icon("arrow-square-out")}</a>
  </div>
  <div class="hash">
    <div class="hash-text"><span class="hash-label mono">SHA-256 of the official file</span>
      <span class="hash-value mono" title="{sha}" translate="no">{sha[:20]}…{sha[-8:]}</span></div>
    <button class="icon-btn js-only" type="button" data-copy="{sha}" aria-label="Copy the SHA-256">{icon("copy")}</button>
    <span class="sr-only" data-copy-status aria-live="polite"></span>
  </div>
  {_case_actions(interactive, token)}
</aside>"""


def _read_fields(res: VerificationResult) -> str:
    f = res.fields or {}
    rows = [(label, f.get(key)) for key, label in (("ref_no", "Reference"), ("doc_date", "Dated"),
                                                   ("subject", "Subject"), ("signatory", "Signed by"))]
    rows = [(k, v) for k, v in rows if v]
    if not rows:
        return ""
    body = "".join(f"<div><dt>{k}</dt><dd>{escape(human_date(v) if k == 'Dated' else v)}</dd></div>" for k, v in rows)
    return f'<p class="label read-label">Read from the document</p><dl class="facts">{body}</dl>'


def _debunk_panel(res: VerificationResult, interactive: bool, token: str | None = None) -> str:
    fake = res.known_fake or {}
    items = []
    for d in fake.get("debunks", []):
        claim = f'<p class="debunk-claim">{escape(d["claim"])}</p>' if d.get("claim") else ""
        items.append(f'<li><p class="debunk-who">{escape(d.get("source") or "Fact-checker")}'
                     f' <span class="mono">{escape(human_date(d.get("published")))}</span></p>{claim}'
                     f'<a class="btn btn-secondary" href="{escape(d["url"])}" target="_blank" rel="noopener noreferrer">'
                     f'See the debunk{icon("arrow-square-out")}</a></li>')
    how = "This exact file" if fake.get("exact") else f"A {fake.get('strength', 0):.0%} match"
    return f"""
<aside class="case-source debunk" aria-label="Debunk">
  <p class="label">Debunked</p>
  <p class="issuer">{icon("warning-octagon")}{how}</p>
  <ul class="debunks">{"".join(items)}</ul>
  {_case_actions(interactive, token)}
</aside>"""


def _next_steps(res: VerificationResult, interactive: bool, token: str | None = None) -> str:
    if res.verdict == UNREADABLE:
        steps = ["Try the original PDF, or a PNG or JPEG screenshot of it.",
                 "Password-protected or damaged files cannot be opened."]
    else:
        steps = ["Treat it as unverified until you find it on ugc.gov.in.",
                 "Search the index for the real notice on this topic.",
                 "If it was issued in the last few days, refresh the index (circverify crawl) and check again."]
    search = ""
    if interactive and res.verdict == NO_MATCH:
        search = f"""
  <form class="source-search" action="/search" method="get" role="search">
    <label for="ns-q">Search the index</label>
    <div class="search-row"><input class="field" id="ns-q" name="q" type="search" value="{escape(res.search_hint)}"
      placeholder="e.g. fee refund…" autocomplete="off" spellcheck="false">
      <button class="btn btn-secondary" type="submit">{icon("magnifying-glass")}<span class="sr-only">Search</span></button></div>
  </form>"""
    return f"""
<aside class="case-source" aria-label="What to do next">
  <p class="label">What to do next</p>
  <ol class="next-steps">{"".join(f"<li>{escape(s)}</li>" for s in steps)}</ol>{search}
  {_read_fields(res)}
  {_case_actions(interactive, token)}
</aside>"""


def render_result(res: VerificationResult, *, interactive: bool = True, thumb_url: ThumbUrl | None = None,
                  token: str | None = None) -> str:
    label, kind, tone, icon_name = VERDICTS[res.verdict]
    if res.withdrawn and res.verdict in (VERIFIED_EXACT, VERIFIED_CONTENT):
        kind = f"Real, but withdrawn from ugc.gov.in on {human_date(res.withdrawn)}"
    m = res.match
    bits = [escape(res.filename or "Uploaded file")]
    if res.kind:
        bits.append(escape(res.kind.upper()))
    if res.n_pages:
        bits.append(_plural(res.n_pages, "page"))
    file_icon = icon({"pdf": "file-pdf", "text": "clipboard-text"}.get(res.kind or "", "file-image"))
    head = f"""
<header class="case-head">
  <div class="case-verdict"><span class="seal">{icon(icon_name)}</span>
    <div><h2 class="verdict-word" id="verdict-title">{label}</h2><p class="verdict-kind">{kind}</p></div></div>
  <p class="case-summary">{escape(res.summary)}</p>
  <p class="case-file mono">{file_icon}{" · ".join(bits)}</p>
</header>"""

    evidence = []
    if res.signatures:
        sigs = "".join(f'<li>{escape(describe_signature(s))}</li>' for s in res.signatures)
        evidence.append(f'<section aria-labelledby="h-sig"><h3 id="h-sig">{icon("signature")}Digital signature</h3>'
                        f'<ul class="notes">{sigs}</ul></section>')
    if m and m.text and m.text.changes:
        evidence.append(_diffs(m.text.changes))
    shown = [v for v in (m.visual if m else []) if v.official_image is not None and v.query_image is not None]
    if shown:
        evidence.append('<section aria-labelledby="h-pages"><h3 id="h-pages">Page comparison</h3>'
                        + "".join(_viewer(v) for v in shown) + "</section>")
        evidence.append(_zooms(shown))
    elif m and thumb_url and res.verdict in (VERIFIED_EXACT, VERIFIED_CONTENT):
        evidence.append(
            f'<section aria-labelledby="h-sheet"><h3 id="h-sheet">The official document</h3>'
            f'<figure class="official-sheet"><img src="{escape(thumb_url(m.sha256, 1000))}" alt="First page of the '
            f'official file" width="1000" height="1414" loading="lazy"><figcaption>First page of the file '
            f"on ugc.gov.in.</figcaption></figure></section>")
    if res.signal_notes:
        evidence.append('<section aria-labelledby="h-signals"><h3 id="h-signals">Signals, not a verdict</h3>'
                        '<p class="section-lede">Hints about where the document may have come from. A genuine '
                        'notice can show some of these too.</p><ul class="notes">'
                        + "".join(f"<li>{escape(n)}</li>" for n in res.signal_notes) + "</ul></section>")
    if res.notes:
        evidence.append('<section aria-labelledby="h-notes"><h3 id="h-notes">Notes</h3><ul class="notes">'
                        + "".join(f"<li>{escape(n)}</li>" for n in res.notes) + "</ul></section>")
    if res.alternatives:
        items = "".join(
            f'<li><a href="{escape(a["url"] or "#")}" target="_blank" rel="noopener noreferrer">'
            f'{escape(a["title"] or a["sha256"][:12])}</a> <span class="mono">{escape(human_date(a["published"]))}'
            f"</span></li>" for a in res.alternatives)
        evidence.append(f'<section aria-labelledby="h-alt"><h3 id="h-alt">Other close official documents</h3>'
                        f'<ul class="alternatives">{items}</ul></section>')

    tech = [("File SHA-256", f'<span class="mono" translate="no">{res.sha256}</span>')]
    if m:
        if m.text_containment is not None and res.verdict != VERIFIED_EXACT:
            tech.append(("Text overlap", f"{m.text_containment:.1%} of the submission's word sequences"))
        if m.text is not None:
            tech.append(("Text similarity", f"{m.text.ratio:.1%}"))
    tech.append(("Text source", {"layer": "Text layer" if res.kind != "text" else "Pasted text", "ocr": "OCR",
                                 "none": "None, compared by page image"}
                 .get(res.text_source or "", "Not needed (exact file match)")))
    for key, name in (("producer", "Written by"), ("created", "Created"), ("modified", "Modified")):
        if res.signals.get(key):
            tech.append((name, escape(str(res.signals[key]))))
    tech.append(("Index", escape(index_status(res.index))))
    evidence.append('<details class="tech"><summary>Technical details</summary><dl>'
                    + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in tech) + "</dl></details>")

    if res.verdict == KNOWN_FAKE:
        aside = _debunk_panel(res, interactive, token)
    elif m and m.provenance:
        aside = _source_panel(res, interactive, token)
    else:
        aside = _next_steps(res, interactive, token)
    return (f'<section class="case tone-{tone}" aria-labelledby="verdict-title">{head}'
            f'<div class="case-body"><div class="evidence">{"".join(evidence)}</div>{aside}</div></section>')


def _skeleton() -> str:
    return ('<div class="skeleton" aria-hidden="true"><div class="sk-head"><div class="sk sk-seal"></div>'
            '<div class="sk sk-word"></div></div><div class="sk-body"><div><div class="sk sk-line sk-w72"></div>'
            '<div class="sk sk-line sk-w54"></div><div class="sk sk-sheet"></div></div>'
            '<div class="sk sk-panel"></div></div></div>')


# -- pages --------------------------------------------------------------------

def _shell(title: str, body: str, *, info: dict, active: str = "", inline: bool = False, body_class: str = "",
           status: bool = True, description: str = "") -> str:
    if inline:
        assets = f"<style>{_inline_css()}</style>"
        script = f"<script>{_inline_js()}</script>"
        brand = f'<span class="brand" translate="no">{icon("seal-check")}circverify</span>'
        nav = ""
    else:
        assets = ('<link rel="preload" href="/static/fonts/Geist-Variable.woff2" as="font" type="font/woff2" crossorigin>\n'
                  '<link rel="stylesheet" href="/static/app.css">\n'
                  '<link rel="icon" href="/favicon.svg" type="image/svg+xml">')
        script = '<script src="/static/app.js" defer></script>'
        brand = f'<a class="brand" href="/" translate="no">{icon("seal-check")}circverify</a>'
        cur = ' aria-current="page"'
        nav = (f'<nav class="nav" aria-label="Main"><a href="/"{cur if active == "check" else ""}>Check</a>'
               f'<a href="/search"{cur if active == "search" else ""}>Search</a></nav>')
    status_html = f'<p class="topbar-status mono">{escape(index_status(info))}</p>' if status else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{escape(description or 'Check a circular against every document the University Grants Commission has published.')}">
<meta name="theme-color" content="#eceef1" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0e1114" media="(prefers-color-scheme: dark)">
<title>{escape(title)}</title>
{assets}
</head>
<body class="{body_class}">
<a class="skip" href="#main">Skip to content</a>
<header class="topbar"><div class="shell">{brand}{nav}{status_html}</div></header>
<main id="main">{body}</main>
<footer class="footer"><div class="shell">
<p>Documents are checked on this computer and are not kept. {escape(DISCLAIMER)}</p>
<p><a href="{PROJECT_URL}" rel="noopener noreferrer">Source code</a></p>
</div></footer>
{script}
</body>
</html>"""


def render_page(index_info: dict, result: VerificationResult | None = None, *, error: str | None = None,
                max_bytes: int = 0, sources: dict | None = None, recent: list | None = None,
                thumb_url: ThumbUrl | None = None, token: str | None = None) -> str:
    """The home page: drop zone, the index at a glance, latest documents. With ``result``, the case file."""
    n = index_info.get("files_indexed", 0)
    empty = not n
    disabled = " disabled" if empty else ""
    if empty and not error:
        error = "The index is empty. Run circverify crawl, then reload this page."
    mb = max_bytes // 1048576 or 30

    source_rows = "".join(
        f'<div><dt>{escape(SOURCE_NAMES.get(k.split("/")[-1], k))}</dt><dd>{v:,}</dd></div>'
        for k, v in sorted((sources or {}).items(), key=lambda kv: -kv[1]))
    panel = ""
    if not empty:
        panel = f"""
  <aside class="index-panel" aria-labelledby="index-h">
    <p class="label" id="index-h">The index</p>
    <p class="bignum">{n:,}</p>
    <p class="bignum-label">official UGC documents, {index_info.get("pages", 0):,} pages, every word searchable.</p>
    {f'<dl class="sources">{source_rows}</dl>' if source_rows else ""}
    <p class="synced mono">Synced with ugc.gov.in on {escape(human_date(index_info.get("last_listing_sync")))}.
      <a href="/coverage">Coverage and blind spots</a></p>
  </aside>"""

    shelf = ""
    if recent and thumb_url:
        cards = "".join(f"""
    <li><a class="sheet-link" href="{escape(r['url'])}" target="_blank" rel="noopener noreferrer">
      <span class="sheet-thumb"><img src="{escape(thumb_url(r['sha256'], 360))}" alt="First page of {escape(r['title'][:90])}"
        width="360" height="509" loading="lazy"></span>
      <span class="shelf-date mono">{escape(human_date(r['published']))}</span>
      <span class="shelf-title">{escape(r['title'])}</span></a></li>""" for r in recent)
        shelf = f"""
<section class="shelf-section shell" aria-labelledby="shelf-h">
  <div class="section-head"><h2 id="shelf-h">Latest from UGC</h2>
    <span class="head-links"><a href="/feed.xml">{icon("rss-simple")}Feed of new notices</a>
    <a href="https://www.ugc.gov.in/Notices" target="_blank" rel="noopener noreferrer">All notices on ugc.gov.in</a></span></div>
  <ol class="shelf">{cards}</ol>
</section>"""

    legend_items = [
        ("tone-ok", "seal-check", "Verified: exact copy", "Byte for byte, a file UGC published. You get the link to it."),
        ("tone-ok", "seal-check", "Verified: same content",
         "The same text or page as an official file, saved or screenshotted differently."),
        ("tone-bad", "seal-warning", "Altered",
         "An official document with something changed. Every change is shown next to the original."),
        ("tone-warn", "file-dashed", "Not found", "Nothing UGC has published matches it. Treat it as unverified."),
    ]
    legend = f"""
<section class="legend shell" aria-labelledby="legend-h">
  <h2 id="legend-h">Four possible answers</h2>
  <dl class="legend-grid">{"".join(
        f'<div class="{t}"><dt>{icon(i)}{escape(term)}</dt><dd>{escape(desc)}</dd></div>'
        for t, i, term, desc in legend_items)}</dl>
</section>"""

    result_html = render_result(result, thumb_url=thumb_url, token=token) if result else ""
    paste = f"""
    <details class="paste">
      <summary>{icon("clipboard-text")}No file? Paste the text of the message instead</summary>
      <form id="text-form" method="post" action="/verify-text">
        <label for="text">Text of the circular</label>
        <textarea class="field" id="text" name="text" rows="6" required minlength="60"
          placeholder="Paste two or three sentences or more\u2026"{disabled}></textarea>
        <div class="actions"><button class="btn btn-primary" type="submit"{disabled}>Check text</button></div>
      </form>
    </details>"""
    body = f"""
<div class="shell">
<section class="home">
  <div class="home-main">
    <div class="home-intro">
      <h1>Check a circular before you forward it.</h1>
      <p class="lede">It is compared with every document the University Grants Commission has published, down to the
      changed word.</p>
    </div>
    <form id="check-form" class="tray" method="post" action="/verify" enctype="multipart/form-data"
          data-max-bytes="{max_bytes}" data-indexed="{n:,} documents">
      <input id="file" class="tray-input" name="file" type="file" required{disabled}
             accept=".pdf,.png,.jpg,.jpeg,.webp,.gif,.tif,.tiff,application/pdf,image/*"
             aria-describedby="tray-hint file-error">
      <label class="tray-zone" for="file">
        <span class="tray-icon">{icon("file-arrow-up")}</span>
        <span class="tray-title">Drop a PDF or screenshot</span>
        <span class="tray-sub">or <span class="tray-browse">choose a file</span>, or paste an image</span>
        <span class="tray-hint mono" id="tray-hint">PDF, PNG, JPEG, WebP · up to {mb}&nbsp;MB</span>
      </label>
      <p class="tray-status" id="tray-status" aria-live="polite"></p>
      <p class="error" id="file-error" role="alert">{escape(error or "")}</p>
      <div class="no-js-only"><button class="btn btn-primary" type="submit"{disabled}>Check document</button></div>
    </form>{paste}
  </div>{panel}
</section>
<div id="result">{result_html}</div>
</div>{shelf if not result else ""}{legend if not result else ""}
<template id="skeleton-tpl">{_skeleton()}</template>"""
    return _shell("Check a circular · circverify", body, info=index_info, active="check",
                  body_class="has-result" if result else "", status=bool(result))


def render_search(index_info: dict, query: str, hits: list[dict]) -> str:
    q = escape(query)
    if not query:
        results = ('<p class="empty-note">Try a few words from the notice you are looking for.</p><div class="suggest">'
                   + "".join(f'<a href="/search?q={escape(s)}">{escape(s)}</a>' for s in SEARCH_SUGGESTIONS)
                   + "</div>")
    elif not hits:
        results = (f'<p class="empty-note">Nothing in the index mentions “{q}”. Check the spelling, or use fewer, '
                   "more distinctive words.</p>")
    else:
        rows = "".join(f"""
  <li class="hit"><a class="hit-title" href="{escape(h['url'])}" target="_blank" rel="noopener noreferrer">{escape(h['title'])}</a>
    <p class="hit-meta mono">{escape(human_date(h['published']))} · {escape(SOURCE_NAMES.get(h['source'], h['source']))} · page {h['page_no']}</p>
    <p class="hit-snippet">{h['snippet_html']}</p></li>""" for h in hits)
        results = (f'<p class="results-meta mono">{_plural(len(hits), "document")} mention “{q}”</p>'
                   f'<ol class="hits">{rows}</ol>')
    body = f"""
<div class="shell">
  <header class="page-head">
    <h1>Search the index</h1>
    <p class="lede">Find the real notice on a topic. Every page of every indexed UGC document is searchable.</p>
    <form class="search-form" action="/search" method="get" role="search">
      <label for="q">Words to look for</label>
      <div class="search-row"><input class="field" id="q" name="q" type="search" value="{q}"
        placeholder="e.g. fee refund policy…" autocomplete="off" spellcheck="false">
        <button class="btn btn-primary" type="submit">{icon("magnifying-glass")}Search</button></div>
    </form>
  </header>
  {results}
</div>"""
    title = f"{query} · Search · circverify" if query else "Search · circverify"
    return _shell(title, body, info=index_info, active="search")


def render_not_found(index_info: dict) -> str:
    body = f"""
<div class="shell notfound">
  <h1>This page is not here.</h1>
  <p>The address may be mistyped. Everything circverify does starts from the check page.</p>
  <p><a class="btn btn-primary" href="/">{icon("arrow-left")}Back to checking</a></p>
</div>"""
    return _shell("Not found · circverify", body, info=index_info)


def render_report(res: VerificationResult) -> str:
    when = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC").lstrip("0")
    body = (f'<div class="shell"><p class="report-head mono">Verification report · generated {when} · '
            f'{escape(index_status(res.index))}</p>{render_result(res, interactive=False)}</div>')
    return _shell(f"Report: {res.filename or res.sha256[:12]} · circverify", body, info=res.index, inline=True,
                  description="circverify verification report")


def snippet_html(raw: str) -> str:
    """FTS snippet with \\x02...\\x03 around matches, escaped and turned into <mark>."""
    return re.sub(r"\x02(.*?)\x03", r"<mark>\1</mark>", escape(raw))


def render_receipt(payload: dict, rid: str, created_at: str, index_info: dict) -> str:
    label, kind, tone, icon_name = VERDICTS.get(payload.get("verdict"), VERDICTS[UNREADABLE])
    off = payload.get("official")
    rows = []
    if off:
        rows.append(f'<div><dt>Official document</dt><dd><a href="{escape(off["url"])}" target="_blank" '
                    f'rel="noopener noreferrer">{escape(off["title"])}</a> <span class="mono">'
                    f'{escape(human_date(off.get("published")))}</span></dd></div>')
    for d in (payload.get("known_fake") or {}).get("debunks", []):
        rows.append(f'<div><dt>Debunked by</dt><dd><a href="{escape(d["url"])}" target="_blank" '
                    f'rel="noopener noreferrer">{escape(d.get("source") or "a fact-checker")}</a> '
                    f'<span class="mono">{escape(human_date(d.get("published")))}</span></dd></div>')
    if payload.get("withdrawn"):
        rows.append(f'<div><dt>Withdrawn</dt><dd class="mono">{escape(human_date(payload["withdrawn"]))}</dd></div>')
    rows.append(f'<div><dt>Checked file</dt><dd>{escape(payload["file"].get("name") or "")} '
                f'<span class="mono" translate="no">SHA-256 {escape(payload["file"]["sha256"][:16])}…</span></dd></div>')
    changes = "".join(
        f'<li><span class="mono">official</span> <mark class="hl-off">{escape(c["official"] or "nothing")}</mark> '
        f'<span class="mono">this copy</span> <mark class="hl-sub">{escape(c["submitted"] or "removed")}</mark></li>'
        for c in payload.get("changes", []))
    if payload.get("changed_regions"):
        changes += f"<li>{_plural(payload['changed_regions'], 'changed area')} on the page image</li>"
    body = f"""
<div class="shell receipt">
  <p class="report-head mono">Verification receipt {escape(rid)} · {escape(human_date(created_at))}</p>
  <section class="case tone-{tone}" aria-labelledby="verdict-title">
    <header class="case-head">
      <div class="case-verdict"><span class="seal">{icon(icon_name)}</span>
        <div><h1 class="verdict-word" id="verdict-title">{label}</h1><p class="verdict-kind">{kind}</p></div></div>
      <p class="case-summary">{escape(payload.get("summary", ""))}</p>
    </header>
    <dl class="facts receipt-facts">{"".join(rows)}</dl>
    {f'<h2 class="receipt-h">What changed</h2><ul class="notes">{changes}</ul>' if changes else ""}
    <p class="section-lede">Checked with circverify against {escape(index_status(payload.get("index") or {}))}.
      The submitted document itself is not stored.</p>
    <p><a class="btn btn-primary" href="/">{icon("arrow-counter-clockwise")}Check a document yourself</a></p>
  </section>
</div>"""
    return _shell(f"Receipt: {label} · circverify", body, info=index_info, description=payload.get("summary", ""))


def render_hash_page(index_info: dict, sha: str, prov) -> str:
    if prov:
        p = prov[0]
        verdict = f"""<h1 class="verdict-word" id="verdict-title">Verified</h1>
        <p class="verdict-kind">A file with this exact fingerprint is an official UGC document</p>
        <p class="case-summary">“{escape(p.title)}” <span class="mono">{escape(human_date(p.published))}</span>.
        Compare the copy you hold with <a href="{escape(p.url)}" target="_blank" rel="noopener noreferrer">the
        official file</a>, or check it on the <a href="/">check page</a>.</p>"""
        tone, ico = "ok", "seal-check"
    else:
        verdict = """<h1 class="verdict-word" id="verdict-title">Not found</h1>
        <p class="verdict-kind">No official document in the index has this fingerprint</p>
        <p class="case-summary">Check the document itself on the <a href="/">check page</a>.</p>"""
        tone, ico = "warn", "file-dashed"
    body = f"""
<div class="shell"><section class="case tone-{tone}" aria-labelledby="verdict-title"><header class="case-head">
  <div class="case-verdict"><span class="seal">{icon(ico)}</span><div>{verdict}</div></div>
  <p class="case-file mono" translate="no">SHA-256 {escape(sha)}</p></header></section></div>"""
    return _shell("Fingerprint check · circverify", body, info=index_info)


def render_coverage(index_info: dict, cov: dict, fakes: list) -> str:
    def row(label: str, value) -> str:
        v = f"{value:,}" if isinstance(value, int) else escape(str(value))
        return f"<div><dt>{escape(label)}</dt><dd class=\"mono\">{v}</dd></div>"

    crawl = [("Listing entries", cov["listing_entries"]), ("No longer listed (withdrawn)", cov["listing_entries_removed"]),
             ("Files they link to", cov["linked_files"]), ("Downloaded from ugc.gov.in", cov["downloaded_live"]),
             ("Recovered from the Wayback Machine", cov["recovered_from_wayback"]), ("Dead links", cov["dead"]),
             ("Coverage", f"{cov['coverage_pct']}%")]
    text = [("Files indexed", cov["files_indexed"]), ("Pages indexed", cov["pages"]),
            ("Pages with no text layer", cov["pages_without_text_layer"]), ("Read with OCR", cov["pages_ocr"]),
            ("Still without text (matched by image only)", cov["pages_still_without_text"]),
            ("Files with a reference number", cov["files_with_reference_number"]),
            ("Archived documents no longer listed", cov["archived_only_documents"])]
    fake_rows = "".join(
        f'<li><a href="{escape(f["url"])}" target="_blank" rel="noopener noreferrer">{escape(f["source"])}</a> '
        f'<span class="mono">{escape(human_date(f["published"]))}</span> {escape((f["claim"] or "")[:140])}</li>'
        for f in fakes)
    body = f"""
<div class="shell">
  <header class="page-head"><h1>Coverage and blind spots</h1>
    <p class="lede">What the index holds, measured against what UGC lists. Knowing what is missing is part of
    reading a result honestly.</p></header>
  <div class="coverage-grid">
    <section><h2>Crawl</h2><dl class="facts">{"".join(row(a, b) for a, b in crawl)}</dl></section>
    <section><h2>Text and fields</h2><dl class="facts">{"".join(row(a, b) for a, b in text)}</dl></section>
  </div>
  <section class="coverage-fakes"><h2>Known fakes ({cov["known_fakes"]})</h2>
    <p class="section-lede">Documents publicly debunked by UGC or PIB Fact Check. Only a match with one of these
    produces the verdict "Known fake".</p><ul class="notes">{fake_rows or "<li>None yet.</li>"}</ul></section>
</div>"""
    return _shell("Coverage · circverify", body, info=index_info)


def atom_feed(notices: list[dict], base_url: str, updated: str | None) -> str:
    entries = []
    for n in notices:
        link = escape(n.get("url") or base_url)
        when = escape((n.get("first_seen") or "")[:19] + "Z")
        entries.append(
            f"<entry><title>{escape(n['title'])}</title><link href=\"{link}\"/>"
            f"<id>{link}</id><updated>{when}</updated>"
            f"<summary>{escape(SOURCE_NAMES.get(n.get('source'), n.get('source') or ''))}, published "
            f"{escape(n.get('published') or 'date not listed')}</summary></entry>")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">'
            "<title>New UGC notices (circverify)</title>"
            f'<link href="{escape(base_url)}"/><id>{escape(base_url)}/feed.xml</id>'
            f"<updated>{escape((updated or '')[:19])}Z</updated>{''.join(entries)}</feed>")
