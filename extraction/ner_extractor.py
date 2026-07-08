"""
NER outbreak extractor: baseline for the model comparison.

Two roles in one file:
  1. Spot-test (default): print extracted entities for a few DONs, for eyeballing.
  2. Populate (--populate): write a per-extractor outbreaks table via the shared
     `base` core, so its (year, iso, icd) tuples are scored the same way as the
     LLM extractors.

Two models in combination:
  - d4data/biomedical-ner-all (transformers) -> biomedical entities incl. Disease.
  - en_core_web_lg / en_core_web_sm (spaCy)  -> GPE/LOC (countries).
  Year is not taken from NER; base.parse_year reads it from the DON identifier,
  the same rule every extractor uses.

BASELINE CAVEAT: NER returns a flat disease list and a flat country list with no
link between them. This writer pairs every disease with every country (a cross
product), which is the phantom-pairing the grouped LLM schema was built to
avoid. So NER is a floor, not a candidate: read its recall as signal and treat
its precision as a known artifact of the flat pairing, not a fair loss.

BERT models cap at 512 tokens; longer DONs are split into overlapping windows
(stride=128) so entities near chunk boundaries are not lost.

Setup (run once):
  pip install transformers torch --prefer-binary
  pip install spacy --prefer-binary
  python -m spacy download en_core_web_lg

Usage:
  python -m extraction.ner_extractor                                  # spot-test, 5 random DONs
  python -m extraction.ner_extractor --n 10                           # spot-test, 10 random DONs
  python -m extraction.ner_extractor --id 2020-DON123                 # spot-test, one DON
  python -m extraction.ner_extractor --populate --table outbreaks_ner --limit 200 --seed 42
"""

import argparse
import os
import re
import sqlite3
import sys
import textwrap

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from extraction import base

_BIO_MODEL = "d4data/biomedical-ner-all"

# Entity groups from d4data/biomedical-ner-all that we treat as diseases.
# The model uses "Disease_disorder" not "Disease" — verified against live output.
_DISEASE_GROUPS = {"Disease_disorder"}

# Labels to suppress from the "other biomedical" section (not useful for DONs).
_SKIP_GROUPS = {"Biological_structure", "Lab_value", "Nonbiological_location", "Outcome"}


# -- model loading -----------------------------------------------------------

def _load_bio_pipeline():
    try:
        from transformers import pipeline
    except ImportError:
        sys.exit(
            "transformers not found. Install with:\n"
            "  pip install transformers torch --prefer-binary"
        )

    print(f"[bio model] loading {_BIO_MODEL} (first run downloads ~400 MB)...")
    bio = pipeline(
        "ner",
        model=_BIO_MODEL,
        aggregation_strategy="simple",   # merges B-/I- tokens into full spans
        stride=128,                      # overlap between 512-token windows
    )
    print("[bio model] ready")
    return bio


def _load_spacy():
    try:
        import spacy
    except ImportError:
        print("[spacy] not installed, country extraction disabled")
        return None

    for model_name in ("en_core_web_lg", "en_core_web_sm"):
        try:
            nlp = spacy.load(model_name)
            print(f"[spacy] {model_name} loaded (GPE/LOC)")
            return nlp
        except OSError:
            continue

    print(
        "[spacy] no web model found, country extraction disabled.\n"
        "  Install with: python -m spacy download en_core_web_lg"
    )
    return None


# -- year extraction via regex (spot-test display only) ----------------------

_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _extract_years(text: str) -> list[str]:
    return sorted(set(_YEAR_RE.findall(text)))


# -- entity extraction -------------------------------------------------------

def _extract(text: str, bio_pipeline, spacy_nlp) -> dict:
    raw = bio_pipeline(text)

    diseases: list[str] = []
    other_bio: dict[str, list[str]] = {}

    for ent in raw:
        group = ent.get("entity_group", "")
        word = ent["word"].strip()
        if not word:
            continue
        if group in _DISEASE_GROUPS:
            if word not in diseases:
                diseases.append(word)
        elif group not in _SKIP_GROUPS:
            other_bio.setdefault(group, [])
            if word not in other_bio[group]:
                other_bio[group].append(word)

    diseases.sort()

    if spacy_nlp is not None:
        doc = spacy_nlp(text)
        countries = sorted({e.text for e in doc.ents if e.label_ in ("GPE", "LOC")})
    else:
        countries = []

    return {
        "years": _extract_years(text),
        "diseases": diseases,
        "countries": countries,
        "other_bio": other_bio,
    }


