"""Parsing of UGC listing data. Records are trimmed copies of real API responses."""

from __future__ import annotations

from circverify.issuers.base import parse_date
from circverify.issuers.ugc import UGC, canonical_url, parse_api_record, parse_table_page


def test_canonical_url_variants():
    assert canonical_url("../pdfnews/123_Notice.pdf") == "https://www.ugc.gov.in/pdfnews/123_Notice.pdf"
    assert canonical_url("http://www.ugc.ac.in/pdfnews/9_A.pdf") == "https://www.ugc.gov.in/pdfnews/9_A.pdf"
    assert canonical_url("https://www.ugc.gov.in/pdfnews/UGC Chart.pdf") == \
        "https://www.ugc.gov.in/pdfnews/UGC%20Chart.pdf"
    assert canonical_url("https://www.ugc.gov.in/pdfnews/UGC%20Chart.pdf") == \
        "https://www.ugc.gov.in/pdfnews/UGC%20Chart.pdf"
    assert canonical_url("https://deb.ugc.ac.in/Uploads/x.pdf") == "https://deb.ugc.ac.in/Uploads/x.pdf"
    assert canonical_url("https://www.ugcnetonline.in/notice.pdf") is None  # other site
    assert canonical_url("https://www.ugc.gov.in/page.aspx") is None  # not a document
    assert canonical_url("javascript:window.open('x.pdf')") is None


def test_single_link_title():
    item = {
        "ID": "6500",
        "title": '<a href=../pdfnews/7942181_Rashtriy-Ekta-Diwas-National-Unity-Day-2026.pdf target="_blank" '
                 'class="red-color">UGC letter regarding: Observance of Rashtriya Ekta Diwas</a><br/>',
        "pdfnews": "7942181_Rashtriy-Ekta-Diwas-National-Unity-Day-2026.pdf",
        "created": "2026-10-30",
    }
    n = parse_api_record(item, "notices")
    assert n.title == "UGC letter regarding: Observance of Rashtriya Ekta Diwas"
    assert n.published == "2026-10-30"
    assert [l.url for l in n.links] == [
        "https://www.ugc.gov.in/pdfnews/7942181_Rashtriy-Ekta-Diwas-National-Unity-Day-2026.pdf"]
    assert n.links[0].label is None


def test_title_with_several_labelled_files():
    item = {
        "ID": "6400",
        "title": "UGC letter regarding Intervention on Non-Compliance with UGC Fee Refund Policy<br/>"
                 '<a href=../pdfnews/4373827_Letter-to-all-HEIs.pdf target="_blank">Letter-to-all-HEIs</a><br/>'
                 '<a href=../pdfnews/3768850_List-of-HEIs.pdf target="_blank">List-of-HEIs</a><br/>',
        "pdfnews": "4373827_Letter-to-all-HEIs.pdf",
        "pdfnews1": "3768850_List-of-HEIs.pdf",
        "created": "2026-07-01",
    }
    n = parse_api_record(item, "notices")
    assert n.title == "UGC letter regarding Intervention on Non-Compliance with UGC Fee Refund Policy"
    assert [(l.url.rsplit("/", 1)[1], l.label) for l in n.links] == [
        ("4373827_Letter-to-all-HEIs.pdf", "Letter-to-all-HEIs"),
        ("3768850_List-of-HEIs.pdf", "List-of-HEIs"),
    ]


def test_language_list_on_old_domain_and_placeholder_date():
    item = {
        "ID": "88",
        "title": 'UGC Notice reg.: Unrecognized Status of IIPM, New Delhi <ul>\r\n'
                 '<li><a href="http://www.ugc.ac.in/pdfnews/9280318_Public-Notice-IIPM-English.pdf">English</a></li>\r\n'
                 '<li><a href="http://www.ugc.ac.in/pdfnews/0792788_Public-Notice-IIPM-Hindi.pdf">Hindi</a></li>\r\n</ul>',
        "pdfnews": None,
        "created": "1900-01-01",
    }
    n = parse_api_record(item, "notices")
    assert n.title == "UGC Notice reg.: Unrecognized Status of IIPM, New Delhi"
    assert n.published is None
    assert [l.label for l in n.links] == ["English", "Hindi"]
    assert all(l.url.startswith("https://www.ugc.gov.in/pdfnews/") for l in n.links)


def test_table_page_rows():
    html = """<div id="content"><table><tbody>
      <tr><td>1</td><td>UGC (Minimum Standards of Instruction) Regulations, 2025</td>
          <td>Published on 02/04/2025</td>
          <td><a href="https://www.ugc.gov.in/pdfnews/5576784_UG-and-PG-Regulations-2025.pdf">View</a></td></tr>
      <tr><td>2</td><td>Press Release: Status regarding UGC Online Registration</td><td>2012-11-17</td>
          <td><a href="../pdfnews/4699712_Press-Release.pdf">Press Release</a></td></tr>
      <tr><td>3</td><td>Row without a document</td><td>2012-01-01</td></tr>
    </tbody></table></div>
    <ul class="nav"><li><a href="https://www.ugc.gov.in/pdfnews/menu.pdf">Menu link</a></li></ul>"""
    rows = list(parse_table_page(html, "regulations", "https://www.ugc.gov.in/regulations"))
    assert [(r.title, r.published) for r in rows] == [
        ("UGC (Minimum Standards of Instruction) Regulations, 2025", "2025-04-02"),
        ("Press Release: Status regarding UGC Online Registration", "2012-11-17"),
    ]
    assert rows[1].links[0].url == "https://www.ugc.gov.in/pdfnews/4699712_Press-Release.pdf"


def test_parse_date_formats():
    assert parse_date("Published on 04/04/2025") == "2025-04-04"
    assert parse_date("2026-10-30") == "2026-10-30"
    assert parse_date("1900-01-01") is None
    assert parse_date("31/02/2024") is None


def test_official_hosts():
    ugc = UGC()
    assert ugc.is_official_url("https://www.ugc.gov.in/x.pdf")
    assert ugc.is_official_url("https://deb.ugc.ac.in/x.pdf")
    assert not ugc.is_official_url("https://ugc.gov.in.verify-now.xyz/x.pdf")
