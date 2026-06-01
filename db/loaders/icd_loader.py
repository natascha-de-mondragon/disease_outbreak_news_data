import sqlite3
import time
from collections import deque

import requests
import yaml

TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"
API_BASE = "https://id.who.int/icd/release/11"
LINEARIZATION = "mms"
RELEASE = "2024-01"
REQUEST_DELAY = 0.05  # seconds between API calls

# ICD-11 Chapter 01 = "Certain infectious or parasitic diseases" (~400-500 codes).
# Set to None to load the full ICD-11 tree (~17 000 codes).
DEFAULT_ROOT_CODE = "01"


def _read_secrets(secrets_path: str) -> tuple[str, str]:
    with open(secrets_path) as f:
        secrets = yaml.safe_load(f)
    return secrets["ClientId"], secrets["ClientSecret"]


def _get_token(client_id: str, client_secret: str) -> str:
    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "icdapi_access",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Language": "en",
        "API-Version": "v2",
    }


def _fetch(url: str, token: str) -> dict:
    r = requests.get(url, headers=_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()


def _str_value(obj) -> str:
    if isinstance(obj, dict):
        return obj.get("@value", "")
    return str(obj) if obj else ""


def _resolve_root(token: str, root_code: str | None) -> str:
    """Return the entity URI to start traversal from.

    If root_code is given, resolve it via the codeinfo endpoint so we get
    the canonical entity URI for that chapter/block. Otherwise start from
    the linearization root.
    """
    if root_code is None:
        return f"{API_BASE}/{RELEASE}/{LINEARIZATION}"
    codeinfo_url = f"{API_BASE}/{RELEASE}/{LINEARIZATION}/codeinfo/{root_code}"
    r = requests.get(codeinfo_url, headers=_headers(token), timeout=30)
    r.raise_for_status()
    return r.json()["stemId"]


def load_icd(
    db_path: str,
    secrets_path: str = "config/secrets.yaml",
    root_code: str | None = DEFAULT_ROOT_CODE,
) -> int:
    client_id, client_secret = _read_secrets(secrets_path)
    token = _get_token(client_id, client_secret)

    start_url = _resolve_root(token, root_code)
    queue = deque([start_url])
    count = 0

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        while queue:
            url = queue.popleft()
            entity = _fetch(url, token)

            code = entity.get("code", "").strip()
            name = _str_value(entity.get("title", ""))

            if code and name:
                conn.execute(
                    "INSERT OR REPLACE INTO icd (code, name_en) VALUES (?, ?)",
                    (code, name),
                )
                seen = set()
                for field in ("synonym", "inclusion", "indexTerm"):
                    for item in entity.get(field, []):
                        alias = _str_value(item.get("label", ""))
                        if alias and alias not in seen:
                            seen.add(alias)
                            conn.execute(
                                "INSERT INTO icd_aliases (icd_code, alias) VALUES (?, ?)",
                                (code, alias),
                            )
                count += 1

            queue.extend(entity.get("child", []))
            time.sleep(REQUEST_DELAY)

    return count


if __name__ == "__main__":
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)
    n = load_icd(
        db_path=config["database"]["path"],
        secrets_path="config/secrets.yaml",
    )
    print(f"Loaded {n} ICD entries")
