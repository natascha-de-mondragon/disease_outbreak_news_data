# python -m pytest tests/test_icd_loader.py


import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from db.loaders.icd_loader import (
    _fetch,
    _get_token,
    _headers,
    _read_secrets,
    _resolve_root,
    _str_value,
    load_icd,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS icd (
    code    TEXT PRIMARY KEY,
    name_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS icd_aliases (
    id       INTEGER PRIMARY KEY,
    icd_code TEXT NOT NULL,
    alias    TEXT NOT NULL,
    FOREIGN KEY (icd_code) REFERENCES icd(code)
);
"""


def _make_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)


# --- _read_secrets ---

def test_read_secrets(tmp_path):
    f = tmp_path / "secrets.yaml"
    f.write_text("ClientId: my_id\nClientSecret: my_secret\n")
    client_id, client_secret = _read_secrets(str(f))
    assert client_id == "my_id"
    assert client_secret == "my_secret"


# --- _get_token ---

def test_get_token_returns_access_token():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"access_token": "tok123"}
    with patch("db.loaders.icd_loader.requests.post", return_value=mock_resp) as mock_post:
        token = _get_token("id", "secret")
    assert token == "tok123"
    posted_data = mock_post.call_args[1]["data"]
    assert posted_data["grant_type"] == "client_credentials"
    assert posted_data["client_id"] == "id"


def test_get_token_raises_on_http_error():
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("HTTP 401")
    with patch("db.loaders.icd_loader.requests.post", return_value=mock_resp):
        with pytest.raises(Exception, match="HTTP 401"):
            _get_token("bad_id", "bad_secret")


# --- _headers ---

def test_headers_authorization_format():
    h = _headers("mytoken")
    assert h["Authorization"] == "Bearer mytoken"


def test_headers_required_keys():
    h = _headers("tok")
    assert "Accept" in h
    assert "Accept-Language" in h
    assert "API-Version" in h


# --- _fetch ---

def test_fetch_returns_parsed_json():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"code": "1A00", "title": {"@value": "Cholera"}}
    with patch("db.loaders.icd_loader.requests.get", return_value=mock_resp):
        result = _fetch("https://example.com/entity", "tok")
    assert result["code"] == "1A00"


def test_fetch_raises_on_http_error():
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("HTTP 404")
    with patch("db.loaders.icd_loader.requests.get", return_value=mock_resp):
        with pytest.raises(Exception, match="HTTP 404"):
            _fetch("https://example.com/missing", "tok")


# --- _str_value ---

@pytest.mark.parametrize("obj,expected", [
    ({"@value": "Cholera"}, "Cholera"),
    ({"@value": ""}, ""),
    ({"other_key": "x"}, ""),
    ("plain string", "plain string"),
    (None, ""),
    (42, "42"),
])
def test_str_value(obj, expected):
    assert _str_value(obj) == expected


# --- _resolve_root ---

def test_resolve_root_returns_linearization_root_when_no_code():
    result = _resolve_root("tok", None)
    assert "mms" in result


def test_resolve_root_resolves_code_to_stem_id():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"stemId": "https://id.who.int/icd/entity/257068234"}
    with patch("db.loaders.icd_loader.requests.get", return_value=mock_resp):
        result = _resolve_root("tok", "01")
    assert result == "https://id.who.int/icd/entity/257068234"


# --- load_icd ---

_ENTITIES = {
    "https://root": {
        "code": "1A00",
        "title": {"@value": "Cholera"},
        "synonym": [{"label": {"@value": "Cholera disease"}}],
        "child": ["https://child1"],
    },
    "https://child1": {
        "code": "1A01",
        "title": {"@value": "Severe cholera"},
        "synonym": [],
        "child": [],
    },
}


def test_load_icd_inserts_codes_and_names(tmp_path):
    db_file = str(tmp_path / "test.db")
    _make_db(db_file)
    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text("ClientId: id\nClientSecret: secret\n")

    with (
        patch("db.loaders.icd_loader._read_secrets", return_value=("id", "secret")),
        patch("db.loaders.icd_loader._get_token", return_value="tok"),
        patch("db.loaders.icd_loader._resolve_root", return_value="https://root"),
        patch("db.loaders.icd_loader._fetch", side_effect=lambda url, tok: _ENTITIES[url]),
        patch("db.loaders.icd_loader.time.sleep"),
    ):
        count = load_icd(db_file, str(secrets_file), root_code="01")

    assert count == 2
    with sqlite3.connect(db_file) as conn:
        rows = conn.execute("SELECT code, name_en FROM icd ORDER BY code").fetchall()
    assert rows == [("1A00", "Cholera"), ("1A01", "Severe cholera")]


def test_load_icd_inserts_aliases(tmp_path):
    db_file = str(tmp_path / "test.db")
    _make_db(db_file)
    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text("ClientId: id\nClientSecret: secret\n")

    with (
        patch("db.loaders.icd_loader._read_secrets", return_value=("id", "secret")),
        patch("db.loaders.icd_loader._get_token", return_value="tok"),
        patch("db.loaders.icd_loader._resolve_root", return_value="https://root"),
        patch("db.loaders.icd_loader._fetch", side_effect=lambda url, tok: _ENTITIES[url]),
        patch("db.loaders.icd_loader.time.sleep"),
    ):
        load_icd(db_file, str(secrets_file), root_code="01")

    with sqlite3.connect(db_file) as conn:
        aliases = conn.execute(
            "SELECT alias FROM icd_aliases WHERE icd_code='1A00'"
        ).fetchall()
    assert aliases == [("Cholera disease",)]


def test_load_icd_skips_entity_without_code(tmp_path):
    db_file = str(tmp_path / "test.db")
    _make_db(db_file)
    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text("ClientId: id\nClientSecret: secret\n")

    entity = {"title": {"@value": "No code entity"}, "synonym": [], "child": []}

    with (
        patch("db.loaders.icd_loader._read_secrets", return_value=("id", "secret")),
        patch("db.loaders.icd_loader._get_token", return_value="tok"),
        patch("db.loaders.icd_loader._resolve_root", return_value="https://root"),
        patch("db.loaders.icd_loader._fetch", return_value=entity),
        patch("db.loaders.icd_loader.time.sleep"),
    ):
        count = load_icd(db_file, str(secrets_file))

    assert count == 0
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT COUNT(*) FROM icd").fetchone()[0] == 0


def test_load_icd_traverses_children(tmp_path):
    db_file = str(tmp_path / "test.db")
    _make_db(db_file)
    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text("ClientId: id\nClientSecret: secret\n")

    entities = {
        "https://root": {
            "code": "1A00", "title": {"@value": "Parent"},
            "synonym": [], "child": ["https://c1", "https://c2"],
        },
        "https://c1": {
            "code": "1A01", "title": {"@value": "Child 1"},
            "synonym": [], "child": [],
        },
        "https://c2": {
            "code": "1A02", "title": {"@value": "Child 2"},
            "synonym": [], "child": [],
        },
    }

    with (
        patch("db.loaders.icd_loader._read_secrets", return_value=("id", "secret")),
        patch("db.loaders.icd_loader._get_token", return_value="tok"),
        patch("db.loaders.icd_loader._resolve_root", return_value="https://root"),
        patch("db.loaders.icd_loader._fetch", side_effect=lambda url, tok: entities[url]),
        patch("db.loaders.icd_loader.time.sleep"),
    ):
        count = load_icd(db_file, str(secrets_file))

    assert count == 3


def test_load_icd_upserts_on_duplicate_code(tmp_path):
    db_file = str(tmp_path / "test.db")
    _make_db(db_file)
    with sqlite3.connect(db_file) as conn:
        conn.execute("INSERT INTO icd (code, name_en) VALUES ('1A00', 'Old name')")

    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text("ClientId: id\nClientSecret: secret\n")

    entity = {"code": "1A00", "title": {"@value": "New name"}, "synonym": [], "child": []}

    with (
        patch("db.loaders.icd_loader._read_secrets", return_value=("id", "secret")),
        patch("db.loaders.icd_loader._get_token", return_value="tok"),
        patch("db.loaders.icd_loader._resolve_root", return_value="https://root"),
        patch("db.loaders.icd_loader._fetch", return_value=entity),
        patch("db.loaders.icd_loader.time.sleep"),
    ):
        load_icd(db_file, str(secrets_file))

    with sqlite3.connect(db_file) as conn:
        name = conn.execute("SELECT name_en FROM icd WHERE code='1A00'").fetchone()[0]
    assert name == "New name"
