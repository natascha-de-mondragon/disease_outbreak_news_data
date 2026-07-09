PRAGMA foreign_keys = ON;

-- Disease outbreak news published
CREATE TABLE IF NOT EXISTS don (
    recorded    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    identifier  TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    publication DATE NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT,
    scrape      INTEGER DEFAULT 0,
    is_outbreak INTEGER DEFAULT 1
);

-- ISO main table (one row per country)
CREATE TABLE IF NOT EXISTS iso (
    code        TEXT PRIMARY KEY,
    name_en     TEXT NOT NULL
);

-- ISO alias table (many rows per country, one alias per row)
CREATE TABLE IF NOT EXISTS iso_aliases (
    id       INTEGER PRIMARY KEY,
    iso_code TEXT NOT NULL,
    alias    TEXT NOT NULL,
    UNIQUE (iso_code, alias),
    FOREIGN KEY (iso_code) REFERENCES iso(code)
);

-- ICD main table (one row per disease)
CREATE TABLE IF NOT EXISTS icd (
    code        TEXT PRIMARY KEY,
    name_en     TEXT NOT NULL
);

-- ICD alias table (many rows per disease)
CREATE TABLE IF NOT EXISTS icd_aliases (
    id          INTEGER PRIMARY KEY,
    icd_code    TEXT NOT NULL,
    alias       TEXT NOT NULL,
    FOREIGN KEY (icd_code) REFERENCES icd(code)
);

-- Outbreaks table for review
-- id_outbreak is NOT unique: the same (year, iso3, icd) tuple legitimately
-- appears in multiple DONs (original report + updates). Use DISTINCT on
-- id_outbreak when building the prediction set for HDX comparison.
CREATE TABLE IF NOT EXISTS outbreaks (
    recorded         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    id_outbreak      TEXT,     -- HDX key year+iso3+icd4, NULL if unresolved, non-unique
    year             INTEGER,
    iso_code         TEXT,     -- iso3, NULL if country mention unresolved
    icd_code         TEXT,     -- dotless ICD, NULL if disease mention unresolved
    disease_mention  TEXT,     -- raw string the model returned
    country_mention  TEXT,     -- raw string the model returned
    don              TEXT,     -- source DON identifier (provenance)
    FOREIGN KEY (don) REFERENCES don(identifier),
    FOREIGN KEY (iso_code) REFERENCES iso(code),
    FOREIGN KEY (icd_code) REFERENCES icd(code)
);

CREATE INDEX IF NOT EXISTS idx_icd_aliases ON icd_aliases(alias);

-- HDX disease outbreaks reference dataset (gold standard for evaluation)
CREATE TABLE IF NOT EXISTS hdx (
    id_outbreak TEXT PRIMARY KEY,  -- composite key from HDX: year+iso3+icd104c
    year        INTEGER NOT NULL,
    icd10c      TEXT,              -- ICD-10 chapter range (e.g. A00-A09)
    icd103c     TEXT,              -- ICD-10 3-character code (e.g. A00)
    icd104c     TEXT,              -- ICD-10 4-character code (e.g. A000)
    disease     TEXT,              -- disease name (ICD-10 label)
    country     TEXT,              -- country name
    iso3        TEXT,              -- ISO 3166-1 alpha-3 code
    who_region  TEXT,
    dons        TEXT               -- raw DONs field (comma-separated DON references)
);
CREATE INDEX IF NOT EXISTS idx_hdx_iso3 ON hdx(iso3);
CREATE INDEX IF NOT EXISTS idx_hdx_year ON hdx(year);