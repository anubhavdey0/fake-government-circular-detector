from __future__ import annotations

from circverify.extract import text_quality
from circverify.fields import extract_fields, find_dates, ref_key

LETTER = """
विश्वविद्यालय अनुदान आयोग
University Grants Commission
D.O.No.30-57/2012(CVO)                                  21st October, 2026
Subject: Observance of Vigilance Awareness Week 2026
Dear Madam/Sir,
As you are aware, the Central Vigilance Commission observes Vigilance Awareness Week every year.
Yours sincerely,
(Prof. Manish R. Joshi)
Secretary
"""


def test_letter_fields():
    f = extract_fields(LETTER)
    assert f["ref_no"] == "30-57/2012(CVO)" and f["ref_key"] == "30572012CVO"
    assert f["doc_date"] == "2026-10-21"
    assert f["subject"] == "Observance of Vigilance Awareness Week 2026"
    assert f["signatory"] == "Prof. Manish R. Joshi" and f["designation"] == "Secretary"


def test_reference_variants_from_scanner_text():
    assert extract_fields("D. 0. No.14-15/2011(CPP-II) January 2013")["ref_no"] == "14-15/2011(CPP-II)"
    assert extract_fields("F No 5- 151/2012(HRP) Dr. Meti")["ref_key"] == "51512012HRP"
    assert extract_fields("No.F.1-59/66(CDN) dated")["ref_no"] == "1-59/66(CDN)"
    assert ref_key("1-15/2026 (CPP-II)") == ref_key("1-15/2026(CPP-II)")


def test_date_formats_day_first():
    iso = [d for _, d in find_dates("30.10.2026, October 5, 2026 and 3rd of March 2014")]
    assert iso == ["2026-10-30", "2026-10-05", "2014-03-03"]
    assert find_dates("31/02/2024") == []


def test_signatory_without_title_and_misread_bracket():
    f = extract_fields("Yours faithfully,\n(Rajnish JainI\nSecretary")
    assert f["signatory"] == "Rajnish JainI" or f["signatory"] == "Rajnish Jain"


def test_text_quality_flags_unreadable_layers():
    english = "The Commission has decided to extend the last date for online submission of applications " * 4
    kruti_dev = "fo'ofo|ky; vu qnkuvk;ksx] ubZ fnYyh&110 002 fe0 60&1@2014¼,l,&II/III½ fofHkUUkv/;srko`fÙk;ks a] " * 4
    cipher = "^qlTfq9Z>q9i9Z>q aL>q!>SaZ9Pq6MKMQ9S<>q !TRRM^^MTSq !6!qaL>q 9W>kqMSa>KZMalq " * 8
    assert text_quality(english) > 0.8
    assert text_quality(kruti_dev) < 0.2 and text_quality(cipher) < 0.2
    assert text_quality("too short") is None


def test_rereading_a_file_keeps_fields_the_new_text_does_not_yield(tmp_path):
    from circverify.db import Database

    db = Database(tmp_path / "i.sqlite3")
    db.save_fields("a" * 64, {"ref_no": "F.1-2/2015", "ref_key": "122015", "doc_date": "2015-03-01"})
    db.save_fields("a" * 64, {"ref_no": None, "ref_key": None, "doc_date": "2015-03-02"}, keep_existing=True)
    row = db.fields("a" * 64)
    assert row["ref_no"] == "F.1-2/2015" and row["ref_key"] == "122015" and row["doc_date"] == "2015-03-02"
    db.save_fields("a" * 64, {"ref_no": None, "ref_key": None, "doc_date": None})  # a full re-index replaces
    assert db.fields("a" * 64)["ref_no"] is None
    db.close()
