"""Known fakes: documents that the issuer or a fact-checker has publicly debunked.

This is the only basis on which circverify ever says "fake". A submission
that matches a debunked document gets the Known fake verdict with a link to
the debunk; anything else that is missing from the index is merely
"not found".

Two ways in:

* ``register_fake``: add a file by hand with the URL of the debunk (a UGC
  public notice, a PIB Fact Check post, a news fact-check).
* ``sync_pib``: read PIB Fact Check's public Telegram channel preview
  (``t.me/s/PIB_FactCheck``), keep posts that mention UGC, and register
  their images.

Safety rule: a debunk post sometimes shows the issuer's genuine clarification
next to the fake. Any image that matches an official document is refused, so
a real notice can never end up labelled fake.
"""

from __future__ import annotations

import html as htmllib
import re
from collections import Counter
from dataclasses import dataclass, field

from .crawler import Fetcher
from .db import Database
from .extract import HASH_WIDTH, UnsupportedFile, iter_pages, sniff_kind
from .fingerprint import page_fingerprint
from .store import BlobStore

PIB_CHANNEL = "PIB_FactCheck"
DEFAULT_QUERIES = ("UGC", "University Grants Commission", "ugc_india")
ISSUER_WORDS = re.compile(r"\b(UGC|University Grants Commission|ugc_india)\b", re.I)


@dataclass
class DebunkPost:
    url: str
    published: str | None
    text: str
    images: list[str] = field(default_factory=list)


def parse_telegram_posts(page: str, channel: str = PIB_CHANNEL) -> list[DebunkPost]:
    """Posts on a ``t.me/s/<channel>`` preview page: link, date, text and image URLs."""
    posts = []
    marker = f'data-post="{channel}/'
    chunks = page.split(marker)[1:]
    for chunk in chunks:
        post_id = chunk.split('"', 1)[0]
        body = chunk
        text_m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', body, re.S)
        text = ""
        if text_m:
            text = re.sub(r"<br\s*/?>", "\n", text_m.group(1))
            text = htmllib.unescape(re.sub(r"<[^>]+>", " ", text))
            text = re.sub(r"[ \t]+", " ", text).strip()
        date_m = re.search(r'<time[^>]*datetime="([^"]+)"', body)
        images = re.findall(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", body)
        posts.append(DebunkPost(url=f"https://t.me/{channel}/{post_id}",
                                published=date_m.group(1)[:10] if date_m else None,
                                text=text, images=list(dict.fromkeys(images))))
    return posts


def extract_document(data: bytes) -> bytes | None:
    """The notice itself, cut out of a fact-check card, with the red FAKE stamp removed.

    Fact-checkers publish the fake inside a branded card with a red stamp
    across it. People forward the bare notice, so that is what should be
    fingerprinted: paper-white sheet only, stamp pixels turned back to white.
    """
    try:
        import cv2
    except ImportError:
        return None
    import io

    import numpy as np
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    arr = np.asarray(img).astype(np.int16)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    stamp = (r > 140) & (r - g > 60) & (r - b > 60)
    clean = arr.copy()
    clean[stamp] = 255
    gray = cv2.cvtColor(clean.astype(np.uint8), cv2.COLOR_RGB2GRAY)
    # Paper is bright *and* unsaturated; card backgrounds are usually tinted (PIB uses light blue).
    spread = clean.max(axis=2) - clean.min(axis=2)
    white = ((clean.min(axis=2) > 225) & (spread < 22)).astype(np.uint8) * 255
    k = max(5, min(gray.shape) // 40)
    closed = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    sheet = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(sheet)
    if not (0.15 <= (w * h) / gray.size <= 0.95):
        return None
    out = io.BytesIO()
    Image.fromarray(clean.astype(np.uint8)[y:y + h, x:x + w]).save(out, format="PNG")
    return out.getvalue()


def fake_pages(data: bytes, ocr=None) -> list[dict]:
    """Fingerprints of a fake, prepared like a submission so the two compare like for like."""
    pages = []
    for page in iter_pages(data, render_width=HASH_WIDTH, photo=True, max_pages=6, max_render_pages=6, ocr=ocr):
        text, source = page.text, page.text_source
        if not text and ocr is not None and page.image is not None:
            try:
                text = ocr(page.image) or ""
                source = "ocr" if text.strip() else "none"
            except Exception:  # noqa: BLE001
                text, source = "", "none"
        pages.append({"page_no": page.number, "text": text, "text_source": source,
                      **page_fingerprint(text, page.image)})
    return pages


def register_fake(db: Database, store: BlobStore, data: bytes, *, source: str, url: str,
                  published: str | None, claim: str | None, verifier=None, ocr=None,
                  note: str | None = None) -> tuple[str | None, str]:
    """Add one debunked file. Returns ``(sha256, status)``; sha256 is None if it was refused."""
    from .matcher import UNCONFIRMED_MATCH, VERIFIED_CONTENT, VERIFIED_EXACT  # local: matcher imports our peers

    kind = sniff_kind(data[:2048])
    if kind is None:
        return None, "not a PDF or image"
    if verifier is not None:
        res = verifier.verify(data, "candidate fake")
        if res.verdict in (VERIFIED_EXACT, VERIFIED_CONTENT, UNCONFIRMED_MATCH):
            title = res.match.primary.title if res.match and res.match.primary else "an official document"
            return None, f"refused: matches the official “{title}”"
    try:
        pages = fake_pages(data, ocr)
    except UnsupportedFile as exc:
        return None, f"unreadable: {exc}"
    sha = store.put_bytes(data)
    debunk_id = db.add_debunk(source=source, url=url, published=published, claim=claim)
    db.add_fake(sha, debunk_id, kind, pages, note)
    db.commit()
    return sha, "added"


def sync_pib(db: Database, store: BlobStore, fetcher: Fetcher, *, queries=DEFAULT_QUERIES, verifier=None,
             ocr=None, on_post=None) -> Counter:
    """Register images from PIB Fact Check posts that mention the issuer."""
    counts: Counter = Counter()
    seen: dict[str, DebunkPost] = {}
    for q in queries:
        url = f"https://t.me/s/{PIB_CHANNEL}"
        if not fetcher.allowed(url):
            counts["robots"] += 1
            continue
        fetcher.throttle()
        try:
            resp = fetcher.client.get(url, params={"q": q})
            resp.raise_for_status()
        except Exception:  # noqa: BLE001 - one failed query should not stop the rest
            counts["query_failed"] += 1
            continue
        for post in parse_telegram_posts(resp.text):
            if post.images and ISSUER_WORDS.search(post.text):
                seen.setdefault(post.url, post)
    for post in seen.values():
        claim = post.text.split("\n")[0][:300]
        for img_url in post.images:
            fetcher.throttle()
            try:
                img = fetcher.client.get(img_url)
                img.raise_for_status()
            except Exception:  # noqa: BLE001
                counts["image_failed"] += 1
                continue
            versions = [(img.content, img_url)]
            sheet = extract_document(img.content)
            if sheet is not None:
                versions.append((sheet, f"notice cut out of {img_url}"))
            for blob, note in versions:
                sha, status = register_fake(db, store, blob, source="PIB Fact Check", url=post.url,
                                            published=post.published, claim=claim, verifier=verifier, ocr=ocr,
                                            note=note)
                counts["added" if sha else ("refused" if status.startswith("refused") else "skipped")] += 1
        if on_post:
            on_post(post)
    counts["posts"] = len(seen)
    return counts
