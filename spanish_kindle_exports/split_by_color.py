"""
split_by_color.py

Splits a translated highlights CSV into separate files per highlight color.
A word highlighted in multiple colors appears in each relevant file.

Usage:
    python split_by_color.py translated/<stem>_translated.csv

Output:
    by_color/<stem>_<color>.csv  — one file per color found in the data

Requirements:
    No additional dependencies beyond the standard library.
"""

import csv
import re
import sys
from pathlib import Path


def load_csv(path: str) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def split_by_color(translated_csv: str) -> None:
    fieldnames, rows = load_csv(translated_csv)

    script_dir = Path(__file__).parent
    out_dir = script_dir / "by_color"
    out_dir.mkdir(exist_ok=True)

    stem = re.sub(r"_translated$", "", Path(translated_csv).stem)

    # Collect rows per color; a row can appear under multiple colors.
    color_buckets: dict[str, list[dict]] = {}
    for row in rows:
        colors_raw = row.get("highlight_colors", "")
        colors = [c.strip() for c in colors_raw.split("|") if c.strip()]
        if not colors:
            colors = ["unknown"]
        for color in colors:
            color_buckets.setdefault(color, []).append(row)

    for color, color_rows in sorted(color_buckets.items()):
        out_path = out_dir / f"{stem}_{color}.csv"
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(color_rows)
        print(f"  {color:12s} → {out_path}  ({len(color_rows)} rows)")


def main():
    if len(sys.argv) != 2:
        print("Usage: python split_by_color.py translated/<stem>_translated.csv")
        sys.exit(1)

    translated_csv = sys.argv[1]
    print(f"Splitting {translated_csv} by highlight color...")
    split_by_color(translated_csv)
    print("Done.")


if __name__ == "__main__":
    main()
