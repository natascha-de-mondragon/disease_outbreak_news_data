import sqlite3
import yaml


# Curated aliases not captured by the ICD-11 API (synonyms, abbreviations,
# informal names used in WHO Disease Outbreak News titles).
# Maps (icd_code, alias) — icd_code must exist in the icd table.
ALIASES = [
    # Dengue
    ("1D2Z",   "Dengue"),
    ("1D20",   "Dengue fever"),
    ("1D21",   "Dengue haemorrhagic fever"),
    ("1D21",   "Dengue hemorrhagic fever"),
    ("1D21",   "DHF"),
    # Influenza – seasonal
    ("1E30",   "Influenza"),
    ("1E30",   "Influenza A(H3N2)"),
    # Influenza – zoonotic / pandemic
    ("1E31",   "Swine influenza"),
    ("1E31",   "Avian influenza"),
    ("1E31",   "Influenza A(H5N1)"),
    ("1E31",   "Influenza A(H1N1)"),
    ("1E31",   "Influenza A(H7N9)"),
    ("1E31",   "Influenza A(H9N2)"),
    ("1E31",   "Influenza A(H5N6)"),
    ("1E31",   "Influenza A(H7N2)"),
    ("1E31",   "Influenza A(H7N7)"),
    ("1E31",   "Influenza A H5N6"),
    ("1E31",   "Influenza A H7N9"),
    ("1E31",   "Influenza A H1N1"),
    ("1E31",   "Pandemic (H1N1) 2009"),
    # Polio
    ("1C81",   "Polio"),
    ("1C81",   "Poliovirus"),
    ("1C81",   "Poliomyelitis"),
    ("1C81",   "Wild poliovirus"),
    ("1C81",   "Circulating vaccine-derived poliovirus"),
    # Meningitis
    ("1D01.Z", "Meningitis"),
    ("1C1C.0", "Cerebrospinal meningitis"),
    ("1C1C.0", "Meningococcal meningitis"),
    ("1C8E.Z", "Viral meningitis"),
    # Chikungunya
    ("1D40",   "Chikungunya"),
    # SARS / MERS (novel coronavirus 2012-era MERS → 1D64; 2003 SARS → 1D65)
    ("1D65",   "SARS"),
    ("1D64",   "MERS"),
    ("1D64",   "Novel coronavirus"),
    # West Nile
    ("1D46",   "West Nile"),
    ("1D46",   "West Nile virus"),
    # Malaria
    ("1F4Z",   "Malaria"),
    # Hepatitis E
    ("1E50.4", "Hepatitis E"),
    # Nipah / Henipavirus (1D63 = Henipavirus encephalitis)
    ("1D63",   "Nipah"),
    ("1D63",   "Nipah virus"),
    ("1D63",   "Nipah-like virus"),
    ("1D63",   "Hendra-like virus"),
    # Hantavirus (Seoul virus is a hantavirus)
    ("1D62",   "Hantavirus"),
    ("1D62",   "Seoul virus"),
    # Ebola
    ("1D60.0", "Ebola"),
    ("1D60.0", "Ebola virus"),
    ("1D60.0", "Ebola Reston"),
    # Marburg
    ("1D60.1", "Marburg"),
    ("1D60.1", "Marburg virus"),
    # Tuberculosis
    ("1B1Z",   "Tuberculosis"),
    ("1B1Z",   "XDR-TB"),
    # Tularemia (US spelling; British spelling already loaded from ICD-11 API)
    ("1B94",   "Tularemia"),
    # Listeria
    ("1C1A",   "Listeria"),
    ("1C1A",   "Listeriosis"),
    # Salmonella
    ("1A09",   "Salmonella"),
    # Legionnaires
    ("1C19.1", "Legionnaires"),
    ("1C19.1", "Legionella"),
    # Enterovirus
    ("1D91",   "Enterovirus"),
    # E. coli / EHEC
    ("1A03",   "E. coli"),
    ("1A03",   "E.coli"),
    ("1A03.3", "EHEC"),
]

# Title patterns that indicate a DON is advisory / informational rather than
# an active outbreak report. Evaluated as SQL LIKE expressions against don.title.
NON_OUTBREAK_PATTERNS = [
    # Travel / pilgrimage advisories
    "title LIKE '%traveller%'",
    "title LIKE '%Pilgrimage to Mecca%'",
    "title LIKE '%International Travel and Health%'",
    # Non-disease events
    "title LIKE '%Hurricane%'",
    "title LIKE '%repatriation%'",
    "title LIKE '%Silicone implant%'",
    # Policy / guidance documents
    "title LIKE '%Antimicrobial drugs in Food Animal%'",
    "title LIKE '%Surveillance Standard%'",
    "title LIKE '%Influenza vaccine for%'",
    "title LIKE '%virus sharing%'",
    "title LIKE '%Medical Impact of Use%'",
    # WHO statements / meetings
    "title LIKE '%Statement by WHO%'",
    "title LIKE '%WHO scientific meeting%'",
    "title LIKE '%Director-General%'",
    "title LIKE '%WHO Director%'",
    # Rhetorical / retrospective titles
    "title LIKE '%What happens if%'",
    "title LIKE '%Can % be eradicated%'",
    "title LIKE '%effect of patents%'",
    "title LIKE '%Chronology%'",
    "title LIKE '%one hundred days%'",
    # Advisory subtitles (disease context but advisory framing)
    "title LIKE '%Necessary precaution%'",
    "title LIKE '% - Prevention of further cases%'",
    "title LIKE '%need for virus sharing%'",
    # Food / chemical safety advisories
    "title LIKE '%International food safety event%'",
    "title LIKE '%Melamine-contaminated%'",
    # Antimicrobial resistance situation reports (not outbreaks)
    "title LIKE '%Antimicrobial Resistance%Global situation%'",
    "title LIKE '%Vancomycin resistant%'",
]


def load_icd_alias_extensions(db_path: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        # ── aliases ───────────────────────────────────────────────────────────
        existing = {
            f"{row[0]}|{row[1].lower()}"
            for row in conn.execute("SELECT icd_code, alias FROM icd_aliases")
        }
        to_insert = [
            (code, alias)
            for code, alias in ALIASES
            if f"{code}|{alias.lower()}" not in existing
        ]
        conn.executemany(
            "INSERT INTO icd_aliases (icd_code, alias) VALUES (?, ?)", to_insert
        )

        # ── is_outbreak column ────────────────────────────────────────────────
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(don)")
        }
        if "is_outbreak" not in existing_cols:
            conn.execute(
                "ALTER TABLE don ADD COLUMN is_outbreak INTEGER DEFAULT 1"
            )

        conn.execute("UPDATE don SET is_outbreak = 1")
        where = " OR ".join(f"({p})" for p in NON_OUTBREAK_PATTERNS)
        conn.execute(f"UPDATE don SET is_outbreak = 0 WHERE {where}")

        n_aliases  = len(to_insert)
        n_advisory = conn.execute(
            "SELECT COUNT(*) FROM don WHERE is_outbreak = 0"
        ).fetchone()[0]
        n_total = conn.execute("SELECT COUNT(*) FROM don").fetchone()[0]

    return {
        "aliases_inserted": n_aliases,
        "don_flagged_advisory": n_advisory,
        "don_total": n_total,
    }


if __name__ == "__main__":
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)

    result = load_icd_alias_extensions(db_path=config["database"]["path"])
    print(f"Inserted {result['aliases_inserted']} new ICD aliases")
    print(
        f"Flagged {result['don_flagged_advisory']} / {result['don_total']} "
        "DONs as non-outbreak (is_outbreak = 0)"
    )
