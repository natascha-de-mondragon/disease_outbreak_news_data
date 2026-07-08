"""
Compare the `outbreaks` table against the HDX gold-standard dataset.

This is a binary exact-tuple evaluation. A prediction is correct only if its
(year, iso3, icd) tuple exactly matches an HDX tuple. There is no partial
credit and no bipartite consumption, so nothing here can manufacture a phantom
partial match or pair two unrelated errors into one. It is three set relations:

  TP  = predicted tuples that are in HDX
  FN  = HDX rows with no matching predicted tuple
  FP  = predicted tuples that are in no HDX row

Recall is counted over HDX ROWS (a row is found if any acceptable code for its
disease was predicted). Precision is counted over predicted TUPLES.

The false positives are then diagnosed, NOT scored, by non-exclusive
neighbour lookups (a single FP may sit next to several FNs, which is exactly
why this cannot fabricate matches):

  disease-confusion : the (year, iso) exists in HDX but this disease does not
                      -> the disease coder is the suspect
  geography-rollup  : this disease/code exists in HDX that year in a DIFFERENT
                      country -> the country roll-up is the suspect
  isolated          : neither -> candidate discovery, or a deflected duplicate

Isolated FPs are triaged: an FP that shares a source DON, country, and ICD
family with a true positive is almost certainly a second, deflected extraction
of an event we already got right, so it is flagged rather than shown as a
discovery. What survives is the genuine discovery queue: tuples the model
found that HDX never coded and that no correct extraction explains.

CODE-SPACE NOTE:
  HDX disease names are translated into your icd lookup's code space with the
  same `_find_codes` the extractor uses, so both sides share a coding. HDX
  diseases that map to no code are reported as `unmappable` and excluded from
  the mappable-recall denominator, because that is a gap in your alias table,
  not a model error. If your icd table already uses HDX's own icd104c coding,
  you can drop the translation and compare hdx.icd104c directly, which removes
  the unmappable losses entirely.

Output files (evaluation/):
  fn.csv               every HDX row not found (mappable misses + unmappable)
  fp.csv               every in-window false positive, fully annotated
  discovery_queue.csv  isolated, non-deflected FPs: the queue a human reads
  fp_out_of_window.csv  FPs in years HDX does not cover (not scored)
  summary.txt
"""

import csv
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import yaml

from extraction.regex_extractor import _build_icd_lookup, _find_codes

_OUT_DIR = os.path.dirname(__file__)

# WHO synthetic region codes are not ISO alpha-3 and never appear in HDX.
_REGION_PREFIX = "WHO."


