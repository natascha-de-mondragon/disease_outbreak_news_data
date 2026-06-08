import sqlite3
import yaml


# WHO regions and their string variants as they appear in DON titles.
# Each entry: (synthetic_iso_code, canonical_name, [alias_strings])
# The canonical name is stored in iso.name_en and matched by the lookup builder,
# so aliases here are only variant strings that differ from the canonical name.
REGIONS = [
    (
        "WHO.AFR",
        "African Region",
        [
            "Africa",
            "African Meningitis Belt",
            "West and Central Africa",
            "west and central Africa",
            "West and Central Africa region",
            "Central Africa",
            "Horn of Africa",
            "East, West, and Central Africa",
            "East Africa",
            "Great Lakes Region",
            "Rift Valley",
        ],
    ),
    (
        "WHO.EMR",
        "Eastern Mediterranean Region",
        [
            "Middle East",
        ],
    ),
    (
        "WHO.EUR",
        "European Region",
        [
            "Europe",
        ],
    ),
    (
        "WHO.AMR",
        "Region of the Americas",
        [
            "the Americas",
            "Americas",
        ],
    ),
    (
        "WHO.SEA",
        "South-East Asia Region",
        [],
    ),
    (
        "WHO.WPR",
        "Western Pacific Region",
        [
            "Pacific Island Countries and Areas",
            "Pacific Islands",
        ],
    ),
    (
        "WHO.GLO",
        "Global",
        [
            "Multi-country",
            "Multi-locations",
            "Multiple Regions",
            "Northern Hemisphere",
        ],
    ),
]

# Aliases for standard ISO countries whose official name_en is parenthetical
# or otherwise differs from how they appear in DON titles.
# Each entry: (iso_code, [alias_strings]) — the code must exist in iso.
COUNTRY_ALIASES = [
    ("VEN", ["Venezuela", "Bolivarian Republic of Venezuela"]),
    ("NLD", ["Netherlands"]),
    ("HKG", ["Hong Kong"]),
    ("PHL", ["Philipines"]),          # typo as it appears in DON titles
    ("CIV", ["Côte d'Ivoir"]),        # truncated misspelling in DON titles
    ("CAN", ["Toronto"]),
]


def load_iso_region_extensions(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        conn.executemany(
            "INSERT OR IGNORE INTO iso (code, name_en) VALUES (?, ?)",
            [(code, name) for code, name, _ in REGIONS],
        )

        inserted = 0
        for code, _, aliases in REGIONS:
            for alias in aliases:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO iso_aliases (iso_code, alias) VALUES (?, ?)",
                    (code, alias),
                )
                inserted += cur.rowcount

        for code, aliases in COUNTRY_ALIASES:
            # SQLite INSERT OR IGNORE does not suppress FK violations, only PK/UNIQUE.
            # Skip silently if the country hasn't been loaded yet.
            if not conn.execute("SELECT 1 FROM iso WHERE code = ?", (code,)).fetchone():
                continue
            for alias in aliases:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO iso_aliases (iso_code, alias) VALUES (?, ?)",
                    (code, alias),
                )
                inserted += cur.rowcount

    return inserted


if __name__ == "__main__":
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)

    n = load_iso_region_extensions(db_path=config["database"]["path"])
    print(f"Inserted {n} new ISO region entries")
