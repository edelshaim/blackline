from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .core import (
    RedlineReport,
    generate_report,
    write_docx_blackline_with_formatting,
    write_docx_report,
    write_html_report,
    write_json_report,
    write_pdf_report,
)
from .strict import CompareOptions

VALID_FORMATS = {"html", "docx", "pdf", "json"}


@dataclass(slots=True)
class GenerationResult:
    report: RedlineReport
    files: dict[str, Path]
    preview_html: Path


def generate_outputs(
    original_path: Path,
    revised_path: Path,
    output_dir: Path,
    *,
    base_name: str,
    formats: set[str],
    options: CompareOptions,
    ensure_html_preview: bool = False,
) -> GenerationResult:
    output_dir.mkdir(parents=True, exist_ok=True)

    report = generate_report(original_path, revised_path, options=options)
    files: dict[str, Path] = {}
    docx_output: Path | None = None
    temp_docx_for_pdf: Path | None = None

    html_output = output_dir / f"{base_name}.html"
    if "html" in formats or ensure_html_preview:
        orig_bytes = original_path.read_bytes() if original_path.suffix.lower() == ".docx" else None
        rev_bytes = revised_path.read_bytes() if revised_path.suffix.lower() == ".docx" else None
        write_html_report(
            report,
            html_output,
            original_bytes=orig_bytes,
            revised_bytes=rev_bytes,
        )
        if "html" in formats:
            files["html"] = html_output

    if "docx" in formats:
        docx_output = output_dir / f"{base_name}.docx"
        _write_docx_output(original_path, revised_path, report, docx_output, options=options)
        files["docx"] = docx_output

    if "pdf" in formats:
        if docx_output is None and original_path.suffix.lower() == ".docx" and revised_path.suffix.lower() == ".docx":
            temp_docx_for_pdf = output_dir / f".{base_name}.pdf-source.docx"
            _write_docx_output(original_path, revised_path, report, temp_docx_for_pdf, options=options)
            docx_output = temp_docx_for_pdf

        pdf_output = output_dir / f"{base_name}.pdf"
        write_pdf_report(report, pdf_output, docx_source_path=docx_output)
        files["pdf"] = pdf_output

    if "json" in formats:
        json_output = output_dir / f"{base_name}.json"
        write_json_report(report, json_output)
        files["json"] = json_output

    if temp_docx_for_pdf is not None and temp_docx_for_pdf.exists():
        temp_docx_for_pdf.unlink()

    return GenerationResult(
        report=report,
        files=files,
        preview_html=html_output,
    )


def _write_docx_output(
    original_path: Path,
    revised_path: Path,
    report: RedlineReport,
    output_path: Path,
    *,
    options: CompareOptions,
) -> None:
    if original_path.suffix.lower() == ".docx" and revised_path.suffix.lower() == ".docx":
        write_docx_blackline_with_formatting(
            original_path,
            revised_path,
            output_path,
            options=options,
        )
        return
    write_docx_report(report, output_path, template_path=None)