def _write_csv(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _norm_icd(code: str) -> str:
    return code.replace(".", "").upper()


def _family(code: str) -> str:
    """3-character ICD family, e.g. A984 -> A98 (Marburg and Ebola share A98)."""
    return code[:3]


_ALLOWED_TABLES = {
    "outbreaks",
    "outbreaks_regex",
    "outbreaks_ner",
    "outbreaks_llama",
    "outbreaks_claude",
}


def compare(db_path: str, table: str = "outbreaks") -> dict:
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"table {table!r} not in allowed set {sorted(_ALLOWED_TABLES)}")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # ---- predictions: raw rows, keep provenance for discovery triage -----
        pred_tuples: set[tuple[int, str, str]] = set()
        tuple_to_dons: dict[tuple[int, str, str], set[str]] = {}
        n_pred_rows = 0
        for r in conn.execute(f"SELECT year, iso_code, icd_code, don FROM {table}"):
            n_pred_rows += 1
            iso, icd = r["iso_code"], r["icd_code"]
            if not iso or not icd or iso.startswith(_REGION_PREFIX):
                continue
            t = (int(r["year"]), iso.upper(), _norm_icd(icd))
            pred_tuples.add(t)
            tuple_to_dons.setdefault(t, set()).add(r["don"])

        # ---- HDX gold: drop the coronavirus-dashboard rows (no DON article) --
        hdx_raw = [
            dict(r)
            for r in conn.execute("SELECT year, disease, iso3, dons FROM hdx")
            if r["dons"] is None or "dashboard" not in str(r["dons"]).lower()
        ]

        icd_lookup = _build_icd_lookup(conn)
        icd_names = dict(conn.execute("SELECT code, name_en FROM icd"))

    if not hdx_raw:
        print("ERROR: hdx table is empty or all rows were dashboard rows.")
        return {}
    if n_pred_rows == 0:
        print("WARNING: outbreaks table is empty. Run the extractor first.")

    # ---- dedupe HDX to (year, iso3, disease) rows and translate to codes -----
    disease_to_icds: dict[str, set[str]] = {}
    gold_rows: set[tuple[int, str, str]] = set()
    for row in hdx_raw:
        if row["year"] and row["iso3"] and row["disease"]:
            gold_rows.add((int(row["year"]), row["iso3"].upper(), row["disease"]))

    for _, _, disease in gold_rows:
        if disease not in disease_to_icds:
            disease_to_icds[disease] = {_norm_icd(c) for c in _find_codes(disease, icd_lookup)}

    if not gold_rows:
        print("ERROR: no usable HDX rows (missing year/iso3/disease).")
        return {}

    hdx_year_min = min(y for y, _, _ in gold_rows)
    hdx_year_max = max(y for y, _, _ in gold_rows)

    # ---- acceptable keys per gold row, and the union across all rows ---------
    gold_row_keys: dict[tuple[int, str, str], frozenset] = {}
    gold_keys: set[tuple[int, str, str]] = set()
    for (year, iso3, disease) in gold_rows:
        keys = frozenset((year, iso3, code) for code in disease_to_icds[disease])
        gold_row_keys[(year, iso3, disease)] = keys
        gold_keys |= keys

    gold_keys_family = {(y, iso, _family(c)) for (y, iso, c) in gold_keys}
    pred_family = {(y, iso, _family(c)) for (y, iso, c) in pred_tuples}
    gold_year_iso = {(y, iso) for (y, iso, _) in gold_rows}          # HDX country-years
    gold_year_code = {(y, c) for (y, _, c) in gold_keys}             # HDX year+code (any country)

    # ---- RECALL: per gold row (found if any acceptable key was predicted) ----
    fn_rows: list[dict] = []
    tp_row_cnt = 0
    tp_row_family_cnt = 0
    unmappable_cnt = 0

    for (year, iso3, disease) in sorted(gold_rows):
        keys = gold_row_keys[(year, iso3, disease)]
        mappable = bool(keys)
        found = bool(keys & pred_tuples)
        found_family = any((y, iso, _family(c)) in pred_family for (y, iso, c) in keys)

        if not mappable:
            unmappable_cnt += 1
        if found:
            tp_row_cnt += 1
        if found_family:
            tp_row_family_cnt += 1

        if not found:
            fn_rows.append({
                "year": year,
                "iso3": iso3,
                "disease": disease,
                "mappable": int(mappable),
                "expected_icd": "|".join(sorted(disease_to_icds[disease])) or "unmappable",
                "note": ("not found" if mappable else "HDX disease maps to no code (alias-table gap)"),
            })

    # ---- PRECISION: per predicted tuple -------------------------------------
    tp_pred = pred_tuples & gold_keys
    fp_pred = pred_tuples - gold_keys

    # Build don -> the TP tuples that came from it, for the deflection check.
    don_to_tp: dict[str, list[tuple[int, str, str]]] = {}
    for t in tp_pred:
        for d in tuple_to_dons.get(t, ()):
            don_to_tp.setdefault(d, []).append(t)

    fp_in_window: list[dict] = []
    fp_out_window: list[dict] = []
    discovery: list[dict] = []

    for (year, iso, icd) in sorted(fp_pred):
        in_window = hdx_year_min <= year <= hdx_year_max
        right_country_year = (year, iso) in gold_year_iso
        right_disease_elsewhere = (year, icd) in gold_year_code  # same year+code, different country

        if right_country_year:
            label = "disease_confusion"
        elif right_disease_elsewhere:
            label = "geography_rollup"
        else:
            label = "isolated"

        # Deflection check: does a TP from the SAME DON share country + ICD
        # family? That is the same real event extracted twice, once deflected.
        deflected = False
        for d in tuple_to_dons.get((year, iso, icd), ()):
            for (ty, tiso, ticd) in don_to_tp.get(d, ()):
                if tiso == iso and _family(ticd) == _family(icd):
                    deflected = True
                    break
            if deflected:
                break

        rec = {
            "year": year,
            "iso_code": iso,
            "icd_code": icd,
            "icd_name": icd_names.get(icd, "?"),
            "right_country_year": int(right_country_year),
            "right_disease_elsewhere": int(right_disease_elsewhere),
            "label": label,
            "deflected_duplicate": int(deflected),
            "source_dons": "|".join(sorted(tuple_to_dons.get((year, iso, icd), ()))),
        }

        if not in_window:
            fp_out_window.append(rec)
        else:
            fp_in_window.append(rec)
            if label == "isolated" and not deflected:
                discovery.append(rec)

    # ---- write CSVs ----------------------------------------------------------
    _write_csv(os.path.join(_OUT_DIR, "fn.csv"),
               ["year", "iso3", "disease", "mappable", "expected_icd", "note"], fn_rows)
    fp_fields = ["year", "iso_code", "icd_code", "icd_name", "right_country_year",
                 "right_disease_elsewhere", "label", "deflected_duplicate", "source_dons"]
    _write_csv(os.path.join(_OUT_DIR, "fp.csv"), fp_fields, fp_in_window)
    _write_csv(os.path.join(_OUT_DIR, "discovery_queue.csv"), fp_fields, discovery)
    _write_csv(os.path.join(_OUT_DIR, "fp_out_of_window.csv"), fp_fields, fp_out_window)

    # ---- summary -------------------------------------------------------------
    total_rows = len(gold_rows)
    mappable_rows = total_rows - unmappable_cnt
    n_pred = len(pred_tuples)
    tp_in_window = sum(1 for t in tp_pred if hdx_year_min <= t[0] <= hdx_year_max)
    fp_in_window_cnt = len(fp_in_window)

    def _pct(n: int, d: int) -> str:
        return f"{100 * n / d:.1f}%" if d else "n/a"

    n_disease_conf = sum(1 for r in fp_in_window if r["label"] == "disease_confusion")
    n_geo = sum(1 for r in fp_in_window if r["label"] == "geography_rollup")
    n_isolated = sum(1 for r in fp_in_window if r["label"] == "isolated")
    n_deflected = sum(1 for r in fp_in_window if r["label"] == "isolated" and r["deflected_duplicate"])

    lines = [
        "=== HDX exact-match comparison ===",
        "",
        f"HDX rows (year,iso,disease), non-COVID:  {total_rows:>6}   years {hdx_year_min}-{hdx_year_max}",
        f"  unmappable (no code; excluded from mappable recall): {unmappable_cnt}",
        f"Predicted unique tuples:                 {n_pred:>6}   (from {n_pred_rows} raw rows)",
        "",
        "Recall (HDX rows found):",
        f"  exact  end-to-end : {tp_row_cnt}/{total_rows}  = {_pct(tp_row_cnt, total_rows)}",
        f"  exact  mappable   : {tp_row_cnt}/{mappable_rows}  = {_pct(tp_row_cnt, mappable_rows)}",
        f"  family (3-char)   : {tp_row_family_cnt}/{total_rows}  = {_pct(tp_row_family_cnt, total_rows)}"
        "   [gap vs exact = leaf-level ICD errors, e.g. Marburg vs Ebola]",
        "",
        "Precision (predicted tuples, in HDX year range):",
        f"  {tp_in_window}/{tp_in_window + fp_in_window_cnt}  = {_pct(tp_in_window, tp_in_window + fp_in_window_cnt)}",
        "",
        "False-positive diagnosis (in-window, non-exclusive labels):",
        f"  disease_confusion : {n_disease_conf:>5}  right country-year, disease not in HDX there",
        f"  geography_rollup  : {n_geo:>5}  right disease that year, wrong country",
        f"  isolated          : {n_isolated:>5}  neither  ({n_deflected} flagged deflected duplicates)",
        "",
        f"DISCOVERY QUEUE (isolated, not deflected): {len(discovery)}  -> discovery_queue.csv",
        f"Out-of-window FPs (not scored): {len(fp_out_window)}  -> fp_out_of_window.csv",
        "",
        "Files: evaluation/fn.csv, fp.csv, discovery_queue.csv, fp_out_of_window.csv",
    ]
    summary = "\n".join(lines)
    print(summary)
    with open(os.path.join(_OUT_DIR, "summary.txt"), "w") as f:
        f.write(summary + "\n")

    return {
        "hdx_rows": total_rows,
        "unmappable": unmappable_cnt,
        "pred_tuples": n_pred,
        "tp_rows": tp_row_cnt,
        "tp_rows_family": tp_row_family_cnt,
        "recall_exact": _pct(tp_row_cnt, total_rows),
        "recall_mappable": _pct(tp_row_cnt, mappable_rows),
        "precision": _pct(tp_in_window, tp_in_window + fp_in_window_cnt),
        "fp_disease_confusion": n_disease_conf,
        "fp_geography_rollup": n_geo,
        "fp_isolated": n_isolated,
        "discovery_queue": len(discovery),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compare outbreaks table against HDX gold standard")
    parser.add_argument("--db", default=None, help="Path to don_registry.db")
    parser.add_argument("--table", default="outbreaks", help="Outbreaks table to evaluate")
    args = parser.parse_args()

    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)
    db_path = args.db or config["database"]["path"]

    compare(db_path=db_path, table=args.table)
