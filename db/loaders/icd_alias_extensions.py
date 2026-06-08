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
    # Meningococcaemia (American spelling not covered by ICD API)
    ("1C1C.2", "Meningococcemia"),
    ("1C1C.2", "Meningococcal disease"),
    # Chikungunya
    ("1D40",   "Chikungunya"),
    # SARS / MERS (novel coronavirus 2012-era MERS → 1D64; 2003 SARS → 1D65)
    ("1D65",   "SARS"),
    ("1D64",   "MERS"),
    ("1D64",   "Novel coronavirus"),
    # 2002-2003 outbreak titles used "acute respiratory syndrome" before SARS was named
    ("1D65",   "Acute respiratory syndrome"),
    ("1D65",   "Pneumonia of unknown cause"),
    # Viral haemorrhagic fever — unspecified (1D86 aliases from API all have literal
    # brackets; add plain-text variants for common DON title phrasings)
    ("1D86",   "haemorrhagic fever syndrome"),
    ("1D86",   "acute haemorrhagic fever syndrome"),
    ("1D86",   "viral haemorrhagic fever syndrome"),
    ("1D86",   "haemorrhagic fever of unknown aetiology"),
    ("1D86",   "haemorrhagic fever of unknown origin"),
    ("1D86",   "suspected haemorrhagic fever"),
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

# Synthetic ICD entries for diseases that appear in DON titles but are not
# covered by the loaded ICD-11 chapters (chapters 0–1 only).
# Each entry: (synthetic_code, name_en, [alias_strings])
SYNTHETIC_ICD = [
    (
        "X.COVID19",
        "COVID-19",
        [
            "COVID",
            "coronavirus disease 2019",
            "SARS-CoV-2",
        ],
    ),
    (
        "X.GBS",
        "Guillain-Barré syndrome",
        [
            "Guillain-Barre syndrome",
            "Guillain-Barré",
            "Guillain-Barre",
            "GBS",
        ],
    ),
    (
        "X.PRION",
        "Prion disease",
        [
            "Creutzfeldt-Jakob disease",
            "CJD",
            "variant CJD",
            "vCJD",
            "new variant CJD",
            "bovine spongiform encephalopathy",
            "BSE",
        ],
    ),
    (
        "X.HUS",
        "Haemolytic uraemic syndrome",
        [
            "haemolytic uraemic syndrome",
            "hemolytic uremic syndrome",
            "HUS",
        ],
    ),
    (
        "X.MICRO",
        "Microcephaly",
        [
            "microcephaly",
            "congenital microcephaly",
        ],
    ),
]


def load_icd_alias_extensions(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        # Insert synthetic ICD codes (not in the standard ICD-11 chapter load)
        conn.executemany(
            "INSERT OR IGNORE INTO icd (code, name_en) VALUES (?, ?)",
            [(code, name) for code, name, _ in SYNTHETIC_ICD],
        )

        existing = {
            f"{row[0]}|{row[1].lower()}"
            for row in conn.execute("SELECT icd_code, alias FROM icd_aliases")
        }

        to_insert = [
            (code, alias)
            for code, alias in ALIASES
            if f"{code}|{alias.lower()}" not in existing
        ]
        for code, _, aliases in SYNTHETIC_ICD:
            for alias in aliases:
                if f"{code}|{alias.lower()}" not in existing:
                    to_insert.append((code, alias))

        conn.executemany(
            "INSERT INTO icd_aliases (icd_code, alias) VALUES (?, ?)", to_insert
        )

    return len(to_insert)


if __name__ == "__main__":
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)

    n = load_icd_alias_extensions(db_path=config["database"]["path"])
    print(f"Inserted {n} new ICD aliases")
