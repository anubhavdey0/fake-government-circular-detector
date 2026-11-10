"""Links, e-mail addresses and QR codes in a submitted document, and whose domains they are.

A fake notice usually wants something: a "registration fee" paid on a website that looks like the
issuer's (ugc-gov-in.com instead of ugc.gov.in), or a reply to an address that is not the issuer's.
The domain is the one part a forger cannot borrow from the real notice.

Each domain is put in one of these groups, in this order:

* ``official``: the issuer's own domains (ugc.gov.in, ugc.ac.in and their subdomains);
* ``government``: Indian government and academic domains (gov.in, nic.in, ac.in, ...), which only
  government bodies and institutions can register;
* ``shortener``: link shorteners, which hide where they lead;
* ``lookalike``: imitates an official domain (contains the issuer's name, is one or two typos away,
  puts the official name in front of another domain, or uses look-alike characters), unless the
  issuer's own documents use it widely (UGC's NET portal is ugcnetonline.in);
* ``known``: appears in at least two of the issuer's own documents;
* ``unknown``: none of the above.

This produces hints for an unverified document, never a verdict.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import numpy as np
from PIL import Image

OFFICIAL_DOMAINS = ("ugc.gov.in", "ugc.ac.in")
ISSUER_TOKENS = ("ugc",)
# Second-level suffixes under which an organisation's own name is the third label.
_TWO_LEVEL = ("gov.in", "nic.in", "ac.in", "edu.in", "res.in", "co.in", "org.in", "net.in", "firm.in", "gen.in",
              "ind.in", "mil.in", "ernet.in", "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au")
# Registered only by government bodies and recognised institutions in India.
RESTRICTED_SUFFIXES = ("gov.in", "nic.in", "ac.in", "edu.in", "res.in", "mil.in", "ernet.in")
SHORTENERS = {"bit.ly", "tinyurl.com", "goo.gl", "t.ly", "cutt.ly", "rb.gy", "is.gd", "shorturl.at", "ow.ly",
              "tiny.cc", "t.co", "s.id", "rebrand.ly", "bl.ink", "short.io", "tny.im"}
KNOWN_MIN_DOCS = 2
# A domain that looks like the issuer's but that the issuer itself uses this widely is the issuer's.
LOOKALIKE_OVERRIDE_DOCS = 10

_TLDS = (r"in|com|org|net|info|co|io|online|site|xyz|top|live|app|gov|edu|biz|me|us|uk|website|space|store|"
         r"click|link|cc|tk|ml|ga|cf|gq|news|today|world|ly|gl|gd|im|id")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'()\[\]{}|\\^`]+")
_EMAIL = re.compile(r"(?i)\b[a-z0-9._%+-]+@((?:[a-z0-9-]+\.)+[a-z]{2,})\b")
_BARE = re.compile(rf"(?i)(?<![@\w.-])((?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.)+(?:{_TLDS}))\b(/[^\s<>\"')\]]*)?")


@dataclass
class LinkFinding:
    source: str  # "text", "email" or "qr"
    target: str  # the URL, address or QR payload as found
    host: str
    domain: str  # registrable domain
    kind: str  # official | government | shortener | lookalike | known | unknown
    imitates: str | None = None
    page: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def registrable(host: str) -> str:
    """``uamp.ugc.ac.in`` -> ``ugc.ac.in``; ``www.ugc-gov-in.com`` -> ``ugc-gov-in.com``."""
    host = host.lower().strip(".")
    labels = host.split(".")
    for suffix in _TWO_LEVEL:
        if host == suffix:
            return host
        if host.endswith("." + suffix):
            return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def host_of(target: str) -> str | None:
    t = target.strip().strip(".,;:)")
    if "@" in t and "/" not in t:
        return t.rsplit("@", 1)[1].lower()
    if not re.match(r"(?i)^[a-z][a-z0-9+.-]*://", t):
        t = "http://" + t
    try:
        host = urlsplit(t).hostname
    except ValueError:
        return None
    return host.lower().strip(".") if host and "." in host else None


def find_in_text(text: str) -> list[tuple[str, str]]:
    """``(source, target)`` for every URL, bare domain and e-mail address in ``text``."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _EMAIL.finditer(text):
        out.append(("email", m.group(0)))
        seen.add(m.group(1).lower())
    for m in _URL.finditer(text):
        target = m.group(0).rstrip(".,;:")
        host = host_of(target)
        if host and host not in seen:
            out.append(("text", target))
            seen.add(host)
    for m in _BARE.finditer(text):
        host = m.group(1).lower()
        if host not in seen and not any(host.endswith("." + s) or host == s for s in seen):
            out.append(("text", m.group(0)))
            seen.add(host)
    return out


