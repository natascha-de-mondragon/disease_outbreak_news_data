import sqlite3
import yaml
from pathlib import Path


def init_database(db_path, schema_path):
    schema = Path(schema_path).read_text()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)


if __name__ == "__main__":
    with open("config/config.yaml", "r") as f:
        config = yaml.safe_load(f)

    init_database(
        db_path=config["database"]["path"],
        schema_path=config["schema"]["path"],
    )
