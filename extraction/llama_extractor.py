"""
Llama-based outbreak entity extractor using a local Ollama instance.

Thin wrapper over extraction.base. The only thing specific to this file is the
Ollama call; everything the evaluation reads is produced by shared code in
base, so this extractor and the Claude one cannot diverge. Writes to
`outbreaks_llama` by default.

  ollama pull llama3.1:8b
  python -m extraction.llama_extractor --limit 5 --model llama3.1:8b
"""

import logging
import os
import sys

import ollama

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from extraction import base

logger = logging.getLogger(__name__)


def extract_entities(
    don_text: str,
    model: str = "llama3.1:8b",
    host: str = "http://localhost:11434",
) -> base.OutbreakExtraction:
    """Call a local Ollama model to extract grouped disease/country entities."""
    client = ollama.Client(host=host)
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": base.SYSTEM_PROMPT},
            {"role": "user", "content": don_text},
        ],
        format=base.OutbreakExtraction.model_json_schema(),
    )
    return base.OutbreakExtraction.model_validate_json(response.message.content)


def populate(
    db_path: str,
    table: str = "outbreaks_llama",
    limit=None,
    seed: int = 42,
    model: str = "llama3.1:8b",
    host: str = "http://localhost:11434",
) -> dict:
    return base.run(
        db_path,
        table,
        lambda text: extract_entities(text, model=model, host=host),
        limit=limit,
        seed=seed,
    )


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Llama outbreak entity extractor (via Ollama)")
    parser.add_argument("--db", help="SQLite path (default: from config/config.yaml)")
    parser.add_argument("--table", default="outbreaks_llama", help="Target table")
    parser.add_argument("--limit", type=int, default=5, help="DONs to process (default: 5)")
    parser.add_argument("--seed", type=int, default=42, help="Sampling seed (default: 42)")
    parser.add_argument("--model", default="llama3.1:8b", help="Ollama model")
    parser.add_argument("--host", default="http://localhost:11434", help="Ollama host URL")
    args = parser.parse_args()

    result = populate(
        base.load_db_path(args.db),
        table=args.table,
        limit=args.limit,
        seed=args.seed,
        model=args.model,
        host=args.host,
    )
    base.print_stats(result)