def text_domains(text: str) -> set[str]:
    """Registrable domains mentioned in ``text`` (for learning which domains the issuer uses)."""
    return {registrable(h) for _, t in find_in_text(text) if (h := host_of(t))}


def read_qr_codes(image: Image.Image) -> list[str]:
    """Payloads of the QR codes printed on a page image."""
    try:
        import cv2
    except ImportError:
        return []
    gray = np.asarray(image.convert("L"))
    detector = cv2.QRCodeDetector()
    for scale in (1.0, 0.5, 2.0):
        img = gray if scale == 1.0 else cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        try:
            ok, texts, _, _ = detector.detectAndDecodeMulti(img)
        except cv2.error:
            continue
        found = [t for t in (texts if ok else ()) if t]
        if found:
            return list(dict.fromkeys(found))
    return []


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def assess(host: str, known: dict[str, int] | None = None) -> tuple[str, str | None]:
    """``(kind, imitated official domain)`` for a host; see the module docstring for the groups."""
    known = known or {}
    domain = registrable(host)
    if domain in OFFICIAL_DOMAINS or any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS):
        return "official", None
    if any(host == s or host.endswith("." + s) for s in RESTRICTED_SUFFIXES):
        return "government", None
    if domain in SHORTENERS:
        return "shortener", None
    uses = known.get(domain, 0)
    imitates = _imitates(host, domain)
    if imitates and uses < LOOKALIKE_OVERRIDE_DOCS:
        return "lookalike", imitates
    if uses >= KNOWN_MIN_DOCS:
        return "known", None
    return "unknown", None


def _imitates(host: str, domain: str) -> str | None:
    for official in OFFICIAL_DOMAINS:
        if host.startswith(official + "."):  # ugc.gov.in.secure-verify.com
            return official
    if "xn--" in host:
        return OFFICIAL_DOMAINS[0]
    name = domain.split(".")[0]
    if any(tok in re.split(r"[^a-z0-9]+", name) or name.startswith(tok) for tok in ISSUER_TOKENS):
        return OFFICIAL_DOMAINS[0]
    for official in OFFICIAL_DOMAINS:
        if 0 < _distance(domain, official) <= 2:
            return official
    return None


def check(text: str, images: list[tuple[int, Image.Image]], known: dict[str, int] | None = None,
          *, qr: bool = True) -> list[LinkFinding]:
    """Every link, address and QR code in the submission, with its domain's group."""
    findings: list[LinkFinding] = []
    for page_no, img in images if qr else ():
        for payload in read_qr_codes(img):
            host = host_of(payload) if re.match(r"(?i)^(https?://|www\.)", payload.strip()) else None
            if host:
                kind, imitates = assess(host, known)
                findings.append(LinkFinding("qr", payload, host, registrable(host), kind, imitates, page_no))
            else:
                findings.append(LinkFinding("qr", payload, "", "", "not-a-link", None, page_no))
    for source, target in find_in_text(text or ""):
        host = host_of(target)
        if host:
            kind, imitates = assess(host, known)
            findings.append(LinkFinding(source, target, host, registrable(host), kind, imitates))
    return findings


def describe(f: LinkFinding) -> str | None:
    """A sentence for the report about a finding worth pointing out, or None."""
    where = {"qr": f"Its QR code{f' (page {f.page})' if f.page else ''} opens", "email": "It gives the address",
             "text": "It points to"}[f.source]
    shown = f.target if len(f.target) <= 80 else f.target[:77] + "…"
    if f.kind == "lookalike":
        return (f"{where} {shown}, on {f.domain}, which imitates {f.imitates}. The issuer's own documents do not "
                "use that domain; lookalike sites are a common way to collect fees and personal details.")
    if f.kind == "shortener":
        return f"{where} {shown}, a shortened link that hides where it really leads."
    if f.kind == "unknown":
        return (f"{where} {shown}. {f.domain} is not a government domain and does not appear in the issuer's "
                "own documents.")
    if f.kind == "not-a-link" and f.source == "qr":
        return f"It has a QR code{f' on page {f.page}' if f.page else ''} that holds text rather than a link: “{shown}”."
    return None
