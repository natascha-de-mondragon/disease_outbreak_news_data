import argparse
import sqlite3
import yaml


def _config():
    with open("config/config.yaml") as f:
        return yaml.safe_load(f)


def _step_init(config):
    from db.init_db import init_database
    init_database(config["database"]["path"], config["schema"]["path"])
    print("init: schema applied")


def _step_load_iso(config):
    from db.loaders.iso_loader import load_iso
    n = load_iso(config["database"]["path"])
    print(f"load-iso: {n} countries loaded")


def _step_load_icd(config):
    from db.loaders.icd_loader import load_icd
    secrets = config.get("secrets", {}).get("path", "config/secrets.yaml")
    n = load_icd(config["database"]["path"], secrets)
    print(f"load-icd: {n} ICD codes loaded")


def _step_load_regions(config):
    from db.loaders.iso_region_extensions import load_iso_region_extensions
    n = load_iso_region_extensions(config["database"]["path"])
    print(f"load-regions: {n} region aliases inserted")


def _step_load_icd_ext(config):
    from db.loaders.icd_alias_extensions import load_icd_alias_extensions
    n = load_icd_alias_extensions(config["database"]["path"])
    print(f"load-icd-ext: {n} ICD aliases inserted")


def _step_load_hdx(config):
    from db.loaders.hdx_loader import load_hdx
    n = load_hdx(
        db_path=config["database"]["path"],
        xlsx_path=config["hdx"]["xlsx_path"],
    )
    print(f"load-hdx: {n} HDX outbreak rows inserted")


def _step_scrape(config):
    from scraper.index_scraper import step1_index, step2_content
    scraper = config["scraper"]
    with sqlite3.connect(config["database"]["path"]) as conn:
        step1_index(conn, scraper["base_url"], scraper["api_url"], scraper["page_size"])
        step2_content(conn, scraper["api_url"])


def _step_tag(config):
    from extraction.regex_extractor import tag_outbreak_status
    r = tag_outbreak_status(config["database"]["path"])
    print(f"tag: {r['tagged']} DONs — {r['outbreak']} outbreak, {r['advisory']} advisory")


def _step_populate(config):
    from extraction.regex_extractor import populate_outbreaks
    r = populate_outbreaks(config["database"]["path"])
    print(f"populate: {r['dons_processed']} DONs → {r['outbreak_rows']} outbreak rows")


# Ordered pipeline steps: (flag-name, function, help-text)
_STEPS = [
    ("init",         _step_init,         "Apply DB schema (safe to re-run)"),
    ("load-iso",     _step_load_iso,     "Fetch ISO countries from HDX"),
    ("load-icd",     _step_load_icd,     "Fetch ICD codes from WHO API (slow)"),
    ("load-regions", _step_load_regions, "Insert WHO region extensions into iso table"),
    ("load-icd-ext", _step_load_icd_ext, "Insert curated ICD alias extensions"),
    ("load-hdx",     _step_load_hdx,     "Load HDX disease outbreaks reference dataset"),
    ("scrape",       _step_scrape,       "Scrape DON index and content from WHO"),
    ("tag",          _step_tag,          "Tag outbreak vs advisory DONs"),
    ("populate",     _step_populate,     "Populate outbreaks table from tagged DONs"),
]


def main():
    parser = argparse.ArgumentParser(description="DON data pipeline")
    parser.add_argument("--all", action="store_true", help="Run all steps in order")
    for name, _, desc in _STEPS:
        parser.add_argument(f"--{name}", dest=name.replace("-", "_"),
                            action="store_true", help=desc)

    args = parser.parse_args()
    if not any(vars(args).values()):
        parser.print_help()
        return

    config = _config()
    for name, fn, _ in _STEPS:
        if args.all or getattr(args, name.replace("-", "_")):
            print(f"\n=== {name} ===")
            fn(config)


if __name__ == "__main__":
    main()
