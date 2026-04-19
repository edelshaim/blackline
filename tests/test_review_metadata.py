from blackline_tool.core import DocumentBlock, build_report_from_blocks
from blackline_tool.core import CompareOptions


def _single_section(
    original_text: str,
    revised_text: str,
    *,
    kind: str = "paragraph",
    container: str = "body",
):
    report = build_report_from_blocks(
        [DocumentBlock(label="Paragraph 1", text=original_text, kind=kind, container=container)],
        [DocumentBlock(label="Paragraph 1", text=revised_text, kind=kind, container=container)],
        source_a="original.docx",
        source_b="revised.docx",
        options=CompareOptions(detect_moves=False),
    )
    assert len(report.sections) == 1
    return report.sections[0]


def test_change_facets_tag_whitespace_only_inserts() -> None:
    report = build_report_from_blocks(
        [],
        [DocumentBlock(label="Paragraph 1", text="   ", kind="paragraph", container="body")],
        source_a="original.docx",
        source_b="revised.docx",
        options=CompareOptions(detect_moves=False),
    )
    section = report.sections[0]

    assert section.kind == "insert"
    assert "whitespace" in section.change_facets
    assert "content" not in section.change_facets


def test_change_facets_tag_capitalization_only_replacements() -> None:
    section = _single_section("Termination Date", "termination date")

    assert section.kind == "replace"
    assert "capitalization" in section.change_facets
    assert "content" not in section.change_facets


def test_change_facets_tag_punctuation_only_replacements() -> None:
    section = _single_section("Clause 1, effective immediately.", "Clause 1 effective immediately")

    assert section.kind == "replace"
    assert "punctuation" in section.change_facets
    assert "content" not in section.change_facets


def test_change_facets_tag_numbering_only_replacements() -> None:
    section = _single_section("Section 1. Scope", "Section 2. Scope")

    assert section.kind == "replace"
    assert "numbering" in section.change_facets
    assert "content" not in section.change_facets


def test_change_facets_include_structural_location_and_table_signals() -> None:
    header_section = _single_section(
        "Header original",
        "Header revised",
        container="/word/header1.xml",
    )
    table_section = _single_section(
        "Fee | $100",
        "Cost | $100",
        kind="table_row",
        container="body",
    )

    assert header_section.location_kind == "header"
    assert "header" in header_section.change_facets
    assert "content" in header_section.change_facets

    assert table_section.location_kind == "body"
    assert "table" in table_section.change_facets
    assert "content" in table_section.change_facets


def test_change_facets_tag_style_only_changes_as_formatting() -> None:
    report = build_report_from_blocks(
        [DocumentBlock(label="Paragraph 1", text="Defined Term", kind="paragraph", style_name="Body Text")],
        [DocumentBlock(label="Paragraph 1", text="Defined Term", kind="paragraph", style_name="Heading 2")],
        source_a="original.docx",
        source_b="revised.docx",
        options=CompareOptions(detect_moves=False),
    )
    section = report.sections[0]

    assert section.kind == "replace"
    assert "formatting" in section.change_facets
    assert "style" in section.change_facets
    assert "content" not in section.change_facets
    assert "capitalization" not in section.change_facets


def test_change_facets_tag_alignment_and_layout_changes() -> None:
    report = build_report_from_blocks(
        [
            DocumentBlock(
                label="Paragraph 1",
                text="Payment Terms",
                kind="paragraph",
                alignment=0,
                layout={"indent_left": 0, "spacing_before": 120, "spacing_after": 120},
            )
        ],
        [
            DocumentBlock(
                label="Paragraph 1",
                text="Payment Terms",
                kind="paragraph",
                alignment=2,
                layout={"indent_left": 360, "spacing_before": 0, "spacing_after": 240},
            )
        ],
        source_a="original.docx",
        source_b="revised.docx",
        options=CompareOptions(detect_moves=False),
    )
    section = report.sections[0]

    assert section.kind == "replace"
    assert "formatting" in section.change_facets
    assert "alignment" in section.change_facets
    assert "layout" in section.change_facets
    assert "indentation" in section.change_facets
    assert "spacing" in section.change_facets


def test_change_facets_include_header_for_formatting_only_changes() -> None:
    report = build_report_from_blocks(
        [
            DocumentBlock(
                label="Header 1 Paragraph 1",
                text="Confidential",
                kind="paragraph",
                container="/word/header1.xml",
                style_name="Header",
            )
        ],
        [
            DocumentBlock(
                label="Header 1 Paragraph 1",
                text="Confidential",
                kind="paragraph",
                container="/word/header1.xml",
                style_name="Title",
            )
        ],
        source_a="original.docx",
        source_b="revised.docx",
        options=CompareOptions(detect_moves=False),
    )
    section = report.sections[0]

    assert section.kind == "replace"
    assert section.location_kind == "header"
    assert "header" in section.change_facets
    assert "formatting" in section.change_facets
