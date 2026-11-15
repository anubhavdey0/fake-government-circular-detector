"""Command-line interface.

``circverify crawl | watch | index | verify | search | stats | coverage | export | fakes | qr | serve | telegram``
"""

from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from .config import default_data_dir

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Verify circulars against an index of everything the issuer has actually published. Unofficial tool.",
)
fakes_app = typer.Typer(no_args_is_help=True, help="Documents publicly debunked by the issuer or a fact-checker.")
app.add_typer(fakes_app, name="fakes")
reports_app = typer.Typer(no_args_is_help=True, help="Documents people reported as fake: the review queue.")
app.add_typer(reports_app, name="reports")

# Output redirected to a file or another program (``--json > result.json``) is
# always UTF-8, so Hindi titles survive intact. An interactive Windows console
# may use a legacy code page (cp1252) that cannot encode them; there, degrade
# those characters instead of crashing halfway through a report.
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream.isatty():
            _stream.reconfigure(errors="replace")
        else:
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

console = Console()
err = Console(stderr=True)

DataDir = typer.Option(None, "--data-dir", "-d", help="Index location (default: per-user app data, "
                                                       "or $CIRCVERIFY_DATA_DIR).")
EXIT_CODES = {"VERIFIED_EXACT": 0, "VERIFIED_CONTENT": 0, "UNCONFIRMED_MATCH": 5, "ALTERED": 1, "NO_MATCH": 3,
              "KNOWN_FAKE": 4,
              "UNREADABLE": 2}


def _paths(data_dir: Optional[Path]):
    from .db import Database
    from .store import BlobStore

    root = data_dir or default_data_dir()
    return Database(root / "index.sqlite3"), BlobStore(root), root


def _progress() -> Progress:
    return Progress(
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("{task.fields[status]}"),
        console=err,
    )


def _tally_progress(label: str, total: int):
    progress = _progress()
    task = progress.add_task(label, total=total, status="")
    tally: dict[str, int] = {}

    def on_result(out):
        key = out.status if getattr(out, "changed", None) is None else ("changed" if out.changed else "unchanged")
        tally[key] = tally.get(key, 0) + 1
        progress.update(task, advance=1, status=" ".join(f"{k}={v}" for k, v in sorted(tally.items())))

    return progress, on_result


def _run_downloads(db, store, fetcher, urls, workers, wayback=False, validators=None, snapshots=None):
    from .crawler import download_all

    if not urls:
        return {}
    progress, on_result = _tally_progress("Re-checking" if validators else
                                          ("Wayback lookups" if wayback else "Downloading"), len(urls))
    with progress:
        return dict(download_all(db, store, fetcher, urls, workers=workers, wayback=wayback,
                                 validators=validators, snapshots=snapshots, on_result=on_result))


def _run_index(db, store, workers, ocr, reindex=False, ocr_pages=2):
    from .indexer import enrich_pending, index_pending

    with _progress() as progress:
        task = progress.add_task("Indexing", total=None, status="")
        errors = 0

        def on_result(res, total):
            nonlocal errors
            errors += res.error is not None
            progress.update(task, total=total, advance=1, status=f"errors={errors}")

        out = index_pending(db, store, workers=workers, use_ocr=ocr, reindex=reindex, ocr_pages=ocr_pages,
                            on_result=on_result)
    # Files indexed before fields existed get them now (cheap; no OCR unless asked).
    enriched = enrich_pending(db, store, workers=workers, use_ocr=False)
    if out["indexed"] or enriched["files"] or not db.conn.execute("SELECT 1 FROM relations LIMIT 1").fetchone():
        _build_relations(db)
    return out


