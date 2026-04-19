import base64
import json
from pathlib import Path
from fastapi.testclient import TestClient
import pytest

from blackline_tool.web import app, WORKSPACE_ROOT
import blackline_tool.web as web

client = TestClient(app)

@pytest.fixture
def clean_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "WORKSPACE_ROOT", tmp_path)
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    return tmp_path

def test_generate_outputs_can_force_html_preview(tmp_path: Path) -> None:
    from blackline_tool.runner import generate_outputs
    from blackline_tool.core import options_for_profile

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


def test_api_compare_persists_metadata_and_outputs(clean_workspace) -> None:
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

    response = client.post("/api/compare", json=payload)
    assert response.status_code == 200
    data = response.json()
    run_id = data["run_id"]
    
    run_dir = clean_workspace / "runs" / run_id
    stored = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))

    assert stored["profile_name"] == "contract"
    assert "html" in stored["downloads"]
    assert "json" in stored["downloads"]
    assert stored["preview_html"] == "studio_review.html"
    assert (run_dir / "outputs" / "studio_review.html").exists()
    assert (run_dir / "outputs" / "studio_review.json").exists()
    assert stored["summary"]["changed_sections"] == 1
    assert stored["sections"][0]["kind"] == "equal"
    assert stored["sections"][1]["kind"] == "replace"


def test_web_pages_expose_upload_and_review_ui(clean_workspace) -> None:
    # Index page
    response = client.get("/")
    assert response.status_code == 200
    index_page = response.text

    assert 'id="compare-form"' in index_page
    assert 'fetch("/api/compare"' in client.get("/static/index.js").text
    assert "Generate Review Run" in index_page
    assert 'id="mode-single"' in index_page
    assert 'id="mode-batch"' in index_page
    assert "Batch Queue" in index_page
    assert 'id="live-deck"' in index_page
    assert 'id="metric-mode"' in index_page
    assert 'id="batch-panel"' in index_page
    assert 'id="batch-open-select"' in index_page
    assert "blackline_batch_history_v1" in client.get("/static/index.js").text

    # Create a run to test review page
    payload = {
        "original_name": "a.txt", "original_content": base64.b64encode(b"a").decode("ascii"),
        "revised_name": "b.txt", "revised_content": base64.b64encode(b"b").decode("ascii"),
        "base_name": "test", "formats": ["json"]
    }
    run_id = client.post("/api/compare", json=payload).json()["run_id"]

    response = client.get(f"/runs/{run_id}")
    assert response.status_code == 200
    review_page = response.text

    assert "/api/runs/" in client.get("/static/v2.js").text
    assert 'id="stage"' in review_page
    assert 'id="doc"' in review_page
    assert 'id="batch-switcher"' in review_page
    assert 'id="btn-shortcuts"' in review_page
    assert 'id="filter-row"' in review_page
    assert 'id="decisions-count"' in review_page
    assert 'id="bulk-accept"' in review_page
    assert 'id="jump-index"' in review_page


def test_review_shell_v2_has_new_chrome(clean_workspace) -> None:
    payload = {
        "original_name": "a.txt", "original_content": base64.b64encode(b"a").decode("ascii"),
        "revised_name": "b.txt", "revised_content": base64.b64encode(b"b").decode("ascii"),
        "base_name": "test", "formats": ["json"]
    }
    run_id = client.post("/api/compare", json=payload).json()["run_id"]

    response = client.get(f"/runs/{run_id}")
    page = response.text

    assert "Inter+Tight" in page
    assert "JetBrains+Mono" in page
    assert 'class="top"' in page
    assert 'id="exportBtn"' in page
    assert 'id="stage"' in page
    assert 'id="inspector"' in page
    assert 'class="status"' in page


def test_v2_and_legacy_shells_diverge(clean_workspace) -> None:
    payload = {
        "original_name": "a.txt", "original_content": base64.b64encode(b"a").decode("ascii"),
        "revised_name": "b.txt", "revised_content": base64.b64encode(b"b").decode("ascii"),
        "base_name": "test", "formats": ["json"]
    }
    run_id = client.post("/api/compare", json=payload).json()["run_id"]

    v2 = client.get(f"/runs/{run_id}").text
    legacy = client.get(f"/runs/{run_id}?v=1").text
    
    assert "Inter+Tight" in v2
    assert "Inter+Tight" not in legacy


def test_run_metadata_includes_combined_tokens(clean_workspace) -> None:
    payload = {
        "original_name": "a.txt", "original_content": base64.b64encode(b"alpha").decode("ascii"),
        "revised_name": "b.txt", "revised_content": base64.b64encode(b"beta").decode("ascii"),
        "base_name": "tokens", "formats": ["json"]
    }
    run_id = client.post("/api/compare", json=payload).json()["run_id"]
    metadata = client.get(f"/api/runs/{run_id}").json()

    changed = [s for s in metadata["sections"] if s["kind"] != "equal"]
    assert changed
    tokens = changed[0].get("combined_tokens")
    assert isinstance(tokens, list) and tokens
    assert {t["kind"] for t in tokens}.issubset({"equal", "insert", "delete"})


def test_api_decisions_roundtrip(clean_workspace) -> None:
    payload = {
        "original_name": "a.txt", "original_content": base64.b64encode(b"a").decode("ascii"),
        "revised_name": "b.txt", "revised_content": base64.b64encode(b"b").decode("ascii"),
        "base_name": "test", "formats": ["json"]
    }
    run_id = client.post("/api/compare", json=payload).json()["run_id"]

    # Save decision
    # Section index is 1 for 'a' -> 'b'
    response = client.post(f"/api/runs/{run_id}/decisions", json={"section_index": 1, "decision": "accept"})
    assert response.status_code == 200
    assert response.json()["decisions"]["1"] == "accept"

    # Verify in metadata
    response = client.get(f"/api/runs/{run_id}")
    assert response.json()["decisions"]["1"] == "accept"

    # Batch decision
    response = client.post(f"/api/runs/{run_id}/decisions/batch", json={"section_indexes": [1], "decision": "reject"})
    assert response.status_code == 200
    assert response.json()["decisions"]["1"] == "reject"
