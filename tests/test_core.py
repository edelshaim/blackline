from blackline_tool.core import _compare_paragraphs, compare_paragraphs, compare_paragraphs_strict, diff_words
from blackline_tool.core import compare_paragraphs, compare_paragraphs_strict, diff_words


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


def test_diff_words_preserves_multiple_spaces() -> None:
    tokens = diff_words("Section  4", "Section   4")
    rebuilt = "".join(token.text for token in tokens if token.kind != "delete")
    assert rebuilt == "Section   4"


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


def test_private_compare_alias_remains_available() -> None:
    report = _compare_paragraphs(["alpha"], ["alpha"])
    assert len(report) == 1
    assert report[0].tokens[0].kind == "equal"
