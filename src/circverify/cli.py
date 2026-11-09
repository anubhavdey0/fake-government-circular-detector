"""Command-line interface: ``circverify crawl | index | verify | search | stats | export | serve``."""

from __future__ import annotations

import csv
import json
import sys
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
    help="Verify circulars against an index of everything the issuer has actually published.",
)

# Windows consoles often use a legacy code page (cp1252) that cannot encode
# Hindi text or some symbols in notice titles. Degrade those characters
# instead of crashing halfway through a report.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

console = Console()
err = Console(stderr=True)

DataDir = typer.Option(None, "--data-dir", "-d", help="Index location (default: per-user app data, "
                                                       "or $CIRCVERIFY_DATA_DIR).")


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


def _run_downloads(db, store, fetcher, urls, workers, wayback=False):
    from .crawler import download_all

    if not urls:
        return {}
    with _progress() as progress:
        label = "Wayback lookups" if wayback else "Downloading"
        task = progress.add_task(label, total=len(urls), status="")
        tally: dict[str, int] = {}

        def on_result(out):
            tally[out.status] = tally.get(out.status, 0) + 1
            progress.update(task, advance=1, status=" ".join(f"{k}={v}" for k, v in sorted(tally.items())))

        return dict(download_all(db, store, fetcher, urls, workers=workers, wayback=wayback,
                                 on_result=on_result))


def _run_index(db, store, workers, ocr, reindex=False):
    from .indexer import index_pending

    with _progress() as progress:
        task = progress.add_task("Indexing", total=None, status="")
        errors = 0

        def on_result(res, total):
            nonlocal errors
            errors += res.error is not None
            progress.update(task, total=total, advance=1, status=f"errors={errors}")

        return index_pending(db, store, workers=workers, use_ocr=ocr, reindex=reindex, on_result=on_result)


@app.command()
def crawl(
    data_dir: Optional[Path] = DataDir,
    issuer: str = typer.Option("ugc", help="Issuer to crawl."),
    source: list[str] = typer.Option(None, "--source", "-s", help="Only these listing sources (repeatable)."),
    limit: Optional[int] = typer.Option(None, help="Download at most this many files (for a trial run)."),
    workers: int = typer.Option(2, help="Concurrent downloads."),
    delay: float = typer.Option(0.5, help="Minimum seconds between requests, across all workers."),
    retry_failed: bool = typer.Option(False, help="Also retry links that were dead last time."),
    wayback: bool = typer.Option(False, help="Recover dead links from the Wayback Machine."),
    download: bool = typer.Option(True, help="Download linked files (off: just sync listings)."),
    index: bool = typer.Option(True, help="Index new files after downloading."),
    index_workers: Optional[int] = typer.Option(None, help="Processes for indexing (default: CPUs - 1)."),
    ocr: Optional[bool] = typer.Option(None, "--ocr/--no-ocr", help="OCR scanned pages (default: if installed)."),
):
    """Sync the issuer's listings, download new files and index them."""
    from .crawler import Fetcher, sync_listings
    from .issuers import get_issuer

    db, store, root = _paths(data_dir)
    fetcher = Fetcher(delay=delay)
    try:
        with err.status("Fetching listings…"):
            res = sync_listings(db, get_issuer(issuer), fetcher, source or None)
        err.print(f"Listings: {res.seen} entries ({res.new} new), {res.links} links "
                  f"[dim]({', '.join(f'{k}={v}' for k, v in sorted(res.by_source.items()))})[/dim]")
        if download:
            urls = db.urls_to_fetch(retry_failed=retry_failed)
            if limit is not None:
                urls = urls[:limit]
            err.print(f"{len(urls)} files to download into {root}")
            counts = _run_downloads(db, store, fetcher, urls, workers)
            if counts:
                err.print("Downloads: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            if wayback:
                dead = db.dead_urls()
                if limit is not None:
                    dead = dead[:limit]
                err.print(f"Looking up {len(dead)} dead links in the Wayback Machine")
                counts = _run_downloads(db, store, fetcher, dead, workers=1, wayback=True)
                if counts:
                    err.print("Wayback: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if index:
            out = _run_index(db, store, index_workers, ocr)
            err.print(f"Indexed {out['indexed']} files ({out['errors']} could not be read)")
    finally:
        fetcher.close()
        db.close()


@app.command("index")
def index_cmd(
    data_dir: Optional[Path] = DataDir,
    workers: Optional[int] = typer.Option(None, help="Processes (default: CPUs - 1)."),
    ocr: Optional[bool] = typer.Option(None, "--ocr/--no-ocr", help="OCR scanned pages (default: if installed)."),
    reindex: bool = typer.Option(False, help="Re-index every file, not just new ones."),
):
    """Extract text and fingerprints from downloaded files."""
    db, store, _ = _paths(data_dir)
    try:
        out = _run_index(db, store, workers, ocr, reindex)
        err.print(f"Indexed {out['indexed']} files ({out['errors']} could not be read)")
    finally:
        db.close()


@app.command()
def verify(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="PDF or image to check."),
    data_dir: Optional[Path] = DataDir,
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
    html: Optional[Path] = typer.Option(None, help="Also write an HTML report with the visual diff."),
    refresh: bool = typer.Option(False, help="Sync the issuer's listings and fetch new files first."),
    ocr: Optional[bool] = typer.Option(None, "--ocr/--no-ocr", help="OCR the input if it has no text."),
):
    """Check a document against the index."""
    from .matcher import Verifier
    from .report import print_result, write_html

    db, store, _ = _paths(data_dir)
    try:
        if refresh:
            _refresh(db, store)
        if db.stats()["files_indexed"] == 0:
            err.print("[red]The index is empty.[/red] Run `circverify crawl` first.")
            raise typer.Exit(2)
        verifier = Verifier(db, store, use_ocr=ocr)
        result = verifier.verify(file.read_bytes(), filename=file.name)
        if as_json:
            sys.stdout.write(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n")
        else:
            print_result(result, console)
        if html:
            write_html(result, html)
            err.print(f"HTML report written to {html}")
        raise typer.Exit({"VERIFIED_EXACT": 0, "VERIFIED_CONTENT": 0, "ALTERED": 1}.get(result.verdict, 3))
    finally:
        db.close()


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
            raise typer.Exit(2)
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
                w.writerow([row[c] for c in cols])
                n += 1
        err.print(f"Wrote {n:,} rows to {out}")
    finally:
        db.close()


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
        raise typer.Exit(2)
    from .web import create_app

    uvicorn.run(create_app(data_dir or default_data_dir()), host=host, port=port)


if __name__ == "__main__":
    app()
