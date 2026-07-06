"""
Claude-based outbreak entity extractor.

Thin wrapper over extraction.base. The only thing specific to this file is the
model call; resolution, keying, schema, the year rule, sampling, and the write
all live in base, so this extractor and the Llama one cannot diverge on
anything the evaluation reads. Writes to `outbreaks_claude` by default.
"""

import logging
import os
import sys

import anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from extraction import base

logger = logging.getLogger(__name__)


def extract_entities(
    don_text: str,
    client: anthropic.Anthropic,
    model: str = "claude-opus-4-8",
) -> base.OutbreakExtraction:
    """Call Claude to extract grouped disease/country entities from a DON."""
    response = client.messages.parse(
        model=model,
        max_tokens=512,
        system=base.SYSTEM_PROMPT,
        messages=[{"role": "user", "content": don_text}],
        output_format=base.OutbreakExtraction,
    )
    return response.parsed_output


def populate(
    db_path: str,
    table: str = "outbreaks_claude",
    limit=None,
    seed: int = 42,
    model: str = "claude-opus-4-8",
) -> dict:
    client = anthropic.Anthropic()
    return base.run(
        db_path,
        table,
        lambda text: extract_entities(text, client, model=model),
        limit=limit,
        seed=seed,
    )


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Claude outbreak entity extractor")
    parser.add_argument("--db", help="SQLite path (default: from config/config.yaml)")
    parser.add_argument("--table", default="outbreaks_claude", help="Target table")
    parser.add_argument("--limit", type=int, default=5, help="DONs to process (default: 5)")
    parser.add_argument("--seed", type=int, default=42, help="Sampling seed (default: 42)")
    parser.add_argument("--model", default="claude-opus-4-8", help="Claude model")
    args = parser.parse_args()

    result = populate(
        base.load_db_path(args.db),
        table=args.table,
        limit=args.limit,
        seed=args.seed,
        model=args.model,
    )
    base.print_stats(result)
