#!/usr/bin/env python3
"""
Inline usage_data.json into report_template.html to produce a single
self-contained HTML report that opens straight off the filesystem.

Kept separate from analyse_flexnet.py so the report can be re-styled without
re-parsing the 30 MB log.

Usage:
    python build_report.py [-d usage_data.json] [-t report_template.html]
                           [-o licence_capacity_report.html]
"""

import argparse
import io
import json
import os
import sys

PLACEHOLDER = "/*__DATA__*/"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--data", default="usage_data.json")
    ap.add_argument("-t", "--template", default="report_template.html")
    ap.add_argument("-o", "--out", default="licence_capacity_report.html")
    args = ap.parse_args()

    for p in (args.data, args.template):
        if not os.path.exists(p):
            sys.exit(f"Missing input: {p}")

    # newline="" so the template's line endings survive untouched.
    tpl = io.open(args.template, "r", encoding="utf-8", newline="").read()
    if PLACEHOLDER not in tpl:
        sys.exit(f"Template has no {PLACEHOLDER} placeholder")

    with io.open(args.data, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    # Re-serialise rather than pasting the file through, so the payload is
    # known-valid JSON. Escaping '<' stops a '</script>' inside any log-derived
    # string (a username, a hostname) from closing the script element early;
    # < is valid JSON and parses back to '<'. The payload lives in a
    # <script type="application/json"> block read via JSON.parse, so U+2028 and
    # U+2029 need no special handling -- they are only hazardous in JS source
    # string literals.
    blob = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")

    out = tpl.replace(PLACEHOLDER, blob)
    with io.open(args.out, "w", encoding="utf-8", newline="") as fh:
        fh.write(out)

    print(f"Wrote {args.out}  ({len(out) / 1024:.0f} KB, data {len(blob) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
