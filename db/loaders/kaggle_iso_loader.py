import os
import sqlite3

import pandas as pd
import yaml

KAGGLE_DATASET = "wbdill/country-aliaseslist-of-alternative-country-names"
CSV_FILENAME = "country aliases (wiki-List of alternative country names).csv"

_EXCLUDED_DESCS = {"official"}  # exact lower-stripped matches to skip


def _is_official_english(desc: str) -> bool:
    if not isinstance(desc, str):
        return False
    d = desc.strip().lower()
    return d in _EXCLUDED_DESCS or ("official" in d and "english" in d)


def _download_csv() -> str:
    import kagglehub
    path = kagglehub.dataset_download(KAGGLE_DATASET)
    return os.path.join(path, CSV_FILENAME)


def load_kaggle_aliases(db_path: str, csv_path: str | None = None) -> int:
    if csv_path is None:
        csv_path = _download_csv()

    df = pd.read_csv(csv_path)
    df = df[df["iso3"].notna()]
    df = df[~df["AliasDescription"].apply(_is_official_english)]

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        existing_codes = {r[0] for r in conn.execute("SELECT code FROM iso").fetchall()}
        df = df[df["iso3"].isin(existing_codes)]

        rows = [(row["iso3"], row["Alias"]) for _, row in df.iterrows()]
        conn.executemany(
            "INSERT OR IGNORE INTO iso_aliases (iso_code, alias) VALUES (?, ?)",
            rows,
        )
        conn.commit()

    inserted = len(rows)
    print(f"Loaded {inserted} Kaggle alias rows for {df['iso3'].nunique()} countries.")
    return inserted


if __name__ == "__main__":
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)
    load_kaggle_aliases(db_path=config["database"]["path"])