# -- populate path (writes a per-extractor outbreaks table via base) ----------

def _ner_extract_fn(bio_pipeline, spacy_nlp):
    """Adapt NER output to base.OutbreakExtraction.

    NER gives a flat disease list and a flat country list with no binding
    between them, so every disease is paired with every country. That cross
    product is the phantom-pairing the grouped LLM schema avoids, and it is why
    this is a baseline floor rather than a candidate.
    """

    def extract_fn(don_text: str) -> base.OutbreakExtraction:
        ents = _extract(don_text, bio_pipeline, spacy_nlp)
        return base.OutbreakExtraction(
            outbreaks=[
                base.Outbreak(disease_name=d, country_names=ents["countries"])
                for d in ents["diseases"]
            ]
        )

    return extract_fn


def populate(db_path: str, table: str = "outbreaks_ner", limit=None, seed: int = 42) -> dict:
    """Load the models once, then drive extraction through the shared core."""
    bio = _load_bio_pipeline()
    nlp = _load_spacy()
    return base.run(db_path, table, _ner_extract_fn(bio, nlp), limit=limit, seed=seed)


# -- database helpers (spot-test) --------------------------------------------

def _fetch_random(db_path: str, n: int) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT identifier, publication, title, content "
            "FROM don ORDER BY RANDOM() LIMIT ?",
            (n,),
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_by_id(db_path: str, identifier: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT identifier, publication, title, content "
            "FROM don WHERE identifier = ?",
            (identifier,),
        ).fetchall()
    return [dict(r) for r in rows]


# -- display (spot-test) -----------------------------------------------------

_WIDTH = 72


def _print_don(don: dict, entities: dict) -> None:
    sep = "-" * _WIDTH
    text = ((don["content"] or "") or don["title"]).strip()

    print(f"\n{sep}")
    print(f"DON:   {don['identifier']}   ({don['publication']})")
    print(f"Title: {don['title']}")
    print(sep)

    preview = textwrap.fill(text[:400] + ("..." if len(text) > 400 else ""), width=_WIDTH)
    print(preview)
    print()

    def _fmt(label: str, items: list[str]) -> None:
        if items:
            body = textwrap.fill(", ".join(items), width=_WIDTH - len(label) - 2)
            print(f"{label}: {body}")
        else:
            print(f"{label}: (none found)")

    _fmt("Years    ", entities["years"])
    _fmt("Diseases ", entities["diseases"])
    _fmt("Countries", entities["countries"])

    if entities["other_bio"]:
        print("Other biomedical entities:")
        for group, items in sorted(entities["other_bio"].items()):
            _fmt(f"  {group:<18}", items)


def _spot_test(db_path: str, n: int, identifier: str | None) -> None:
    bio_pipeline = _load_bio_pipeline()
    spacy_nlp = _load_spacy()

    dons = _fetch_by_id(db_path, identifier) if identifier else _fetch_random(db_path, n)
    if not dons:
        print("No DONs found, check the database path or identifier.")
        return

    print(f"\n{'=' * _WIDTH}")
    print(f"  NER spot-test  ·  {len(dons)} DON(s)  ·  bio={_BIO_MODEL}")
    print(f"{'=' * _WIDTH}")

    for don in dons:
        text = f"{don['title']} {don['content'] or ''}".strip()
        entities = _extract(text, bio_pipeline, spacy_nlp)
        _print_don(don, entities)

    print(f"\n{'-' * _WIDTH}\n")


# -- main --------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="NER outbreak extractor (baseline)")
    parser.add_argument("--db", type=str, default=None, help="Path to don_registry.db")
    # Populate mode
    parser.add_argument("--populate", action="store_true", help="Write the outbreaks table")
    parser.add_argument("--table", default="outbreaks_ner", help="Target table (populate mode)")
    parser.add_argument("--limit", type=int, default=None, help="Sample size (populate mode)")
    parser.add_argument("--seed", type=int, default=42, help="Sampling seed (populate mode)")
    # Spot-test mode
    parser.add_argument("--n", type=int, default=5, help="Random DONs to show (spot-test)")
    parser.add_argument("--id", type=str, default=None, help="Specific DON identifier (spot-test)")
    args = parser.parse_args()

    db_path = base.load_db_path(args.db)

    if args.populate:
        result = populate(db_path, table=args.table, limit=args.limit, seed=args.seed)
        base.print_stats(result)
    else:
        _spot_test(db_path, args.n, args.id)


if __name__ == "__main__":
    main()
