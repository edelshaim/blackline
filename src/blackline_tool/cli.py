from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .core import (
    generate_report,
    write_docx_blackline_with_formatting,
    write_docx_report,
    write_html_report,
    write_json_report,
    write_pdf_report,
)
from .strict import CompareOptions, options_for_profile


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blackline",
        description="Generate a local blackline report for .docx or .txt documents.",
    )
    parser.add_argument("original", type=Path, help="Path to original document (.docx or .txt)")
    parser.add_argument("revised", type=Path, help="Path to revised document (.docx or .txt)")
    parser.add_argument(
        "--formats",
        default="html",
        help="Comma-separated outputs: html,docx,pdf,json or all (default: html)",
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
        "--profile",
        choices=("default", "legal", "contract", "litigation", "factum", "presentation"),
        default="default",
        help="Comparison normalization profile (default: default)",
    )
    parser.add_argument(
        "--strict-legal",
        "--strict_legal",
        "--strict-legal-mode",
        dest="strict_legal",
        action="store_true",
        help="Shortcut for --profile legal",
    )
    parser.add_argument(
        "--ignore-case",
        action="store_true",
        help="Ignore case-only text changes",
    )
    parser.add_argument(
        "--ignore-whitespace",
        action="store_true",
        help="Ignore whitespace-only text changes",
    )
    parser.add_argument(
        "--ignore-smart-punctuation",
        action="store_true",
        help="Normalize smart quotes and em/en dashes before comparison",
    )
    parser.add_argument(
        "--ignore-punctuation",
        action="store_true",
        help="Ignore punctuation-only token changes",
    )
    parser.add_argument(
        "--ignore-numbering",
        action="store_true",
        help="Ignore numbering token changes such as 1. vs (a)",
    )
    parser.add_argument(
        "--no-detect-moves",
        action="store_true",
        help="Disable move detection for inserted/deleted blocks with matching content",
    )
    return parser.parse_args(argv)


def normalize_formats(raw: str) -> set[str]:
    formats = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if "all" in formats:
        return {"html", "docx", "pdf", "json"}

    valid = {"html", "docx", "pdf", "json"}
    invalid = formats - valid
    if invalid:
        raise ValueError(f"Unsupported format(s): {', '.join(sorted(invalid))}")
    return formats or {"html"}


def build_compare_options(args: argparse.Namespace) -> CompareOptions:
    profile_name = "legal" if args.strict_legal else args.profile
    options = options_for_profile(profile_name)
    options.ignore_case = options.ignore_case or args.ignore_case
    options.ignore_whitespace = options.ignore_whitespace or args.ignore_whitespace
    options.ignore_smart_punctuation = options.ignore_smart_punctuation or args.ignore_smart_punctuation
    options.ignore_punctuation = options.ignore_punctuation or args.ignore_punctuation
    options.ignore_numbering = options.ignore_numbering or args.ignore_numbering
    options.detect_moves = not args.no_detect_moves
    return options


def main() -> int:
    args = parse_args()
    try:
        formats = normalize_formats(args.formats)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        options = build_compare_options(args)
        report = generate_report(args.original, args.revised, options=options)
        docx_output: Path | None = None

        stem = args.base_name
        if "html" in formats:
            output = args.output_dir / f"{stem}.html"
            write_html_report(report, output)
            print(f"Generated HTML: {output}")
        if "docx" in formats:
            output = args.output_dir / f"{stem}.docx"
            docx_output = output
            if args.original.suffix.lower() == ".docx" and args.revised.suffix.lower() == ".docx":
                write_docx_blackline_with_formatting(
                    args.original,
                    args.revised,
                    output,
                    options=options,
                )
            else:
                write_docx_report(report, output, template_path=None)
            print(f"Generated DOCX: {output}")
        if "pdf" in formats:
            output = args.output_dir / f"{stem}.pdf"
            write_pdf_report(report, output, docx_source_path=docx_output)
            print(f"Generated PDF: {output}")
        if "json" in formats:
            output = args.output_dir / f"{stem}.json"
            write_json_report(report, output)
            print(f"Generated JSON: {output}")
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
