from __future__ import annotations

import csv
import json

from typer.testing import CliRunner

from conftest import DEADLINE_LINE, OFFICIAL, UNRELATED
from pdfgen import make_pdf

from circverify.cli import app

runner = CliRunner()


def test_verify_exit_codes_json_and_html(index, tmp_path, official_pdf):
    d = str(tmp_path)
    good = tmp_path / "good.pdf"
    good.write_bytes(official_pdf)
    r = runner.invoke(app, ["verify", str(good), "--data-dir", d])
    assert r.exit_code == 0, r.output
    assert "VERIFIED: EXACT COPY" in r.output

    lines = list(OFFICIAL)
    lines[DEADLINE_LINE] = "of applications under the fellowship scheme to 25 November 2026."
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(make_pdf([lines]))
    html = tmp_path / "report.html"
    r = runner.invoke(app, ["verify", str(bad), "--data-dir", d, "--json", "--html", str(html)])
    assert r.exit_code == 1
    payload = json.loads(r.stdout)
    assert payload["verdict"] == "ALTERED"
    assert "Verification report" in html.read_text(encoding="utf-8")

    other = tmp_path / "other.pdf"
    other.write_bytes(make_pdf([UNRELATED]))
    assert runner.invoke(app, ["verify", str(other), "--data-dir", d]).exit_code == 3


def test_stats_search_export(index, tmp_path):
    d = str(tmp_path)
    s = json.loads(runner.invoke(app, ["stats", "--data-dir", d, "--json"]).stdout)
    assert s["files_indexed"] == 2 and s["notices"] == 2

    r = runner.invoke(app, ["search", "fellowship", "--data-dir", d])
    assert r.exit_code == 0 and "Extension of last date" in r.output

    out = tmp_path / "manifest.csv"
    assert runner.invoke(app, ["export", str(out), "--data-dir", d]).exit_code == 0
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 2 and all(len(r["sha256"]) == 64 for r in rows)


def test_verify_on_empty_index(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(make_pdf([OFFICIAL]))
    r = runner.invoke(app, ["verify", str(f), "--data-dir", str(tmp_path / "empty")])
    assert r.exit_code == 2


def test_verify_a_folder(index, tmp_path, official_pdf):
    folder = tmp_path / "inbox"
    folder.mkdir()
    (folder / "a-good.pdf").write_bytes(official_pdf)
    lines = list(OFFICIAL)
    lines[DEADLINE_LINE] = "of applications under the fellowship scheme to 25 November 2026."
    (folder / "b-forged.pdf").write_bytes(make_pdf([lines]))
    (folder / "c-other.pdf").write_bytes(make_pdf([UNRELATED]))
    (folder / "notes.txt").write_text("not a document")
    out = tmp_path / "results.csv"
    r = runner.invoke(app, ["verify", str(folder), "--data-dir", str(tmp_path), "--csv", str(out)])
    assert r.exit_code == 1, r.output  # the most serious verdict: altered
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["verdict"] for r in rows] == ["VERIFIED_EXACT", "ALTERED", "NO_MATCH"]
    assert rows[0]["official_url"].startswith("https://www.ugc.gov.in/")
    r = runner.invoke(app, ["verify", str(folder / "a-good.pdf"), str(folder / "c-other.pdf"), "--data-dir",
                            str(tmp_path), "--json"])
    assert r.exit_code == 3 and len(json.loads(r.stdout)) == 2
