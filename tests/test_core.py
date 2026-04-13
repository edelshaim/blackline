import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import blackline_tool.core as core
from blackline_tool.core import (
    DocumentBlock,
    _compare_paragraphs,
    build_report_from_blocks,
    compare_paragraphs,
    compare_paragraphs_strict,
    diff_words,
    generate_report,
    load_document_blocks,
    load_text,
    write_docx_blackline_with_formatting,
    write_json_report,
)
from blackline_tool.strict import CompareOptions, options_for_profile


def test_diff_words_marks_insert_and_delete() -> None:
    tokens = diff_words("payment due in 30 days", "payment due within 45 days")
    kinds = [token.kind for token in tokens]
    assert "insert" in kinds
    assert "delete" in kinds


def test_compare_paragraphs_detects_inserted_paragraph() -> None:
    report = compare_paragraphs(
        ["alpha clause", "omega clause"],
        ["alpha clause", "new section", "omega clause"],
    )
    assert len(report) == 3
    assert any(token.kind == "insert" for token in report[1].tokens)


def test_diff_words_preserves_whitespace_tokens() -> None:
    tokens = diff_words("payment due in 30 days", "payment due within 30 days")
    rebuilt = "".join(token.text for token in tokens if token.kind != "delete")
    assert any(token.text.isspace() for token in tokens)
    assert rebuilt == "payment due within 30 days"


def test_compare_paragraphs_replaced_text_keeps_spacing() -> None:
    report = compare_paragraphs(["payment due in 30 days"], ["payment due within 30 days"])
    rebuilt = "".join(token.text for token in report[0].tokens if token.kind != "delete")
    assert rebuilt == "payment due within 30 days"


def test_compare_paragraphs_aligns_replace_blocks_to_reduce_noise() -> None:
    report = compare_paragraphs(
        ["A", "B", "C", "D"],
        ["A", "B updated", "C", "D"],
    )

    assert len(report) == 4
    assert report[0].tokens[0].kind == "equal"
    assert any(token.kind in {"insert", "delete"} for token in report[1].tokens)
    assert report[2].tokens[0].kind == "equal"
    assert report[3].tokens[0].kind == "equal"


def test_strict_mode_suppresses_case_and_quote_only_changes() -> None:
    report = compare_paragraphs_strict(
        ["The Agency’s Decision was final."],
        ["the agency's decision was final."],
    )

    assert len(report) == 1
    assert all(token.kind == "equal" for token in report[0].tokens)


def test_compare_paragraphs_preserves_blank_paragraphs() -> None:
    report = compare_paragraphs(
        ["alpha clause", "", "omega clause"],
        ["alpha clause", "", "omega clause"],
    )

    assert len(report) == 3
    assert report[1].tokens[0].kind == "equal"
    assert report[1].tokens[0].text == ""


def test_build_report_detects_moved_section() -> None:
    report = build_report_from_blocks(
        [
            DocumentBlock(label="Paragraph 1", text="Intro", kind="paragraph"),
            DocumentBlock(label="Paragraph 2", text="Moved clause", kind="paragraph"),
            DocumentBlock(label="Paragraph 3", text="Closing", kind="paragraph"),
        ],
        [
            DocumentBlock(label="Paragraph 1", text="Intro", kind="paragraph"),
            DocumentBlock(label="Paragraph 2", text="Closing", kind="paragraph"),
            DocumentBlock(label="Paragraph 3", text="Moved clause", kind="paragraph"),
        ],
        source_a="old.docx",
        source_b="new.docx",
        options=CompareOptions(detect_moves=True),
    )

    moved_sections = [section for section in report.changed_sections if section.kind == "move"]
    assert len(moved_sections) == 1
    assert moved_sections[0].move_from_label in {"Paragraph 2", "Paragraph 3"}
    assert moved_sections[0].move_to_label in {"Paragraph 2", "Paragraph 3"}
    assert moved_sections[0].move_from_label != moved_sections[0].move_to_label


def test_build_report_can_disable_move_detection() -> None:
    report = build_report_from_blocks(
        [
            DocumentBlock(label="Paragraph 1", text="Intro", kind="paragraph"),
            DocumentBlock(label="Paragraph 2", text="Moved clause", kind="paragraph"),
            DocumentBlock(label="Paragraph 3", text="Closing", kind="paragraph"),
        ],
        [
            DocumentBlock(label="Paragraph 1", text="Intro", kind="paragraph"),
            DocumentBlock(label="Paragraph 2", text="Closing", kind="paragraph"),
            DocumentBlock(label="Paragraph 3", text="Moved clause", kind="paragraph"),
        ],
        source_a="old.docx",
        source_b="new.docx",
        options=CompareOptions(detect_moves=False),
    )

    assert all(section.kind != "move" for section in report.sections)
    assert report.summary.moved_sections == 0


def test_presentation_profile_ignores_whitespace_only_changes() -> None:
    report = build_report_from_blocks(
        [DocumentBlock(label="Paragraph 1", text="Section  4", kind="paragraph")],
        [DocumentBlock(label="Paragraph 1", text="Section   4", kind="paragraph")],
        source_a="old.txt",
        source_b="new.txt",
        options=options_for_profile("presentation"),
    )

    assert report.summary.changed_sections == 0


