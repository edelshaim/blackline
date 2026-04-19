import base64
import json
from pathlib import Path

from blackline_tool.runner import generate_outputs
from blackline_tool.strict import options_for_profile
from blackline_tool.web import BlacklineWebApp, build_index_page, build_review_shell, create_review_run


def test_generate_outputs_can_force_html_preview(tmp_path: Path) -> None:
    original = tmp_path / "original.txt"
    revised = tmp_path / "revised.txt"
    output_dir = tmp_path / "output"

    original.write_text("alpha clause\nomega clause\n", encoding="utf-8")
    revised.write_text("alpha clause\nomega clause updated\n", encoding="utf-8")

    result = generate_outputs(
        original,
        revised,
        output_dir,
        base_name="preview_only",
        formats={"json"},
        options=options_for_profile("contract"),
        ensure_html_preview=True,
    )

    assert "json" in result.files
    assert "html" not in result.files
    assert result.preview_html.exists()
    assert "Blackline Document" in result.preview_html.read_text(encoding="utf-8")


def test_create_review_run_persists_metadata_and_outputs(tmp_path: Path) -> None:
    payload = {
        "original_name": "original.txt",
        "original_content": base64.b64encode(b"alpha clause\nclosing clause\n").decode("ascii"),
        "revised_name": "revised.txt",
        "revised_content": base64.b64encode(b"alpha clause\nclosing clause updated\n").decode("ascii"),
        "base_name": "studio_review",
        "profile": "contract",
        "formats": ["html", "json"],
        "detect_moves": True,
    }

    metadata = create_review_run(tmp_path, payload)
    run_dir = tmp_path / "runs" / metadata["run_id"]
    stored = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))

    assert stored["profile_name"] == "contract"
    assert stored["files"]["html"] == "studio_review.html"
    assert stored["files"]["json"] == "studio_review.json"
    assert stored["preview_html"] == "studio_review.html"
    assert (run_dir / "outputs" / "studio_review.html").exists()
    assert (run_dir / "outputs" / "studio_review.json").exists()
    assert stored["summary"]["changed_sections"] == 1
    assert stored["sections"][0]["kind"] == "equal"
    assert stored["sections"][1]["kind"] == "replace"
    assert stored["sections"][1]["location_kind"] == "body"
    assert "change_facets" in stored["sections"][1]
    assert "format_change_facets" in stored["sections"][1]


def test_web_pages_expose_upload_and_review_ui() -> None:
    index_page = build_index_page()
    review_page = build_review_shell("run-123")

    assert 'id="compare-form"' in index_page
    assert "/api/compare" in index_page
    assert "Generate Review Run" in index_page
    assert 'id="mode-single"' in index_page
    assert 'id="mode-batch"' in index_page
    assert "Batch Queue" in index_page
    assert "hero-strip" in index_page
    assert "workflow-rail" in index_page
    assert 'id="live-deck"' in index_page
    assert 'id="metric-mode"' in index_page
    assert 'id="metric-ready-fill"' in index_page
    assert 'id="mode-summary"' in index_page
    assert "Review and switch versions" in index_page
    assert 'id="batch-panel"' in index_page
    assert 'id="batch-results"' in index_page
    assert "batch-empty" in index_page
    assert "Retry Failed Items" in index_page
    assert 'id="batch-open-select"' in index_page
    assert "Switch Version" in index_page
    assert "blackline_batch_history_v1" in index_page

    assert "/api/runs/" in review_page
    assert 'id="preview-shell"' in review_page
    assert 'id="frame"' in review_page
    assert 'id="batch-switcher"' in review_page
    assert 'id="batch-run-select"' in review_page
    assert 'id="batch-run-go"' in review_page
    assert 'id="run-profile-pill"' in review_page
    assert 'id="run-sections-pill"' in review_page
    assert 'id="run-decision-pill"' in review_page
    assert 'id="run-progress-fill"' in review_page
    assert 'id="btn-shortcuts"' in review_page
    assert 'id="shortcut-overlay"' in review_page
    assert 'id="shortcut-close"' in review_page
    assert 'id="preview-mode-label"' in review_page
    assert 'id="filter-row"' in review_page
    assert 'id="facet-row"' in review_page
    assert 'id="decision-row"' in review_page
    assert "Type Filters" in review_page
    assert "Facet Filters" in review_page
    assert "Decision Filters" in review_page
    assert 'id="decision-summary"' in review_page
    assert 'id="nav-progress"' in review_page
    assert 'id="bulk-accept"' in review_page
    assert 'id="bulk-undo"' in review_page
    assert 'id="jump-index"' in review_page
    assert 'id="format-only-toggle"' in review_page
    assert 'id="next-pending-btn"' in review_page
    assert 'id="next-format-btn"' in review_page
    assert 'id="next-changed-btn"' in review_page
    assert 'id="next-undecided-btn"' in review_page
    assert 'id="next-undecided-note"' in review_page
    assert 'id="insp-subtitle"' in review_page
    assert "/decisions/batch" in review_page
    assert "Next Pending" in review_page
    assert "Next Fmt-only" in review_page
    assert "Next Changed" in review_page
    assert "Next Undecided" in review_page
    assert "Undo Last" in review_page
    assert "Fmt-only" in review_page
    assert "Format-only" in review_page
    assert "decision-state" in review_page
    assert "undoLastDecisionChange" in review_page
    assert "Ctrl/Cmd+K" in review_page
    assert "Ctrl/Cmd+Z" in review_page
    assert "detail-sec" in review_page
    assert "Section Details" in review_page
    assert "Formatting Deltas" in review_page
    assert "Formatting" in review_page
    assert "Indentation" in review_page
    assert 'id="btn-inline"' in review_page
    assert "Tri-pane" in review_page


def test_decisions_store_roundtrip(tmp_path: Path) -> None:
    app = BlacklineWebApp(tmp_path)
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)

    app._write_decisions(run_dir, {"2": "accept", "4": "reject"})

    stored = app._read_decisions(run_dir)
    assert stored == {"2": "accept", "4": "reject"}
