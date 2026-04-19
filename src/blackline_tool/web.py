from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import secrets
import shutil
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, List, Optional

from fastapi import FastAPI, Request, HTTPException, status, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from .cli import build_compare_options_from_settings, normalize_formats
from .core import (
    active_rule_labels, 
    report_profile_summary, 
    generate_report, 
    write_docx_report,
    options_for_profile
)
from .runner import generate_outputs

PROFILE_CHOICES = ("default", "legal", "contract", "litigation", "factum", "presentation")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

app = FastAPI(title="Blackline Studio")

# Global configuration (will be set in main)
WORKSPACE_ROOT: Path = Path(".blackline-web")

class ComparePayload(BaseModel):
    original_name: str
    original_content: str  # base64
    revised_name: str
    revised_content: str   # base64
    base_name: str
    profile: str = "default"
    formats: List[str] = ["html", "json"]
    strict_legal: bool = False
    ignore_case: bool = False
    ignore_whitespace: bool = False
    ignore_smart_punctuation: bool = False
    ignore_punctuation: bool = False
    ignore_numbering: bool = False
    detect_moves: bool = True

class DecisionPayload(BaseModel):
    section_index: int
    decision: str  # "accept", "reject", "pending"

class BatchDecisionPayload(BaseModel):
    section_indexes: List[int]
    decision: str

# Helpers
def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4)

def _metadata_path(run_dir: Path) -> Path:
    return run_dir / "metadata.json"

