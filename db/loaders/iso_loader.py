import csv
import io
import sqlite3

import requests
import yaml

HDX_API = "https://data.humdata.org/api/action/resource_show"
RESOURCE_ID = "cf0e892b-4565-4986-a2a5-a64d37810aca"

COL_CODE = "ISO 3166-1 Alpha 3-Codes"
COL_NAME = "Preferred Term"
ALIAS_COLS = {
    "English Short",
    "French Short",
    "Spanish Short",
    "English Formal",
    "M49 English",
    "M49 French",
    "M49 Spanish",
}


def _fetch_download_url() -> str:
    r = requests.get(HDX_API, params={"id": RESOURCE_ID}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]["url"]


def _parse(content: bytes, filename: str) -> list[tuple]:
    if filename.lower().endswith((".xlsx", ".xls")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        all_rows = list(ws.iter_rows(values_only=True))
        header = [str(c).strip() if c is not None else "" for c in all_rows[0]]

        def get(row, col):
            try:
                idx = header.index(col)
                v = row[idx]
                return str(v).strip() if v is not None else ""
            except ValueError:
                return ""

        raw = all_rows[1:]
    else:
        reader = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))

        def get(row, col):
            return row.get(col, "").strip()

        raw = reader

    rows = []
    for row in raw:
        code = get(row, COL_CODE)
        name = get(row, COL_NAME)
        if not code or not name:
            continue
        aliases = [get(row, col) for col in ALIAS_COLS]
        aliases = [a for a in aliases if a]
        rows.append((code, name, aliases))
    return rows


def _migrate_if_needed(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(iso_aliases)").fetchall()}
    if "english_short" in cols:
        conn.execute("DROP TABLE iso_aliases")
        conn.execute("""
            CREATE TABLE iso_aliases (
                id       INTEGER PRIMARY KEY,
                iso_code TEXT NOT NULL,
                alias    TEXT NOT NULL,
                UNIQUE (iso_code, alias),
                FOREIGN KEY (iso_code) REFERENCES iso(code)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_iso_aliases ON iso_aliases(alias)")
        print("Migrated iso_aliases from wide to long format.")


def load_iso(db_path: str) -> int:
    url = _fetch_download_url()
    filename = url.split("?")[0].split("/")[-1]

    r = requests.get(url, timeout=60)
    r.raise_for_status()

    rows = _parse(r.content, filename)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _migrate_if_needed(conn)

        conn.executemany(
            "INSERT OR REPLACE INTO iso (code, name_en) VALUES (?, ?)",
            [(code, name) for code, name, _ in rows],
        )
        for code, _, aliases in rows:
            seen = set()
            for alias in aliases:
                if alias not in seen:
                    seen.add(alias)
                    conn.execute(
                        "INSERT OR IGNORE INTO iso_aliases (iso_code, alias) VALUES (?, ?)",
                        (code, alias),
                    )

    return len(rows)


if __name__ == "__main__":
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)
    n = load_iso(config["database"]["path"])
    print(f"Loaded {n} ISO entries")