def _build_relations(db) -> None:
    from .relations import build

    with err.status("Linking documents that amend, correct or replace earlier ones…"):
        counts = build(db)
    if counts:
        err.print("Relations: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def _print_counts(label: str, counts: dict) -> None:
    if counts:
        err.print(f"{label}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


# -- crawling -------------------------------------------------------------------------

@app.command()
def crawl(
    data_dir: Optional[Path] = DataDir,
    issuer: str = typer.Option("ugc", help="Issuer to crawl."),
    source: list[str] = typer.Option(None, "--source", "-s", help="Only these listing sources (repeatable)."),
    limit: Optional[int] = typer.Option(None, help="Download at most this many files (for a trial run)."),
    workers: int = typer.Option(2, help="Concurrent downloads."),
    delay: float = typer.Option(0.5, help="Minimum seconds between requests, across all workers."),
    retry_failed: bool = typer.Option(False, help="Also retry links that were dead last time."),
    recheck: bool = typer.Option(False, help="Re-check files already downloaded (conditional requests); "
                                             "a changed file is kept as a new version."),
    wayback: bool = typer.Option(False, help="Recover dead links from the Wayback Machine."),
    scan_archive: bool = typer.Option(False, help="Record when the Wayback Machine first captured each file."),
    backfill: Optional[int] = typer.Option(None, help="Also download up to N archived files the issuer no "
                                                      "longer lists (implies --scan-archive)."),
    download: bool = typer.Option(True, help="Download linked files (off: just sync listings)."),
    index: bool = typer.Option(True, help="Index new files after downloading."),
    index_workers: Optional[int] = typer.Option(None, help="Processes for indexing (default: CPUs - 1)."),
    ocr: bool = typer.Option(False, "--ocr/--no-ocr", help="OCR the first pages of scans while indexing (slow)."),
):
    """Sync the issuer's listings, download new files and index them."""
    from .crawler import Fetcher, backfill as do_backfill, recheck_targets, scan_archive as do_scan, sync_listings
    from .issuers import get_issuer

    db, store, root = _paths(data_dir)
    fetcher = Fetcher(delay=delay)
    iss = get_issuer(issuer)
    try:
        with err.status("Fetching listings…"):
            res = sync_listings(db, iss, fetcher, source or None)
        err.print(f"Listings: {res.seen} entries ({res.new} new, {res.removed} no longer listed), {res.links} links "
                  f"[dim]({', '.join(f'{k}={v}' for k, v in sorted(res.by_source.items()))})[/dim]")
        if download:
            urls = db.urls_to_fetch(retry_failed=retry_failed)
            if limit is not None:
                urls = urls[:limit]
            err.print(f"{len(urls)} files to download into {root}")
            _print_counts("Downloads", _run_downloads(db, store, fetcher, urls, workers))
            if recheck:
                targets = recheck_targets(db)
                _print_counts("Re-check", _run_downloads(db, store, fetcher, list(targets), workers,
                                                         validators=targets))
            if wayback:
                dead = db.dead_urls()[:limit] if limit else db.dead_urls()
                err.print(f"Looking up {len(dead)} dead links in the Wayback Machine")
                # A capture found by --scan-archive is the document itself; the availability API's
                # "closest" capture is often the site's HTML error page from after the file was removed.
                _print_counts("Wayback", _run_downloads(db, store, fetcher, dead, workers=1, wayback=True,
                                                        snapshots=db.capture_snapshots(dead)))
        if scan_archive or backfill:
            from .crawler import ArchiveUnavailable

            try:
                with err.status("Reading the Wayback Machine index…"):
                    unlisted, recorded = do_scan(db, iss, fetcher)
            except ArchiveUnavailable as exc:
                err.print(f"[red]Wayback Machine index unavailable ({exc}); nothing recorded. Try again later.[/red]")
                unlisted, recorded = {}, 0
            else:
                err.print(f"Wayback Machine: first-capture dates for {recorded:,} official files; "
                          f"{len(unlisted):,} archived files are no longer listed by the issuer")
            if backfill and unlisted:
                progress, on_result = _tally_progress("Backfilling", min(backfill, len(unlisted)))
                with progress:
                    _print_counts("Backfill", dict(do_backfill(db, store, fetcher, iss, unlisted, limit=backfill,
                                                               workers=workers, on_result=on_result)))
        if index:
            out = _run_index(db, store, index_workers, ocr)
            err.print(f"Indexed {out['indexed']} files ({out['errors']} could not be read)")
    finally:
        fetcher.close()
        db.close()


@app.command()
def watch(
    data_dir: Optional[Path] = DataDir,
    every: float = typer.Option(6.0, help="Hours between runs."),
    recheck_every: int = typer.Option(4, help="Re-check existing files on every Nth run (0: never)."),
):
    """Run the crawl on a schedule and print what is new. Stop with Ctrl+C."""
    run = 0
    while True:
        run += 1
        err.print(f"[bold]Run {run}[/bold] at {time.strftime('%Y-%m-%d %H:%M')}")
        try:
            crawl(data_dir=data_dir, issuer="ugc", source=None, limit=None, workers=2, delay=0.5,
                  retry_failed=False, recheck=bool(recheck_every) and run % recheck_every == 0, wayback=False,
                  scan_archive=False, backfill=None, download=True, index=True, index_workers=None, ocr=False)
        except Exception as exc:  # noqa: BLE001 - keep watching after a failed run
            err.print(f"[red]Run failed:[/red] {exc}")
        db, _, _ = _paths(data_dir)
        try:
            for n in db.newest_notices(5):
                err.print(f"  {n['first_seen'][:10]}  {n['title'][:100]}")
        finally:
            db.close()
        time.sleep(max(60.0, every * 3600))


@app.command("index")
def index_cmd(
    data_dir: Optional[Path] = DataDir,
    workers: Optional[int] = typer.Option(None, help="Processes (default: CPUs - 1, fewer with OCR)."),
    ocr: bool = typer.Option(False, "--ocr/--no-ocr", help="OCR pages that have no text layer."),
    ocr_pages: int = typer.Option(2, help="With --ocr, how many leading pages of each scan to read."),
    reindex: bool = typer.Option(False, help="Re-index every file, not just new ones."),
    enrich: bool = typer.Option(False, help="Fill in fields (and OCR with --ocr) for files already indexed."),
    limit: Optional[int] = typer.Option(None, help="With --enrich, process at most this many files."),
    relations: bool = typer.Option(False, help="Rebuild the links between documents that amend, correct, extend, "
                                              "replace or withdraw earlier ones."),
):
    """Extract text, fingerprints and fields from downloaded files."""
    from .indexer import enrich_pending, mark_garbled
    from .ocr import available_engine

    db, store, _ = _paths(data_dir)
    try:
        if ocr and not available_engine():
            err.print("[red]No OCR engine found.[/red] Install Tesseract and `pip install circverify[ocr]`, "
                      "or `pip install rapidocr onnxruntime` and set CIRCVERIFY_OCR_ENGINE=rapidocr.")
            raise typer.Exit(2)
        out = _run_index(db, store, workers, ocr, reindex, ocr_pages)
        err.print(f"Indexed {out['indexed']} files ({out['errors']} could not be read)")
        if enrich:
            n = mark_garbled(db)
            if n:
                err.print(f"{n:,} pages have a text layer that cannot be read; they are now treated as scans")
            with _progress() as progress:
                task = progress.add_task("Enriching", total=None, status="")
                out = enrich_pending(db, store, workers=workers, use_ocr=ocr, ocr_pages=ocr_pages, limit=limit,
                                     on_result=lambda r, total: progress.update(task, total=total, advance=1))
            err.print(f"Enriched {out['files']} files, {out['ocr_pages']} pages read with OCR "
                      f"({out['errors']} errors)")
        if relations:
            _build_relations(db)
    finally:
        db.close()


# -- checking -------------------------------------------------------------------------

@app.command()
def verify(
    files: Optional[list[Path]] = typer.Argument(None, exists=True, help="PDFs or images to check, or folders of them."),
    data_dir: Optional[Path] = DataDir,
    text: Optional[str] = typer.Option(None, "--text", help="Check pasted text instead of a file ('-' reads stdin)."),
    url: Optional[str] = typer.Option(None, "--url", help="Check the document at a link (and the link's website)."),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
    html: Optional[Path] = typer.Option(None, help="Also write an HTML report with the visual diff."),
    refresh: bool = typer.Option(False, help="Sync the issuer's listings and fetch new files first."),
    ocr: Optional[bool] = typer.Option(None, "--ocr/--no-ocr", help="OCR the input if nothing else matches."),
    csv_out: Optional[Path] = typer.Option(None, "--csv", help="With several files: also write the results as CSV."),
):
    """Check a document (or pasted text) against the index. Several files, or a folder, give a summary table."""
    from .matcher import Verifier
    from .report import print_result, write_html

    paths = _expand(files or [])
    if not paths and text is None and url is None:
        err.print("Give a file to check, --text or --url.")
        raise typer.Exit(2)
    db, store, _ = _paths(data_dir)
    try:
        if refresh:
            _refresh(db, store)
        if db.summary()["files_indexed"] == 0:
            err.print("[red]The index is empty.[/red] Run `circverify crawl` first.")
            raise typer.Exit(2)
        verifier = Verifier(db, store, use_ocr=ocr)
        if len(paths) > 1:
            raise typer.Exit(_verify_many(verifier, paths, as_json, csv_out))
        file = paths[0] if paths else None
        if url is not None:
            from .fetchurl import LinkError, verify_link

            try:
                result = verify_link(verifier, url)
            except LinkError as exc:
                err.print(f"[red]{exc}[/red]")
                raise typer.Exit(2) from None
        elif text is not None:
            body = sys.stdin.read() if text == "-" else text
            result = verifier.verify_text(body)
        else:
            result = verifier.verify(file.read_bytes(), filename=file.name)
        if as_json:
            sys.stdout.write(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n")
        else:
            print_result(result, console)
        if html:
            write_html(result, html)
            err.print(f"HTML report written to {html}")
        raise typer.Exit(EXIT_CODES.get(result.verdict, 3))
    finally:
        db.close()


_DOC_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff"}


def _expand(items: list[Path]) -> list[Path]:
    out: list[Path] = []
    for item in items:
        if item.is_dir():
            out += sorted(p for p in item.rglob("*") if p.is_file() and p.suffix.lower() in _DOC_SUFFIXES)
        else:
            out.append(item)
    return out


def _verify_many(verifier, paths: list[Path], as_json: bool, csv_out: Optional[Path]) -> int:
    """Check many files; the exit code is the most serious verdict among them."""
    rows = []
    with _progress() as progress:
        task = progress.add_task("Checking", total=len(paths), status="")
        for p in paths:
            res = verifier.verify(p.read_bytes(), filename=p.name)
            prim = res.match.primary if res.match and res.match.primary else None
            rows.append({"file": str(p), "verdict": res.verdict, "summary": res.summary, "sha256": res.sha256,
                         "official_title": prim.title if prim else "", "official_url": prim.url if prim else "",
                         "debunk_url": ((res.known_fake or {}).get("debunks") or [{}])[0].get("url", "")})
            progress.update(task, advance=1, status=res.verdict)
    if as_json:
        sys.stdout.write(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    else:
        from .report import VERDICT_LABEL

        table = Table(show_lines=False)
        for col in ("File", "Verdict", "Official document or debunk"):
            table.add_column(col)
        for r in rows:
            label, color = VERDICT_LABEL.get(r["verdict"], (r["verdict"], "white"))
            table.add_row(Path(r["file"]).name, f"[{color}]{label}[/{color}]",
                          r["official_title"] or r["debunk_url"] or "")
        console.print(table)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        err.print(", ".join(f"{v}: {n}" for v, n in sorted(counts.items())))
    if csv_out:
        with csv_out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        err.print(f"Results written to {csv_out}")
    # verified < matches (details unconfirmed) < not found < unreadable < altered < known fake
    severity = {0: 0, 5: 1, 3: 2, 2: 3, 1: 4, 4: 5}
    return max((EXIT_CODES.get(r["verdict"], 3) for r in rows), key=lambda c: severity.get(c, 1))


def _refresh(db, store) -> None:
    from .crawler import Fetcher, sync_listings
    from .indexer import index_pending
    from .issuers import get_issuer

    fetcher = Fetcher()
    try:
        with err.status("Refreshing listings…"):
            res = sync_listings(db, get_issuer("ugc"), fetcher)
            urls = db.urls_to_fetch()
        err.print(f"{res.new} new listing entries, {len(urls)} new files")
        if urls:
            _run_downloads(db, store, fetcher, urls, workers=2)
            index_pending(db, store, workers=1)
    finally:
        fetcher.close()


@app.command()
def search(
    query: str = typer.Argument(..., help="Words to look for (SQLite FTS5 syntax)."),
    data_dir: Optional[Path] = DataDir,
    limit: int = typer.Option(10),
):
    """Full-text search over every indexed page."""
    db, _, _ = _paths(data_dir)
    try:
        table = Table(show_lines=True)
        for col in ("Date", "Title", "Page", "Snippet"):
            table.add_column(col)
        try:
            rows = db.search(query, limit)
        except Exception as exc:  # noqa: BLE001 - FTS5 syntax errors
            err.print(f"[red]Bad query:[/red] {exc}")
            raise typer.Exit(2) from None
        for r in rows:
            prov = db.provenance(r["sha256"])
            p = prov[0] if prov else None
            table.add_row(
                (p.published if p else None) or "?",
                f"{p.title}\n[dim]{p.url}[/dim]" if p else r["sha256"][:12],
                str(r["page_no"]),
                r["snippet"],
            )
        console.print(table)
    finally:
        db.close()


# -- reporting ------------------------------------------------------------------------

@app.command()
def stats(data_dir: Optional[Path] = DataDir, as_json: bool = typer.Option(False, "--json")):
    """What is in the index."""
    db, _, root = _paths(data_dir)
    try:
        s = db.stats()
        if as_json:
            sys.stdout.write(json.dumps(s, indent=2) + "\n")
            return
        table = Table(title=f"Index at {root}", show_header=False)
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Listing entries", f"{s['notices']:,}  ({', '.join(f'{k}: {v}' for k, v in s['notices_by_source'].items())})")
        table.add_row("Linked files", f"{s['links']:,}")
        table.add_row("Fetch status", ", ".join(f"{k}: {v:,}" for k, v in sorted(s["fetch_status"].items())))
        table.add_row("Unique files", f"{s['files']:,}  ({s['files_bytes'] / 1e9:.2f} GB)")
        table.add_row("Indexed", f"{s['files_indexed']:,}  ({s['files_index_errors']} unreadable)")
        table.add_row("Pages", f"{s['pages']:,}  ({s['pages_with_text']:,} with text, {s['pages_ocr']:,} via OCR)")
        table.add_row("Listings synced", s["last_listing_sync"] or "never")
        table.add_row("Last download", s["last_download"] or "never")
        console.print(table)
    finally:
        db.close()


COVERAGE_LABELS = {
    "listing_entries": "Listing entries",
    "listing_entries_removed": "No longer listed (withdrawn)",
    "linked_files": "Files they link to",
    "downloaded_live": "Downloaded from the issuer",
    "recovered_from_wayback": "Recovered from the Wayback Machine",
    "dead": "Dead links",
    "too_large": "Too large to download",
    "coverage_pct": "Coverage (%)",
    "files_indexed": "Files indexed",
    "pages": "Pages indexed",
    "pages_without_text_layer": "Pages without a text layer",
    "pages_ocr": "  read with OCR",
    "pages_still_without_text": "  still without text",
    "files_with_reference_number": "Files with a reference number",
    "archived_only_documents": "Archived-only documents (backfill)",
    "first_capture_known": "Files with a known first capture",
    "known_fakes": "Known fakes",
    "debunks": "Debunks",
}


@app.command()
def coverage(data_dir: Optional[Path] = DataDir, as_json: bool = typer.Option(False, "--json")):
    """How much of what the issuer lists is indexed, and where the blind spots are."""
    db, _, _ = _paths(data_dir)
    try:
        c = db.coverage()
        if as_json:
            sys.stdout.write(json.dumps(c, indent=2) + "\n")
            return
        table = Table(title="Coverage", show_header=False)
        table.add_column(style="bold")
        table.add_column(justify="right")
        for key, label in COVERAGE_LABELS.items():
            v = c.get(key)
            table.add_row(label, f"{v:,}" if isinstance(v, int) else str(v))
        console.print(table)
    finally:
        db.close()


@app.command()
def export(
    out: Path = typer.Argument(..., help="CSV file to write."),
    data_dir: Optional[Path] = DataDir,
):
    """Write a manifest of every listed file and its SHA-256."""
    db, _, _ = _paths(data_dir)
    cols = ["issuer", "source", "record_id", "published", "title", "label", "url", "status", "origin",
            "fetched_at", "sha256", "size", "n_pages", "text_pages"]
    n = 0
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for row in db.iter_manifest():
                if row["source"] == "archive" and row["status"] != "ok":
                    continue  # an archived entry is only worth listing with its file
                w.writerow([row[c] for c in cols])
                n += 1
        err.print(f"Wrote {n:,} rows to {out}")
    finally:
        db.close()


# -- known fakes ----------------------------------------------------------------------

@fakes_app.command("add")
def fakes_add(
    files: list[Path] = typer.Argument(..., exists=True, dir_okay=False, help="Fake files (images or PDFs)."),
    url: str = typer.Option(..., help="Where it was debunked (issuer notice, fact-check post)."),
    source: str = typer.Option(..., help="Who debunked it, e.g. 'UGC' or 'PIB Fact Check'."),
    date: Optional[str] = typer.Option(None, help="Date of the debunk (YYYY-MM-DD)."),
    claim: Optional[str] = typer.Option(None, help="What the fake claimed, in one line."),
    data_dir: Optional[Path] = DataDir,
):
    """Register debunked files. Files that match an official document are refused."""
    from .fakes import extract_document, register_fake
    from .matcher import Verifier
    from .ocr import load_ocr

    db, store, _ = _paths(data_dir)
    try:
        verifier = Verifier(db, store, use_ocr=False)
        ocr = load_ocr(None)
        for f in files:
            data = f.read_bytes()
            versions = [(data, str(f))]
            sheet = extract_document(data)
            if sheet is not None:
                versions.append((sheet, f"document cut out of {f.name}"))
            for blob, note in versions:
                sha, status = register_fake(db, store, blob, source=source, url=url, published=date, claim=claim,
                                            verifier=verifier, ocr=ocr, note=note)
                err.print(f"{f.name}: {status}" + (f" ({sha[:12]})" if sha else ""))
    finally:
        db.close()


@fakes_app.command("sync")
def fakes_sync(
    data_dir: Optional[Path] = DataDir,
    query: list[str] = typer.Option(None, "--query", "-q", help="Search terms (default: UGC names)."),
):
    """Import debunked fakes that mention the issuer from PIB Fact Check's public Telegram channel."""
    from .crawler import Fetcher
    from .fakes import DEFAULT_QUERIES, sync_pib
    from .matcher import Verifier
    from .ocr import load_ocr

    db, store, _ = _paths(data_dir)
    fetcher = Fetcher(delay=1.0)
    try:
        counts = sync_pib(db, store, fetcher, queries=tuple(query or DEFAULT_QUERIES),
                          verifier=Verifier(db, store, use_ocr=False), ocr=load_ocr(None),
                          on_post=lambda p: err.print(f"  {p.published}  {p.url}  {p.text[:80]}"))
        _print_counts("PIB Fact Check", dict(counts))
    finally:
        fetcher.close()
        db.close()


@fakes_app.command("list")
def fakes_list(data_dir: Optional[Path] = DataDir):
    """Known fakes and their debunks."""
    db, _, _ = _paths(data_dir)
    try:
        table = Table(show_lines=True)
        for col in ("Debunked", "By", "Claim", "File"):
            table.add_column(col)
        for r in db.known_fakes():
            table.add_row(r["published"] or "?", f"{r['source']}\n[dim]{r['url']}[/dim]", (r["claim"] or "")[:120],
                          r["sha256"][:12])
        console.print(table)
    finally:
        db.close()


# -- crowd reports --------------------------------------------------------------------

@reports_app.command("list")
def reports_list(
    status: str = typer.Option("new", help="new, promoted, dismissed or all."),
    data_dir: Optional[Path] = DataDir,
):
    """Reports waiting for review, most-reported first."""
    db, _, _ = _paths(data_dir)
    try:
        rows = db.reports(None if status == "all" else status)
        if not rows:
            err.print("No reports.")
            return
        table = Table(show_lines=True)
        for col in ("Report", "Times", "Verdict then", "Where seen / note", "Debunk link", "Status"):
            table.add_column(col)
        for r in rows:
            seen = "\n".join(filter(None, [r["seen_where"], r["note"]]))[:160]
            table.add_row(f"{r['id']}\n[dim]{r['updated_at'][:10]} · {r['channel']}[/dim]", str(r["count"]),
                          r["verdict"], seen, r["debunk_url"] or "", r["status"])
        console.print(table)
    finally:
        db.close()


@reports_app.command("promote")
def reports_promote(
    report_id: str = typer.Argument(..., help="Report id from `circverify reports list`."),
    url: str = typer.Option(..., help="Public debunk: an issuer notice or a fact-check post."),
    source: str = typer.Option(..., help="Who debunked it, e.g. 'UGC' or 'PIB Fact Check'."),
    date: Optional[str] = typer.Option(None, help="Date of the debunk (YYYY-MM-DD)."),
    claim: Optional[str] = typer.Option(None, help="What the fake claimed, in one line."),
    data_dir: Optional[Path] = DataDir,
):
    """Make a reported document a known fake, backed by a public debunk."""
    from .crowd import promote
    from .matcher import Verifier

    db, store, _ = _paths(data_dir)
    try:
        sha, status = promote(db, store, report_id, url=url, source=source, published=date, claim=claim,
                              verifier=Verifier(db, store, use_ocr=False))
        err.print(f"{report_id}: {status}" + (f" (known fake {sha[:12]})" if sha else ""))
        if not sha:
            raise typer.Exit(1)
    finally:
        db.close()


@reports_app.command("dismiss")
def reports_dismiss(
    report_id: str = typer.Argument(...),
    note: Optional[str] = typer.Option(None, help="Why, for the record."),
    data_dir: Optional[Path] = DataDir,
):
    """Close a report without marking anything fake."""
    from .crowd import dismiss

    db, _, _ = _paths(data_dir)
    try:
        if not dismiss(db, report_id, note):
            err.print(f"No report {report_id}.")
            raise typer.Exit(1)
        err.print(f"{report_id}: dismissed")
    finally:
        db.close()


@reports_app.command("export")
def reports_export(
    out: Path = typer.Argument(..., help="Folder to write the files and reports.csv into."),
    status: str = typer.Option("all", help="new, promoted, dismissed or all."),
    data_dir: Optional[Path] = DataDir,
):
    """Copy reported files and their notes out, as a dataset of documents that actually circulated."""
    from .crowd import export

    db, store, _ = _paths(data_dir)
    try:
        n = export(db, store, out, None if status == "all" else status)
        err.print(f"Wrote {n:,} reports to {out}")
    finally:
        db.close()


# -- serving --------------------------------------------------------------------------

@app.command()
def qr(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="An official document."),
    base_url: str = typer.Option(..., help="Public address of a circverify server, e.g. https://verify.example.org"),
    out: Path = typer.Option(Path("verify-qr.png"), help="PNG to write."),
    data_dir: Optional[Path] = DataDir,
):
    """A QR code to print on a circular; scanning it opens a page that verifies the file's hash."""
    import hashlib

    import qrcode

    sha = hashlib.sha256(file.read_bytes()).hexdigest()
    db, _, _ = _paths(data_dir)
    try:
        if not db.provenance(sha):
            err.print("[yellow]This exact file is not in the index yet;[/yellow] the QR page will say so until it is.")
    finally:
        db.close()
    link = f"{base_url.rstrip('/')}/v/{sha}"
    qrcode.make(link).save(out)
    err.print(f"QR code for {link} written to {out}")


@app.command()
def serve(
    data_dir: Optional[Path] = DataDir,
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
):
    """Run the upload-and-check web page."""
    try:
        import uvicorn
    except ImportError:
        err.print("Install the web extra: pip install 'circverify[web]'")
        raise typer.Exit(2) from None
    import os

    from .web import create_app
    from .whatsapp import WhatsAppConfig

    wa = WhatsAppConfig.from_env(public_url=os.environ.get("CIRCVERIFY_PUBLIC_URL"))
    if wa:
        err.print("WhatsApp bot: webhook at /whatsapp/webhook (put this server behind HTTPS for Meta).")
    # Public servers: CIRCVERIFY_RATE_LIMIT="30/600" (checks per seconds, per address) and
    # CIRCVERIFY_MAX_WAITING=6. Behind a proxy, set FORWARDED_ALLOW_IPS so client addresses are real.
    rate = None
    if spec := os.environ.get("CIRCVERIFY_RATE_LIMIT", "").strip():
        try:
            n, secs = (int(x) for x in spec.split("/", 1))
            rate = (n, secs) if n > 0 and secs > 0 else None
        except ValueError:
            err.print(f"Ignoring CIRCVERIFY_RATE_LIMIT={spec!r}: expected checks/seconds, like 30/600.")
    waiting = os.environ.get("CIRCVERIFY_MAX_WAITING", "").strip()
    app = create_app(data_dir or default_data_dir(), whatsapp=wa, rate_limit=rate,
                     max_waiting=int(waiting) if waiting.isdigit() else None)
    uvicorn.run(app, host=host, port=port)


@app.command()
def telegram(
    token: str = typer.Option(..., envvar="CIRCVERIFY_TELEGRAM_TOKEN", help="Bot token from @BotFather."),
    public_url: Optional[str] = typer.Option(None, help="Public address of the web app, for receipt links."),
    data_dir: Optional[Path] = DataDir,
):
    """Run a Telegram bot: send it a circular (photo, PDF or text) and it replies with the verdict."""
    from .telegram import TelegramBot

    root = data_dir or default_data_dir()
    err.print("Telegram bot running. Stop with Ctrl+C.")
    TelegramBot(token, root, public_url=public_url).run_forever()


if __name__ == "__main__":
    app()