def _load_metadata(run_dir: Path) -> dict[str, Any] | None:
    path = _metadata_path(run_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def _save_metadata(run_dir: Path, data: dict[str, Any]) -> None:
    _metadata_path(run_dir).write_text(json.dumps(data, indent=2), encoding="utf-8")

def _decode_file_payload(value: str) -> bytes:
    try:
        return base64.b64decode(value)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 content")

def _safe_filename(name: str, default: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name if name else default

def _get_run_dir(run_id: str) -> Path:
    if not RUN_ID_PATTERN.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    run_dir = WORKSPACE_ROOT / "runs" / run_id
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    return run_dir

# Routes
@app.get("/", response_class=HTMLResponse)
async def index_page():
    template_path = Path(__file__).parent / "web" / "templates" / "index.html"
    content = template_path.read_text(encoding="utf-8")
    
    profile_options = "".join(f'<option value="{p}">{p.title()}</option>' for p in PROFILE_CHOICES)
    format_controls = "".join(
        f'<label class="check-pill"><input type="checkbox" name="formats" value="{f}" {"checked" if f in ("html", "json") else ""} /> <span>{f.upper()}</span></label>'
        for f in ("html", "docx", "pdf", "json")
    )
    
    content = content.replace("{{profile_options}}", profile_options)
    content = content.replace("{{format_controls}}", format_controls)
    return content

@app.post("/api/compare")
async def api_compare(payload: ComparePayload):
    run_id = _new_run_id()
    run_dir = WORKSPACE_ROOT / "runs" / run_id
    inputs_dir = run_dir / "inputs"
    outputs_dir = run_dir / "outputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    orig_path = inputs_dir / _safe_filename(payload.original_name, "original")
    rev_path = inputs_dir / _safe_filename(payload.revised_name, "revised")
    
    orig_path.write_bytes(_decode_file_payload(payload.original_content))
    rev_path.write_bytes(_decode_file_payload(payload.revised_content))

    options = build_compare_options_from_settings(
        profile=payload.profile,
        strict_legal=payload.strict_legal,
        ignore_case=payload.ignore_case,
        ignore_whitespace=payload.ignore_whitespace,
        ignore_smart_punctuation=payload.ignore_smart_punctuation,
        ignore_punctuation=payload.ignore_punctuation,
        ignore_numbering=payload.ignore_numbering,
        detect_moves=payload.detect_moves,
    )

    requested_formats = {f.lower().strip() for f in payload.formats}
    result = generate_outputs(
        orig_path,
        rev_path,
        outputs_dir,
        base_name=payload.base_name,
        formats=requested_formats,
        options=options,
        ensure_html_preview=True,
    )

    metadata = {
        "run_id": run_id,
        "created_at": _iso_utc_now(),
        "original_name": payload.original_name,
        "revised_name": payload.revised_name,
        "profile_name": result.report.options.profile_name,
        "profile_summary": report_profile_summary(result.report.options),
        "active_rules": active_rule_labels(result.report.options),
        "detect_moves": result.report.options.detect_moves,
        "requested_formats": sorted(requested_formats),
        "structure_kinds": result.report.structure_kinds,
        "summary": asdict(result.report.summary),
        "preview_html": result.preview_html.name,
        "downloads": {
            fmt: f"/api/runs/{run_id}/files/{path.name}"
            for fmt, path in result.files.items()
        },

        "decisions": {},
        "sections": [
            {
                "index": s.index,
                "label": s.label,
                "kind": s.kind,
                "kind_label": s.kind_label,
                "is_changed": s.is_changed,
                "block_kind": s.block_kind,
                "container": s.container,
                "location_kind": s.location_kind,
                "change_facets": s.change_facets,
                "format_change_facets": s.format_change_facets,
                "original_text": s.original_text,
                "revised_text": s.revised_text,
                "combined_tokens": [asdict(t) for t in s.combined_tokens],
            }
            for s in result.report.sections
        ],
    }
    _save_metadata(run_dir, metadata)

    return {"run_id": run_id, "run_url": f"/runs/{run_id}"}

@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def review_shell(run_id: str, v: Optional[str] = None):
    _get_run_dir(run_id) # Validate exists
    template_name = "review_v1.html" if v == "1" else "review_v2.html"
    template_path = Path(__file__).parent / "web" / "templates" / template_name
    content = template_path.read_text(encoding="utf-8")
    return content.replace("{{run_id}}", run_id)

@app.get("/api/runs/{run_id}")
async def api_get_run(run_id: str):
    run_dir = _get_run_dir(run_id)
    metadata = _load_metadata(run_dir)
    if not metadata:
        raise HTTPException(status_code=404, detail="Metadata missing")
    
    # Add preview URL
    metadata["preview_url"] = f"/api/runs/{run_id}/files/{metadata['preview_html']}"
    return metadata

@app.get("/api/runs/{run_id}/files/{filename}")
async def api_get_run_file(run_id: str, filename: str):
    run_dir = _get_run_dir(run_id)
    file_path = run_dir / "outputs" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

@app.post("/api/runs/{run_id}/decisions")
async def api_save_decision(run_id: str, payload: DecisionPayload):
    run_dir = _get_run_dir(run_id)
    metadata = _load_metadata(run_dir)
    if not metadata:
        raise HTTPException(status_code=404, detail="Metadata missing")
    
    decisions = metadata.get("decisions", {})
    if payload.decision == "pending":
        decisions.pop(str(payload.section_index), None)
    else:
        decisions[str(payload.section_index)] = payload.decision
    
    metadata["decisions"] = decisions
    _save_metadata(run_dir, metadata)
    return {"status": "ok", "decisions": decisions}

@app.post("/api/runs/{run_id}/decisions/batch")
async def api_save_batch_decisions(run_id: str, payload: BatchDecisionPayload):
    run_dir = _get_run_dir(run_id)
    metadata = _load_metadata(run_dir)
    if not metadata:
        raise HTTPException(status_code=404, detail="Metadata missing")
    
    decisions = metadata.get("decisions", {})
    updated_count = 0
    for idx in payload.section_indexes:
        if payload.decision == "pending":
            if str(idx) in decisions:
                decisions.pop(str(idx))
                updated_count += 1
        else:
            if decisions.get(str(idx)) != payload.decision:
                decisions[str(idx)] = payload.decision
                updated_count += 1
    
    metadata["decisions"] = decisions
    _save_metadata(run_dir, metadata)
    return {"status": "ok", "decisions": decisions, "updated": updated_count}

@app.get("/api/runs/{run_id}/export-clean")
async def api_export_clean(run_id: str):
    run_dir = _get_run_dir(run_id)
    metadata = _load_metadata(run_dir)
    if not metadata:
        raise HTTPException(status_code=404, detail="Metadata missing")
    
    inputs_dir = run_dir / "inputs"
    # Find original and revised paths
    orig_files = list(inputs_dir.glob("*"))
    if len(orig_files) < 2:
        raise HTTPException(status_code=400, detail="Original/Revised files missing in inputs")
    
    # Simple heuristic: original was written first
    sorted_files = sorted(orig_files, key=lambda p: p.stat().st_ctime)
    original_path = sorted_files[0]
    revised_path = sorted_files[1]

    options = options_for_profile(metadata.get("profile_name", "default"))
    report = generate_report(original_path, revised_path, options=options)
    
    export_path = run_dir / "outputs" / "Clean_Decided_Document.docx"
    write_docx_report(report, export_path, decisions=metadata.get("decisions", {}))
    
    return FileResponse(
        export_path, 
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="Clean_Decided_Document.docx"
    )

# Static files
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="blackline-web")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".blackline-web"),
        help="Directory to store web review data (default: .blackline-web)",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the web UI in the default browser",
    )
    return parser.parse_args(argv)

def main(argv: Sequence[str] | None = None) -> int:
    global WORKSPACE_ROOT
    args = parse_args(argv)
    WORKSPACE_ROOT = args.workspace
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_ROOT / "runs").mkdir(exist_ok=True)

    url = f"http://{args.host}:{args.port}"
    print(f"Starting Blackline Studio at {url}")
    print(f"Workspace: {WORKSPACE_ROOT.absolute()}")

    if not args.no_browser:
        webbrowser.open(url)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())
