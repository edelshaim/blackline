from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

<<<<<<< ours
from .runner import VALID_FORMATS, generate_outputs
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
=======
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
    args, unknown = parser.parse_known_args(argv)
    strict_aliases = {"--strict-legal", "--strict_legal", "--strict-legal-mode"}
    if any(item in strict_aliases for item in unknown):
        args.strict_legal = True
        unknown = [item for item in unknown if item not in strict_aliases]
    if unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    return args
>>>>>>> theirs


def normalize_formats(raw: str) -> set[str]:
    formats = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if "all" in formats:
        return set(VALID_FORMATS)

    invalid = formats - VALID_FORMATS
    if invalid:
        raise ValueError(f"Unsupported format(s): {', '.join(sorted(invalid))}")
    return formats or {"html"}


def build_compare_options_from_settings(
    *,
    profile: str = "default",
    strict_legal: bool = False,
    ignore_case: bool = False,
    ignore_whitespace: bool = False,
    ignore_smart_punctuation: bool = False,
    ignore_punctuation: bool = False,
    ignore_numbering: bool = False,
    detect_moves: bool = True,
) -> CompareOptions:
    profile_name = "legal" if strict_legal else profile
    options = options_for_profile(profile_name)
    options.ignore_case = options.ignore_case or ignore_case
    options.ignore_whitespace = options.ignore_whitespace or ignore_whitespace
    options.ignore_smart_punctuation = options.ignore_smart_punctuation or ignore_smart_punctuation
    options.ignore_punctuation = options.ignore_punctuation or ignore_punctuation
    options.ignore_numbering = options.ignore_numbering or ignore_numbering
    options.detect_moves = detect_moves
    return options


def build_compare_options(args: argparse.Namespace) -> CompareOptions:
    return build_compare_options_from_settings(
        profile=args.profile,
        strict_legal=args.strict_legal,
        ignore_case=args.ignore_case,
        ignore_whitespace=args.ignore_whitespace,
        ignore_smart_punctuation=args.ignore_smart_punctuation,
        ignore_punctuation=args.ignore_punctuation,
        ignore_numbering=args.ignore_numbering,
        detect_moves=not args.no_detect_moves,
    )


def main() -> int:
    args = parse_args()
    try:
        formats = normalize_formats(args.formats)
<<<<<<< ours
        options = build_compare_options(args)
        result = generate_outputs(
            args.original,
            args.revised,
            args.output_dir,
            base_name=args.base_name,
            formats=formats,
            options=options,
        )
        for format_name in ("html", "docx", "pdf", "json"):
            output = result.files.get(format_name)
            if output is not None:
                print(f"Generated {format_name.upper()}: {output}")
=======
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
>>>>>>> theirs
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
