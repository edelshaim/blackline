import json
import sys

import pytest

from blackline_tool.cli import build_compare_options, main, normalize_formats, parse_args


def test_parse_args_accepts_strict_legal_flag() -> None:
    args = parse_args(["a.txt", "b.txt", "--strict-legal"])
    assert args.strict_legal is True


def test_build_compare_options_uses_legal_profile_for_strict_flag() -> None:
    args = parse_args(["a.txt", "b.txt", "--strict-legal", "--ignore-numbering"])
    options = build_compare_options(args)

    assert options.profile_name == "legal"
    assert options.ignore_case is True
    assert options.ignore_smart_punctuation is True
    assert options.ignore_numbering is True


def test_normalize_formats_accepts_json() -> None:
    assert normalize_formats("html,json") == {"html", "json"}


def test_normalize_formats_all_includes_json() -> None:
    assert normalize_formats("all") == {"html", "docx", "pdf", "json"}


def test_parse_args_rejects_other_unknown_args() -> None:
    with pytest.raises(SystemExit):
        parse_args(["a.txt", "b.txt", "--not-a-real-flag"])


def test_build_compare_options_supports_contract_profile() -> None:
    args = parse_args(["a.txt", "b.txt", "--profile", "contract"])
    options = build_compare_options(args)

    assert options.profile_name == "contract"
    assert options.ignore_numbering is True
    assert options.normalize_defined_terms is True


def test_main_generates_html_and_json_outputs(tmp_path, monkeypatch) -> None:
    original = tmp_path / "original.txt"
    revised = tmp_path / "revised.txt"
    output_dir = tmp_path / "output"

    original.write_text("alpha clause\nmoved clause\nclosing clause\n", encoding="utf-8")
    revised.write_text("alpha clause\nclosing clause\nmoved clause\n", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "blackline",
            str(original),
            str(revised),
            "--formats",
            "html,json",
            "--output-dir",
            str(output_dir),
            "--profile",
            "presentation",
        ],
    )

    assert main() == 0

    html_output = (output_dir / "blackline_report.html").read_text(encoding="utf-8")
    json_output = json.loads((output_dir / "blackline_report.json").read_text(encoding="utf-8"))

    assert "Blackline Document" in html_output
    assert "Profile: presentation" in html_output
    assert 'class="del"' in html_output
    assert 'class="ins"' in html_output
    assert json_output["summary"]["moved_sections"] == 1
