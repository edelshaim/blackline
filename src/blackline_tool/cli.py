from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .core import (
    compare_paragraphs,
    compare_paragraphs_strict,
    load_text,
    write_docx_blackline_with_formatting,
    write_docx_report,
    write_html_report,
    write_pdf_report,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="blackline", description="Generate a local blackline report for .docx or .txt documents.")
    parser.add_argument("original", type=Path, help="Path to original document (.docx or .txt)")
    parser.add_argument("revised", type=Path, help="Path to revised document (.docx or .txt)")
    parser.add_argument("--formats", default="html", help="Comma-separated outputs: html,docx,pdf or all (default: html)")
    parser.add_argument("--output-dir", type=Path, default=Path("./output"), help="Output directory for generated reports")
    parser.add_argument("--base-name", default="blackline_report", help="Base filename for generated reports")
    parser.add_argument("--strict-legal", "--strict_legal", "--strict-legal-mode", dest="strict_legal", action="store_true", help="Suppress non-substantive edits (e.g., case/quote/dash normalization) for cleaner legal blacklines")
    parser = argparse.ArgumentParser(
        prog="blackline",
        description="Generate a local blackline report for .docx or .txt documents.",
    )
    parser.add_argument("original", type=Path, help="Path to original document (.docx or .txt)")
    parser.add_argument("revised", type=Path, help="Path to revised document (.docx or .txt)")
    parser.add_argument(
        "--formats",
        default="html",
        help="Comma-separated outputs: html,docx,pdf or all (default: html)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./output"),
        help="Output directory for generated reports",
    )
    parser.add_argument(
        "--base-name",
        default="blackline_report",
        help="Base filename for generated reports",
    )
    parser.add_argument(
        "--strict-legal",
        "--strict_legal",
        "--strict-legal-mode",
        dest="strict_legal",
        action="store_true",
        help="Suppress non-substantive edits (e.g., case/quote/dash normalization) for cleaner legal blacklines",
    )
    args, unknown = parser.parse_known_args(argv)
    strict_aliases = {"--strict-legal", "--strict_legal", "--strict-legal-mode"}
    if any(item in strict_aliases for item in unknown):
        args.strict_legal = True
        unknown = [item for item in unknown if item not in strict_aliases]
    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    return args
        action="store_true",
        help="Suppress non-substantive edits (e.g., case/quote/dash normalization) for cleaner legal blacklines",
    )
    return parser.parse_args()


def normalize_formats(raw: str) -> set[str]:
    # normalize and validate requested output formats

    formats = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if "all" in formats:
        return {"html", "docx", "pdf"}
    valid = {"html", "docx", "pdf"}
    invalid = formats - valid
    if invalid:
        raise ValueError(f"Unsupported format(s): {', '.join(sorted(invalid))}")
    return formats or {"html"}


def main() -> int:
    args = parse_args()
    try:
        formats = normalize_formats(args.formats)
        args.output_dir.mkdir(parents=True, exist_ok=True)

        original_paragraphs = load_text(args.original)
        revised_paragraphs = load_text(args.revised)
        report = (
            compare_paragraphs_strict(original_paragraphs, revised_paragraphs)
            if args.strict_legal
            else compare_paragraphs(original_paragraphs, revised_paragraphs)
        )

        stem = args.base_name
        if "html" in formats:
            output = args.output_dir / f"{stem}.html"
            write_html_report(report, output, args.original.name, args.revised.name)
            print(f"Generated HTML: {output}")
        if "docx" in formats:
            output = args.output_dir / f"{stem}.docx"
            if args.original.suffix.lower() == ".docx" and args.revised.suffix.lower() == ".docx":
                write_docx_blackline_with_formatting(
                    args.original,
                    args.revised,
                    output,
                    substantive_only=args.strict_legal,
                )
            else:
                write_docx_report(report, output, args.original.name, args.revised.name)
            print(f"Generated DOCX: {output}")
        if "pdf" in formats:
            output = args.output_dir / f"{stem}.pdf"
            write_pdf_report(report, output, args.original.name, args.revised.name)
            print(f"Generated PDF: {output}")
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
