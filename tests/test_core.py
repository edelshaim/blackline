from blackline_tool.core import compare_paragraphs, diff_words


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
    assert rebuilt == "payment due within 30 days"
