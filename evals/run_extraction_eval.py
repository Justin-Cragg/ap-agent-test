"""Extraction accuracy eval on your own labelled invoices.

Layout (one directory per case, kept out of git if it holds real data):

    evals/cases/<case>/document.pdf      (or .png / .jpg)
    evals/cases/<case>/expected.json     {"vendor_name": ..., "invoice_number": ..., "invoice_date": "YYYY-MM-DD",
                                          "due_date": ..., "po_number": ..., "currency": ..., "total": 123.45,
                                          "tax_total": ..., "line_count": 3, "document_type": "invoice"}

Run (calls the model once per case and costs money - it asks for confirmation):

    python evals/run_extraction_eval.py --cases evals/cases [--yes] [--out results.json]

Reports per-field accuracy and every mismatch, so you can compare models,
effort levels or prompt changes before rolling them out. Use it together with
the production shadow-mode report (/reports/shadow), which measures how often
approvers accept the agent's decisions unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ap_agent.ap.duplicates import normalize_invoice_number
from ap_agent.ap.extraction import extract_invoice
from ap_agent.config import get_settings
from ap_agent.llm.client import LLM
from ap_agent.mail.base import Attachment

FIELDS = ["vendor_name", "invoice_number", "invoice_date", "due_date", "po_number", "currency", "total", "tax_total", "line_count", "document_type"]
TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def same(field: str, got, want) -> bool:
    if want is None:
        return got in (None, "", 0)
    if field in ("total", "tax_total"):
        return got is not None and abs(float(got) - float(want)) < 0.011
    if field == "invoice_number":
        return normalize_invoice_number(got) == normalize_invoice_number(want)
    if field == "vendor_name":
        return bool(got) and (want.lower() in got.lower() or got.lower() in want.lower())
    return str(got or "").strip().lower() == str(want).strip().lower()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=Path, default=Path("evals/cases"))
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    cases = sorted(d for d in args.cases.iterdir() if (d / "expected.json").exists())
    if not cases:
        sys.exit(f"No cases in {args.cases}")
    if not args.yes and input(f"Run {len(cases)} extraction calls against the configured model (billed)? [y/N] ").lower() != "y":
        sys.exit(1)

    llm = LLM(get_settings())
    hits = {f: 0 for f in FIELDS}
    results = []
    for case in cases:
        doc = next(p for p in case.iterdir() if p.suffix.lower() in TYPES)
        want = json.loads((case / "expected.json").read_text())
        got = extract_invoice(llm, Attachment(id=case.name, filename=doc.name, content_type=TYPES[doc.suffix.lower()], data=doc.read_bytes())).model_dump()
        got["line_count"] = len(got.get("lines") or [])
        misses = {}
        for f in FIELDS:
            if f not in want:
                hits[f] += 1
                continue
            if same(f, got.get(f), want[f]):
                hits[f] += 1
            else:
                misses[f] = {"expected": want[f], "got": got.get(f)}
        results.append({"case": case.name, "misses": misses, "confidence": got.get("confidence")})
        print(f"{case.name}: {'OK' if not misses else misses}")

    n = len(cases)
    summary = {"cases": n, "field_accuracy": {f: round(hits[f] / n, 3) for f in FIELDS}, "all_fields_correct": round(sum(not r["misses"] for r in results) / n, 3)}
    print(json.dumps(summary, indent=2))
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=2, default=str))


if __name__ == "__main__":
    main()
