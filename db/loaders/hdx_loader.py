import re
import sqlite3

import openpyxl
import yaml

# Row 1 of the HDX Excel is headers; row 2 is HXL hashtag annotations.
# Data starts at row 3. Column order matches the HDX schema exactly.
_HDX_COLS = (
    "id_outbreak", "year",
    "icd10n", "icd103n", "icd104n",   # ICD-10 names (not stored)
    "icd10c", "icd103c", "icd104c",   # ICD-10 codes
    "disease", "definition",           # disease name/def (definition not stored)
    "country", "iso2", "iso3",         # geography
    "unsd_region", "unsd_subregion", "who_region",
    "dons",
)


def load_hdx(db_path: str, xlsx_path: str) -> int:
    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=3, values_only=True))

    records = []
    for r in rows:
        if not r[0]:
            continue
        id_outbreak = str(r[0])
        year = int(r[1]) if r[1] else None
        icd10c, icd103c, icd104c = r[5], r[6], r[7]
        disease = r[8]
        country = r[10]
        iso3 = r[12]
        who_region = r[15]
        dons = str(r[16]) if r[16] else None
        records.append((id_outbreak, year, icd10c, icd103c, icd104c,
                        disease, country, iso3, who_region, dons))

    with sqlite3.connect(db_path) as conn:
        existing = {r[0] for r in conn.execute("SELECT id_outbreak FROM hdx")}
        to_insert = [r for r in records if r[0] not in existing]
        conn.executemany(
            "INSERT INTO hdx"
            " (id_outbreak, year, icd10c, icd103c, icd104c,"
            "  disease, country, iso3, who_region, dons)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            to_insert,
        )

    return len(to_insert)


def parse_don_numbers(dons_cell: str) -> list[int]:
    """Extract integer DON numbers from a cell like 'DON0001, DON0010'."""
    if not dons_cell:
        return []
    return [int(m) for m in re.findall(r"DON(\d+)", str(dons_cell))]


if __name__ == "__main__":
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)

    n = load_hdx(
        db_path=config["database"]["path"],
        xlsx_path=config["hdx"]["xlsx_path"],
    )
    print(f"Inserted {n} HDX rows")
