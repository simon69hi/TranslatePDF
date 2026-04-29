#!/usr/bin/env python3
"""PDF translator with two modes:

  translate.py extract INPUT.pdf BLOCKS.json
    Dumps all text blocks (with italic ranges, bbox, font size) to a JSON file.

  translate.py render INPUT.pdf OUTPUT.pdf BLOCKS.json TRANSLATIONS
    Applies translations onto the original PDF, preserving layout, fonts,
    italics, and images. TRANSLATIONS may be a single JSON file OR a directory
    containing one or more *.json shards (each mapping block-key -> {translation,
    italic_runs}). Shards are merged; later shards override earlier ones. Blocks
    without a translation entry are left in the original language.

The original PDF is not modified; render writes to OUTPUT.pdf.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import fitz

ROOT = Path(__file__).parent
FONTS_DIR = ROOT / "fonts"

CSS_BASE = """
@font-face { font-family: charis; src: url(Charis-Regular.ttf); }
@font-face { font-family: charis; font-style: italic; src: url(Charis-Italic.ttf); }
@font-face { font-family: charis; font-weight: bold; src: url(Charis-Bold.ttf); }
@font-face { font-family: charis; font-weight: bold; font-style: italic; src: url(Charis-BoldItalic.ttf); }
body { font-family: charis; text-align: justify; line-height: 1.15; margin: 0; padding: 0; }
"""


# ---------- block model ----------

@dataclass
class Block:
    key: str
    page: int
    idx: int
    rect: list  # [x0, y0, x1, y1]
    text: str
    italic_ranges: list
    bold_ranges: list
    font_size: float


def block_key(file_hash: str, page: int, idx: int, text: str) -> str:
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    return f"{file_hash[:8]}:{page:04d}:{idx:03d}:{h}"


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


# ---------- extraction ----------

def extract_blocks(page: fitz.Page, page_num: int, fhash: str) -> list[Block]:
    out: list[Block] = []
    for bi, b in enumerate(page.get_text("dict")["blocks"]):
        if b.get("type") != 0:
            continue
        text_parts: list[str] = []
        italic_ranges: list[list[int]] = []
        bold_ranges: list[list[int]] = []
        x0 = y0 = 99999.0
        x1 = y1 = -99999.0
        sizes: list[float] = []
        offset = 0
        for line in b.get("lines", []):
            for span in line.get("spans", []):
                t = span.get("text", "")
                if not t:
                    continue
                start = offset
                text_parts.append(t)
                offset += len(t)
                end = offset
                flags = span.get("flags", 0)
                if flags & 2:
                    italic_ranges.append([start, end])
                if flags & 16:
                    bold_ranges.append([start, end])
                sx0, sy0, sx1, sy1 = span["bbox"]
                x0 = min(x0, sx0); y0 = min(y0, sy0)
                x1 = max(x1, sx1); y1 = max(y1, sy1)
                sizes.append(span.get("size", 10.0))
            text_parts.append(" ")
            offset += 1
        full = "".join(text_parts)
        text = full.strip()
        if not text or x0 >= x1 or y0 >= y1:
            continue
        lstrip_offset = len(full) - len(full.lstrip())
        L = len(text)

        def shift_clip(rs):
            res = []
            for a, b in rs:
                a2 = max(0, a - lstrip_offset)
                b2 = max(0, b - lstrip_offset)
                if a2 < L and a2 < b2:
                    res.append([a2, min(b2, L)])
            return res

        italic_ranges = shift_clip(italic_ranges)
        bold_ranges = shift_clip(bold_ranges)
        avg_size = sum(sizes) / len(sizes) if sizes else 10.0
        out.append(Block(
            key=block_key(fhash, page_num, bi, text),
            page=page_num, idx=bi,
            rect=[x0, y0, x1, y1],
            text=text,
            italic_ranges=italic_ranges,
            bold_ranges=bold_ranges,
            font_size=avg_size,
        ))
    return out


def cmd_extract(args: argparse.Namespace) -> None:
    inp = Path(args.input)
    out = Path(args.blocks_json)
    if not inp.exists():
        sys.exit(f"ERROR: input not found: {inp}")
    fhash = file_hash(inp)
    doc = fitz.open(str(inp))
    all_blocks: list[dict] = []
    for pnum in range(len(doc)):
        for blk in extract_blocks(doc[pnum], pnum, fhash):
            all_blocks.append(asdict(blk))
    payload = {
        "input_file": inp.name,
        "input_hash": fhash,
        "page_count": len(doc),
        "block_count": len(all_blocks),
        "blocks": all_blocks,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    italics = sum(len(b["italic_ranges"]) for b in all_blocks)
    print(f"Extracted {len(all_blocks)} blocks across {len(doc)} pages")
    print(f"Italic spans: {italics}")
    print(f"Wrote {out}")


# ---------- HTML builder ----------

def to_html(text: str, italic_ranges: list) -> str:
    L = len(text)
    events: dict[int, list[tuple[str, str]]] = {}
    for a, b in italic_ranges:
        events.setdefault(int(a), []).append(("open", "i"))
        events.setdefault(int(b), []).append(("close", "i"))
    out: list[str] = []
    for i in range(L + 1):
        if i in events:
            for kind, tag in events[i]:
                if kind == "close":
                    out.append(f"</{tag}>")
            for kind, tag in events[i]:
                if kind == "open":
                    out.append(f"<{tag}>")
        if i < L:
            ch = text[i]
            if ch == "&":
                out.append("&amp;")
            elif ch == "<":
                out.append("&lt;")
            elif ch == ">":
                out.append("&gt;")
            elif ch == "\n":
                out.append("<br/>")
            else:
                out.append(ch)
    return "".join(out)


# ---------- render ----------

def render_block(page: fitz.Page, rect: fitz.Rect, font_size: float,
                 src_was_bold: bool, translation: str, italic_runs: list,
                 archive: fitz.Archive) -> bool:
    page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1))
    body = to_html(translation, italic_runs)
    if src_was_bold:
        body = f"<b>{body}</b>"
    css = CSS_BASE + f"\nbody {{ font-size: {font_size:.1f}px; }}"

    spare, _ = page.insert_htmlbox(rect, body, css=css, archive=archive, scale_low=0.5)
    if spare >= 0:
        return True
    page_h = page.rect.height
    extra = (rect.y1 - rect.y0) * 0.3
    new_y1 = min(rect.y1 + extra, page_h - 5)
    expanded = fitz.Rect(rect.x0, rect.y0, rect.x1, new_y1)
    page.draw_rect(expanded, color=(1, 1, 1), fill=(1, 1, 1))
    spare, _ = page.insert_htmlbox(expanded, body, css=css, archive=archive, scale_low=0.4)
    if spare >= 0:
        return True
    spare, _ = page.insert_htmlbox(expanded, body, css=css, archive=archive, scale_low=0.2)
    return spare >= 0


def cmd_render(args: argparse.Namespace) -> None:
    inp = Path(args.input)
    out = Path(args.output)
    blocks_path = Path(args.blocks_json)
    trans_path = Path(args.translations_json)

    if not inp.exists():
        sys.exit(f"ERROR: input not found: {inp}")
    if not blocks_path.exists():
        sys.exit(f"ERROR: blocks json not found: {blocks_path}")
    if not trans_path.exists():
        sys.exit(f"ERROR: translations path not found: {trans_path}")
    needed = ["Charis-Regular.ttf", "Charis-Italic.ttf", "Charis-Bold.ttf", "Charis-BoldItalic.ttf"]
    missing = [f for f in needed if not (FONTS_DIR / f).exists()]
    if missing:
        sys.exit(f"ERROR: Charis fonts missing in {FONTS_DIR}: {missing}")

    blocks_payload = json.loads(blocks_path.read_text())
    translations: dict = {}
    if trans_path.is_dir():
        shards = sorted(trans_path.glob("*.json"))
        if not shards:
            sys.exit(f"ERROR: no *.json shards in {trans_path}")
        for shard in shards:
            try:
                translations.update(json.loads(shard.read_text()))
            except Exception as e:
                print(f"WARNING: skipping malformed shard {shard.name}: {e}", file=sys.stderr)
        print(f"Loaded {len(translations)} translations from {len(shards)} shards in {trans_path}")
    else:
        translations = json.loads(trans_path.read_text())
        print(f"Loaded {len(translations)} translations from {trans_path}")

    fhash = file_hash(inp)
    if fhash != blocks_payload.get("input_hash"):
        print(f"WARNING: input hash mismatch with blocks.json — proceeding anyway", file=sys.stderr)

    archive = fitz.Archive(str(FONTS_DIR))
    doc = fitz.open(str(inp))

    # Group blocks by page
    blocks_by_page: dict[int, list[dict]] = {}
    for b in blocks_payload["blocks"]:
        blocks_by_page.setdefault(b["page"], []).append(b)

    overflow: list[tuple[int, int]] = []
    rendered = 0
    skipped_no_translation = 0

    for pnum in range(len(doc)):
        page = doc[pnum]
        for b in blocks_by_page.get(pnum, []):
            tr = translations.get(b["key"])
            if not tr:
                skipped_no_translation += 1
                continue
            translation = tr["translation"]
            italic_runs = tr.get("italic_runs", [])
            rect = fitz.Rect(*b["rect"])
            src_text = b["text"]
            src_was_bold = (
                bool(b["bold_ranges"])
                and b["bold_ranges"][0][0] == 0
                and b["bold_ranges"][0][1] >= max(1, len(src_text) * 0.8)
            )
            ok = render_block(page, rect, b["font_size"], src_was_bold,
                              translation, italic_runs, archive)
            if not ok:
                overflow.append((pnum + 1, b["idx"]))
            rendered += 1

    doc.save(str(out), garbage=4, deflate=True)
    doc.close()

    print(f"Rendered {rendered} blocks to {out}")
    if skipped_no_translation:
        print(f"Skipped (no translation): {skipped_no_translation} blocks (left in original EN)")
    if overflow:
        print(f"Overflow warnings: {len(overflow)} blocks couldn't fully fit")
        for pg, bi in overflow[:8]:
            print(f"  page {pg} block {bi}")


# ---------- CLI ----------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="Dump source blocks to JSON")
    pe.add_argument("input", help="Input PDF")
    pe.add_argument("blocks_json", help="Output JSON path")
    pe.set_defaults(func=cmd_extract)

    pr = sub.add_parser("render", help="Build translated PDF from blocks + translations")
    pr.add_argument("input", help="Original input PDF")
    pr.add_argument("output", help="Output translated PDF")
    pr.add_argument("blocks_json", help="blocks.json from extract")
    pr.add_argument("translations_json", help="translations.json with {key: {translation, italic_runs}}")
    pr.set_defaults(func=cmd_render)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