def test_write_json_report_serializes_summary_and_sections(tmp_path: Path) -> None:
    report = build_report_from_blocks(
        [DocumentBlock(label="Paragraph 1", text="Alpha", kind="paragraph")],
        [DocumentBlock(label="Paragraph 1", text="Beta", kind="paragraph")],
        source_a="old.txt",
        source_b="new.txt",
        options=CompareOptions(),
    )

    output = tmp_path / "report.json"
    write_json_report(report, output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["summary"]["changed_sections"] == 1
    assert payload["sections"][0]["kind"] == "replace"
    assert payload["sections"][0]["revised_text"] == "Beta"


def test_load_text_keeps_blank_docx_paragraphs(monkeypatch) -> None:
    fake_doc = SimpleNamespace(
        paragraphs=[
            SimpleNamespace(text="alpha clause"),
            SimpleNamespace(text=""),
            SimpleNamespace(text="omega clause"),
        ]
    )

    monkeypatch.setattr(core, "Document", lambda path: fake_doc)

    assert load_text(Path("contract.docx")) == ["alpha clause", "", "omega clause"]


def test_load_document_blocks_includes_table_rows_in_order(monkeypatch) -> None:
    class FakeDocument:
        element = object()

    class FakeParagraph:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeCell:
        def __init__(self, *texts: str) -> None:
            self.paragraphs = [SimpleNamespace(text=text) for text in texts]

    class FakeRow:
        def __init__(self, cells) -> None:
            self.cells = cells

    class FakeTable:
        def __init__(self, rows) -> None:
            self.rows = rows

    fake_blocks = [
        FakeParagraph("Intro clause"),
        FakeTable(
            [
                FakeRow([FakeCell("Fee"), FakeCell("$100")]),
                FakeRow([FakeCell("Term"), FakeCell("12 months")]),
            ]
        ),
        FakeParagraph("Closing clause"),
    ]

    monkeypatch.setattr(core, "Document", lambda path: FakeDocument())
    monkeypatch.setattr(core, "DocxDocumentType", FakeDocument)
    monkeypatch.setattr(core, "CT_P", object())
    monkeypatch.setattr(core, "CT_Tbl", object())
    monkeypatch.setattr(core, "DocxParagraph", FakeParagraph)
    monkeypatch.setattr(core, "DocxTable", FakeTable)
    monkeypatch.setattr(core, "_iter_docx_block_items", lambda doc: fake_blocks)

    blocks = load_document_blocks(Path("contract.docx"))

    assert [block.label for block in blocks] == [
        "Paragraph 1",
        "Table 1 Row 1",
        "Table 1 Row 2",
        "Paragraph 2",
    ]
    assert blocks[1].text == "Fee | $100"


def test_private_compare_alias_remains_available() -> None:
    report = _compare_paragraphs(["alpha"], ["alpha"])
    assert len(report) == 1
    assert report[0].tokens[0].kind == "equal"


def test_generate_native_docx_blackline_preserves_headers_and_inserts_rows(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    DocxDocument = docx.Document

    original = tmp_path / "original.docx"
    revised = tmp_path / "revised.docx"
    output = tmp_path / "output.docx"

    original_doc = DocxDocument()
    original_doc.sections[0].header.paragraphs[0].text = "Header clause 1"
    original_doc.add_paragraph("Clause 1")
    table = original_doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Fee"
    table.rows[0].cells[1].text = "$100"
    original_doc.save(original)

    revised_doc = DocxDocument()
    revised_doc.sections[0].header.paragraphs[0].text = "Header clause 2"
    revised_doc.add_paragraph("Clause 1 updated")
    revised_table = revised_doc.add_table(rows=2, cols=2)
    revised_table.rows[0].cells[0].text = "Fee"
    revised_table.rows[0].cells[1].text = "$100"
    revised_table.rows[1].cells[0].text = "Term"
    revised_table.rows[1].cells[1].text = "12 months"
    revised_doc.save(revised)

    write_docx_blackline_with_formatting(original, revised, output, options=options_for_profile("contract"))

    rendered = DocxDocument(output)
    assert rendered.sections[0].header.paragraphs[0].text == "Header clause 1Header clause 2"
    assert "Clause 1" in rendered.paragraphs[0].text
    assert "Clause 1 updated" in rendered.paragraphs[0].text
    assert len(rendered.tables[0].rows) == 2
    assert rendered.tables[0].rows[1].cells[0].text == "Term"
    assert rendered.tables[0].rows[1].cells[1].text == "12 months"


def test_generate_report_skips_empty_header_footer_placeholders(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    DocxDocument = docx.Document

    original = tmp_path / "original.docx"
    revised = tmp_path / "revised.docx"

    doc = DocxDocument()
    doc.sections[0].header.paragraphs[0].text = "Header clause 1"
    doc.sections[0].footer.paragraphs[0].text = "Footer clause 1"
    doc.add_paragraph("Body clause 1")
    doc.save(original)

    doc = DocxDocument()
    doc.sections[0].header.paragraphs[0].text = "Header clause 2"
    doc.sections[0].footer.paragraphs[0].text = "Footer clause 2"
    doc.add_paragraph("Body clause 2")
    doc.save(revised)

    report = generate_report(original, revised, options=options_for_profile("contract"))

    labels = [section.label for section in report.document_sections]
    assert "Header 2 Paragraph 1" not in labels
    assert "Header 3 Paragraph 1" not in labels
    assert "Footer 2 Paragraph 1" not in labels
    assert "Footer 3 Paragraph 1" not in labels
