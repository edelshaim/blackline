import sys

import pytest

from blackline_tool.cli import main, parse_args


def test_parse_args_accepts_strict_legal_flag() -> None:
    args = parse_args(["a.txt", "b.txt", "--strict-legal"])
    assert args.strict_legal is True


def test_parse_args_accepts_legacy_strict_aliases() -> None:
    underscored = parse_args(["a.txt", "b.txt", "--strict_legal"])
    mode_alias = parse_args(["a.txt", "b.txt", "--strict-legal-mode"])
    assert underscored.strict_legal is True
    assert mode_alias.strict_legal is True


def test_parse_args_rejects_other_unknown_args() -> None:
    with pytest.raises(SystemExit):
        parse_args(["a.txt", "b.txt", "--not-a-real-flag"])


def test_main_generates_html_without_optional_docx_pdf_dependencies(
    tmp_path,
    monkeypatch,
) -> None:
    original = tmp_path / "original.txt"
    revised = tmp_path / "revised.txt"
    output_dir = tmp_path / "output"

    original.write_text("alpha clause\nomega clause\n", encoding="utf-8")
    revised.write_text("alpha clause\nnew section\nomega clause\n", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "blackline",
            str(original),
            str(revised),
            "--formats",
            "html",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert main() == 0
    html_output = (output_dir / "blackline_report.html").read_text(encoding="utf-8")
    assert "new section" in html_output
