import sqlite3

import pytest

from db.loaders.icd_alias_extensions import ALIASES, SYNTHETIC_ICD, load_icd_alias_extensions

SCHEMA = """
CREATE TABLE icd (
    code    TEXT PRIMARY KEY,
    name_en TEXT NOT NULL
);
CREATE TABLE icd_aliases (
    id       INTEGER PRIMARY KEY,
    icd_code TEXT NOT NULL,
    alias    TEXT NOT NULL,
    FOREIGN KEY (icd_code) REFERENCES icd(code)
);
"""


def _make_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)


def _seed_icd_codes(db, extra_codes=None):
    """Seed icd table with codes referenced in ALIASES (not SYNTHETIC_ICD — the loader inserts those)."""
    codes = {code for code, _ in ALIASES}
    if extra_codes:
        codes |= set(extra_codes)
    with sqlite3.connect(db) as conn:
        for code in codes:
            conn.execute(
                "INSERT OR IGNORE INTO icd (code, name_en) VALUES (?, ?)",
                (code, f"Dummy {code}"),
            )


def test_inserts_aliases(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed_icd_codes(db)
    n = load_icd_alias_extensions(db)
    assert n > 0


def test_idempotent(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed_icd_codes(db)
    n1 = load_icd_alias_extensions(db)
    n2 = load_icd_alias_extensions(db)
    assert n2 == 0


def test_inserts_synthetic_icd_codes(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed_icd_codes(db)
    load_icd_alias_extensions(db)
    with sqlite3.connect(db) as conn:
        codes = {r[0] for r in conn.execute("SELECT code FROM icd")}
    for code, _, _ in SYNTHETIC_ICD:
        assert code in codes


def test_synthetic_aliases_inserted(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed_icd_codes(db)
    load_icd_alias_extensions(db)
    with sqlite3.connect(db) as conn:
        alias_map = {}
        for icd_code, alias in conn.execute("SELECT icd_code, alias FROM icd_aliases"):
            alias_map.setdefault(icd_code, set()).add(alias.lower())
    for code, _, aliases in SYNTHETIC_ICD:
        for alias in aliases:
            assert alias.lower() in alias_map.get(code, set()), \
                f"Missing alias '{alias}' for {code}"


def test_covid19_synthetic_entry(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed_icd_codes(db)
    load_icd_alias_extensions(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT name_en FROM icd WHERE code = 'X.COVID19'"
        ).fetchone()
    assert row is not None
    assert row[0] == "COVID-19"


def test_vhf_aliases_inserted(tmp_path):
    db = str(tmp_path / "test.db")
    _make_db(db)
    _seed_icd_codes(db)
    load_icd_alias_extensions(db)
    with sqlite3.connect(db) as conn:
        aliases = {
            r[0].lower()
            for r in conn.execute(
                "SELECT alias FROM icd_aliases WHERE icd_code = '1D86'"
            )
        }
    assert "haemorrhagic fever syndrome" in aliases
    assert "acute haemorrhagic fever syndrome" in aliases
