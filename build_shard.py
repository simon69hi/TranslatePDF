#!/usr/bin/env python3
"""Convert easy-to-write translation entries (with italic phrases) into a translations shard
with computed char offsets.

Input format (JSON, list of entries):
  [
    {"key": "<block-key>", "translation": "<PT text>", "italic": ["phrase 1", "phrase 2"]},
    ...
  ]

Output: writes <shard_path> as {"<key>": {"translation": "...", "italic_runs": [[a,b], ...]}}.

Each italic phrase is searched in the translation text; ALL occurrences are converted to
italic ranges (overlapping ranges are merged). If a phrase isn't found, a warning is
printed but the entry is still written with empty runs for that phrase.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path


def find_all(haystack: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    out = []
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            break
        out.append((i, i + len(needle)))
        start = i + 1
    return out


def merge_ranges(rs: list[tuple[int, int]]) -> list[list[int]]:
    if not rs:
        return []
    rs = sorted(rs)
    merged = [list(rs[0])]
    for a, b in rs[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} INPUT.json OUTPUT_SHARD.json")
    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    entries = json.loads(inp.read_text())
    shard: dict = {}
    for e in entries:
        key = e["key"]
        translation = e["translation"]
        phrases = e.get("italic", []) or []
        runs: list[tuple[int, int]] = []
        for ph in phrases:
            found = find_all(translation, ph)
            if not found:
                print(f"WARN: phrase {ph!r} not found in translation for key {key}", file=sys.stderr)
                continue
            runs.extend(found)
        shard[key] = {
            "translation": translation,
            "italic_runs": merge_ranges(runs),
        }
    out.write_text(json.dumps(shard, ensure_ascii=False, indent=2))
    print(f"Wrote {out} with {len(shard)} entries")


if __name__ == "__main__":
    main()
