# python -m pytest tests/test_iso_loader.py

import io
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from db.loaders.iso_loader import _fetch_download_url, _parse, load_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS iso (
    code    TEXT PRIMARY KEY,
    name_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS iso_aliases (
    iso_code       TEXT PRIMARY KEY,
    english_short  TEXT,
    french_short   TEXT,
    spanish_short  TEXT,
    english_formal TEXT,
    m49_english    TEXT,
    m49_french     TEXT,
    m49_spanish    TEXT,
    FOREIGN KEY (iso_code) REFERENCES iso(code)
);
"""

_CSV_HEADER = (
    "ISO 3166-1 Alpha 3-Codes,Preferred Term,"
    "English Short,French Short,Spanish Short,"
    "English Formal,M49 English,M49 French,M49 Spanish\n"
)


def _make_csv(*rows):
    lines = [_CSV_HEADER]
    for r in rows:
        lines.append(",".join(r) + "\n")
    return "".join(lines).encode("utf-8")


def _make_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)


# --- _fetch_download_url ---

def test_fetch_download_url_returns_url():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": {"url": "https://example.com/iso.csv"}}
    with patch("db.loaders.iso_loader.requests.get", return_value=mock_resp):
        url = _fetch_download_url()
    assert url == "https://example.com/iso.csv"


def test_fetch_download_url_raises_on_http_error():
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("HTTP 500")
    with patch("db.loaders.iso_loader.requests.get", return_value=mock_resp):
        with pytest.raises(Exception, match="HTTP 500"):
            _fetch_download_url()


# --- _parse (CSV) ---

def test_parse_csv_returns_code_name_aliases():
    content = _make_csv(
        ("AFG", "Afghanistan", "Afghanistan", "Afghanistan fr", "Afganistán",
         "Islamic Republic of Afghanistan", "Afg M49 en", "Afg M49 fr", "Afg M49 es"),
    )
    rows = _parse(content, "iso.csv")
    assert len(rows) == 1
    code, name, aliases = rows[0]
    assert code == "AFG"
    assert name == "Afghanistan"
    assert aliases["english_short"] == "Afghanistan"
    assert aliases["french_short"] == "Afghanistan fr"
    assert aliases["m49_english"] == "Afg M49 en"


def test_parse_csv_multiple_rows():
    content = _make_csv(
        ("AFG", "Afghanistan", "A", "B", "C", "D", "E", "F", "G"),
        ("ALB", "Albania",     "H", "I", "J", "K", "L", "M", "N"),
    )
    rows = _parse(content, "countries.csv")
    assert len(rows) == 2
    assert rows[0][0] == "AFG"
    assert rows[1][0] == "ALB"


def test_parse_csv_skips_row_with_missing_code():
    content = _make_csv(
        ("", "No Code Country", "s", "f", "e", "fo", "m1", "m2", "m3"),
    )
    assert _parse(content, "data.csv") == []


def test_parse_csv_skips_row_with_missing_name():
    content = _make_csv(
        ("ZZZ", "", "s", "f", "e", "fo", "m1", "m2", "m3"),
    )
    assert _parse(content, "data.csv") == []


def test_parse_csv_empty_alias_becomes_none():
    content = _make_csv(
        ("AFG", "Afghanistan", "", "", "", "", "", "", ""),
    )
    _, _, aliases = _parse(content, "iso.csv")[0]
    assert aliases["english_short"] is None
    assert aliases["m49_french"] is None


def test_parse_csv_header_only_returns_empty():
    content = _CSV_HEADER.encode("utf-8")
    assert _parse(content, "iso.csv") == []


# --- _parse (XLSX) ---

def test_parse_xlsx_basic():
    pytest.importorskip("openpyxl")
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([
        "ISO 3166-1 Alpha 3-Codes", "Preferred Term",
        "English Short", "French Short", "Spanish Short",
        "English Formal", "M49 English", "M49 French", "M49 Spanish",
    ])
    ws.append(["CHE", "Switzerland", "Switzerland", "Suisse", "Suiza",
               "Swiss Confederation", "Switzerland M49", "Suisse M49", "Suiza M49"])

    buf = io.BytesIO()
    wb.save(buf)

    rows = _parse(buf.getvalue(), "iso.xlsx")
    assert len(rows) == 1
    code, name, aliases = rows[0]
    assert code == "CHE"
    assert name == "Switzerland"
    assert aliases["french_short"] == "Suisse"


def test_parse_xlsx_skips_missing_code():
    pytest.importorskip("openpyxl")
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([
        "ISO 3166-1 Alpha 3-Codes", "Preferred Term",
        "English Short", "French Short", "Spanish Short",
        "English Formal", "M49 English", "M49 French", "M49 Spanish",
    ])
    ws.append([None, "No Code", "s", "f", "e", "fo", "m1", "m2", "m3"])

    buf = io.BytesIO()
    wb.save(buf)

    rows = _parse(buf.getvalue(), "iso.xlsx")
    assert rows == []


# --- load_iso ---

def _make_requests_side_effect(csv_content):
    mock_api = MagicMock()
    mock_api.json.return_value = {"result": {"url": "https://example.com/iso.csv?v=1"}}

    mock_file = MagicMock()
    mock_file.content = csv_content

    def fake_get(url, **kwargs):
        return mock_api if "humdata" in url else mock_file

    return fake_get


def test_load_iso_inserts_iso_rows(tmp_path):
    db_file = str(tmp_path / "test.db")
    _make_db(db_file)

    csv_content = _make_csv(
        ("AFG", "Afghanistan", "Afghanistan", "Afghanistan fr", "Afganistán",
         "Islamic Republic of Afghanistan", "Afg M49", "Afg M49 fr", "Afg M49 es"),
        ("ALB", "Albania", "Albania", "Albanie", "Albania es",
         "Republic of Albania", "Alb M49", "Alb M49 fr", "Alb M49 es"),
    )

    with patch("db.loaders.iso_loader.requests.get",
               side_effect=_make_requests_side_effect(csv_content)):
        count = load_iso(db_file)

    assert count == 2
    with sqlite3.connect(db_file) as conn:
        rows = conn.execute("SELECT code, name_en FROM iso ORDER BY code").fetchall()
    assert rows == [("AFG", "Afghanistan"), ("ALB", "Albania")]


def test_load_iso_inserts_alias_rows(tmp_path):
    db_file = str(tmp_path / "test.db")
    _make_db(db_file)

    csv_content = _make_csv(
        ("AFG", "Afghanistan", "Afghanistan", "Afghanistan fr", "Afganistán",
         "Islamic Republic of Afghanistan", "Afg M49", "Afg M49 fr", "Afg M49 es"),
    )

    with patch("db.loaders.iso_loader.requests.get",
               side_effect=_make_requests_side_effect(csv_content)):
        load_iso(db_file)

    with sqlite3.connect(db_file) as conn:
        row = conn.execute(
            "SELECT french_short, m49_english FROM iso_aliases WHERE iso_code='AFG'"
        ).fetchone()
    assert row == ("Afghanistan fr", "Afg M49")


def test_load_iso_returns_zero_for_empty_file(tmp_path):
    db_file = str(tmp_path / "test.db")
    _make_db(db_file)

    with patch("db.loaders.iso_loader.requests.get",
               side_effect=_make_requests_side_effect(_CSV_HEADER.encode("utf-8"))):
        count = load_iso(db_file)

    assert count == 0


def test_load_iso_upserts_existing_row(tmp_path):
    db_file = str(tmp_path / "test.db")
    _make_db(db_file)
    with sqlite3.connect(db_file) as conn:
        conn.execute("INSERT INTO iso (code, name_en) VALUES ('AFG', 'Old name')")

    csv_content = _make_csv(
        ("AFG", "Afghanistan", "Afghanistan", "", "", "", "", "", ""),
    )

    with patch("db.loaders.iso_loader.requests.get",
               side_effect=_make_requests_side_effect(csv_content)):
        load_iso(db_file)

    with sqlite3.connect(db_file) as conn:
        name = conn.execute("SELECT name_en FROM iso WHERE code='AFG'").fetchone()[0]
    assert name == "Afghanistan"
