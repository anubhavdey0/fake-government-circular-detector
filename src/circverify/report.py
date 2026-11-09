"""Human-readable output: terminal and standalone HTML."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .matcher import ALTERED, NO_MATCH, UNREADABLE, VERIFIED_CONTENT, VERIFIED_EXACT, VerificationResult

VERDICT_LABEL = {
    VERIFIED_EXACT: ("Verified: exact copy", "green"),
    VERIFIED_CONTENT: ("Verified: same content", "green"),
    ALTERED: ("Altered", "red"),
    NO_MATCH: ("Not found", "yellow"),
    UNREADABLE: ("Unreadable", "bright_black"),
}


def print_result(res: VerificationResult, console: Console) -> None:
    label, color = VERDICT_LABEL[res.verdict]
    head = Text.assemble((f" {label.upper()} ", f"bold white on {color}"), "  ", (res.filename or "", "dim"))
    body: list = [head, Text(""), Text(res.summary)]
    console.print(Panel(Group(*body), border_style=color, padding=(1, 2)))

    m = res.match
    if m and m.provenance:
        t = Table(title="Official record", title_justify="left", show_header=False, box=None, padding=(0, 2))
        t.add_column(style="bold", no_wrap=True)
        t.add_column(overflow="fold")
        for i, p in enumerate(m.provenance[:5]):
            if i:
                t.add_row("", "")
            t.add_row("Title", escape(p.title))
            t.add_row("Published", p.published or "date not listed")
            t.add_row("Listed under", f"{p.issuer.upper()} / {p.source}" + (f" ({p.label})" if p.label else ""))
            t.add_row("URL", escape(p.url))
        t.add_row("SHA-256", m.sha256)
        if m.text_containment is not None and res.verdict != VERIFIED_EXACT:
            t.add_row("Text overlap", f"{m.text_containment:.1%} of the submission's word sequences")
        console.print(t)

    if m and m.text and m.text.changes:
        t = Table(title=f"Text changes ({len(m.text.changes)})", title_justify="left", show_lines=True)
        t.add_column("#", justify="right", style="dim")
        t.add_column("Official says", style="green", overflow="fold")
        t.add_column("Submission says", style="red", overflow="fold")
        t.add_column("Where", style="dim", overflow="fold")
        t.add_column("Why it matters", overflow="fold")
        for i, c in enumerate(m.text.changes[:40], 1):
            why = ", ".join(c.reasons) if c.reasons else "wording"
            t.add_row(
                str(i),
                escape(c.official) or "[dim](nothing)[/dim]",
                escape(c.submitted) or "[dim](removed)[/dim]",
                escape(f"…{c.context_before} […] {c.context_after}…"),
                f"[bold red]{why}[/bold red]" if c.severity == "high" else why,
            )
        if len(m.text.changes) > 40:
            t.caption = f"{len(m.text.changes) - 40} more not shown; use --json or --html for all."
        console.print(t)

    if m and m.visual:
        t = Table(title="Page images", title_justify="left")
        t.add_column("Submitted page", justify="right")
        t.add_column("Official page", justify="right")
        t.add_column("Changed regions", justify="right")
        t.add_column("Changed area", justify="right")
        t.add_column("Result")
        for v in m.visual:
            verdict = ("different page" if not v.same_document
                       else ("changed" if v.boxes else "identical"))
            style = {"identical": "green", "changed": "red", "different page": "yellow"}[verdict]
            t.add_row(str(v.query_page), str(v.official_page), str(len(v.boxes)),
                      f"{v.changed_ratio:.1%}", f"[{style}]{verdict}[/{style}]")
        console.print(t)

    if res.notes:
        console.print(Text("Notes", style="bold"))
        for n in res.notes:
            console.print(Text("  • ") + Text(n))
    if res.alternatives:
        console.print(Text("Other close official documents", style="bold"))
        for a in res.alternatives:
            console.print(f"  • {escape(a['title'] or a['sha256'][:12])} ({a['published'] or '?'}) "
                          f"[dim]{escape(a['url'] or '')}[/dim]")


def write_html(res: VerificationResult, path: Path) -> None:
    from .htmlreport import render_report

    Path(path).write_text(render_report(res), encoding="utf-8")
