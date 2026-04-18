from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import re
import secrets
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote, unquote, urlparse

from .cli import build_compare_options_from_settings, normalize_formats
from .core import _active_rule_labels, _report_profile_summary
from .runner import generate_outputs

PROFILE_CHOICES = ("default", "legal", "contract", "litigation", "factum", "presentation")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blackline-web",
        description="Run a local web UI for generating and reviewing blacklines.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path(".blackline-web"),
        help="Directory for uploaded files and generated runs",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the web UI in the default browser after the server starts",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    app = BlacklineWebApp(args.workspace)
    server = ThreadingHTTPServer((args.host, args.port), app.handler_class())
    url = f"http://{args.host}:{args.port}/"
    print(f"Blackline web UI running at {url}")
    print(f"Workspace: {app.workspace_root}")
    if args.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping blackline web UI.")
    finally:
        server.server_close()
    return 0


class BlacklineWebApp:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.runs_root = self.workspace_root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def handler_class(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                app.handle_get(self)

            def do_POST(self) -> None:  # noqa: N802
                app.handle_post(self)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

        return Handler

    def handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        path = parsed.path
        if path == "/":
            _send_html(handler, build_index_page())
            return

        parts = [segment for segment in path.split("/") if segment]
        if len(parts) == 2 and parts[0] == "runs":
            _send_html(handler, build_review_shell(parts[1]))
            return
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "runs":
            self._serve_run_metadata(handler, parts[2])
            return
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "preview":
            self._serve_preview(handler, parts[1])
            return
        if len(parts) >= 4 and parts[0] == "runs" and parts[2] == "downloads":
            filename = unquote("/".join(parts[3:]))
            self._serve_download(handler, parts[1], filename)
            return
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "runs" and parts[3] == "export-clean":
            self._serve_export_clean(handler, parts[2])
            return

        _send_error(handler, HTTPStatus.NOT_FOUND, "Not found")

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        parts = [segment for segment in parsed.path.split("/") if segment]
        if len(parts) == 2 and parts[0] == "api" and parts[1] == "compare":
            self._handle_compare(handler)
            return
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "runs" and parts[3] == "decisions":
            self._handle_decision(handler, parts[2])
            return
        if len(parts) == 5 and parts[0] == "api" and parts[1] == "runs" and parts[3] == "decisions" and parts[4] == "batch":
            self._handle_decision_batch(handler, parts[2])
            return
        _send_error(handler, HTTPStatus.NOT_FOUND, "Not found")

    def _read_decisions(self, run_dir: Path) -> dict[str, str]:
        decisions_path = run_dir / "decisions.json"
        if not decisions_path.exists():
            return {}
        data = json.loads(decisions_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    def _write_decisions(self, run_dir: Path, decisions: dict[str, str]) -> None:
        decisions_path = run_dir / "decisions.json"
        decisions_path.write_text(json.dumps(decisions, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    def _handle_decision(self, handler: BaseHTTPRequestHandler, run_id: str) -> None:
        try:
            payload = _read_json(handler)
            run_dir = self._resolve_run_dir(run_id)
            if not run_dir:
                _send_error(handler, HTTPStatus.NOT_FOUND, "Run not found")
                return

            section_index = payload.get("section_index")
            if not isinstance(section_index, int) or section_index < 1:
                _send_json(handler, {"error": "section_index must be a positive integer"}, status=HTTPStatus.BAD_REQUEST)
                return
            idx = str(section_index)
            decision = payload.get("decision")
            if decision not in ("accept", "reject", "pending"):
                _send_json(
                    handler,
                    {"error": "decision must be one of accept, reject, pending"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return

            decisions = self._read_decisions(run_dir)
            if decision == "pending":
                decisions.pop(idx, None)
            else:
                decisions[idx] = decision
            self._write_decisions(run_dir, decisions)

            _send_json(handler, {"status": "ok", "decisions": decisions})
        except Exception as exc:  # noqa: BLE001
            _send_json(handler, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_decision_batch(self, handler: BaseHTTPRequestHandler, run_id: str) -> None:
        try:
            payload = _read_json(handler)
            run_dir = self._resolve_run_dir(run_id)
            if not run_dir:
                _send_error(handler, HTTPStatus.NOT_FOUND, "Run not found")
                return

            decision = payload.get("decision")
            if decision not in ("accept", "reject", "pending"):
                _send_json(
                    handler,
                    {"error": "decision must be one of accept, reject, pending"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return

            raw_indexes = payload.get("section_indexes")
            if not isinstance(raw_indexes, list):
                _send_json(
                    handler,
                    {"error": "section_indexes must be a list of positive integers"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return

            section_indexes: list[int] = []
            for value in raw_indexes:
                if not isinstance(value, int) or value < 1:
                    _send_json(
                        handler,
                        {"error": "section_indexes must be a list of positive integers"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                section_indexes.append(value)

            decisions = self._read_decisions(run_dir)
            for index in section_indexes:
                idx = str(index)
                if decision == "pending":
                    decisions.pop(idx, None)
                else:
                    decisions[idx] = decision
            self._write_decisions(run_dir, decisions)
            _send_json(handler, {"status": "ok", "updated": len(section_indexes), "decisions": decisions})
        except Exception as exc:  # noqa: BLE001
            _send_json(handler, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _handle_compare(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            payload = _read_json(handler)
            result = create_review_run(self.workspace_root, payload)
            _send_json(
                handler,
                {
                    "run_id": result["run_id"],
                    "run_url": f"/runs/{result['run_id']}",
                },
                status=HTTPStatus.CREATED,
            )
        except Exception as exc:  # noqa: BLE001
            _send_json(handler, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def _serve_run_metadata(self, handler: BaseHTTPRequestHandler, run_id: str) -> None:
        metadata_path = _metadata_path(self._resolve_run_dir(run_id))
        if metadata_path is None or not metadata_path.exists():
            _send_error(handler, HTTPStatus.NOT_FOUND, "Run not found")
            return

        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["preview_url"] = f"/runs/{run_id}/preview"
        payload["downloads"] = {
            fmt: f"/runs/{run_id}/downloads/{quote(filename)}"
            for fmt, filename in payload.get("files", {}).items()
        }
        
        run_dir = self._resolve_run_dir(run_id)
        if run_dir is not None:
            payload["decisions"] = self._read_decisions(run_dir)
        else:
            payload["decisions"] = {}

        _send_json(handler, payload)

    def _serve_preview(self, handler: BaseHTTPRequestHandler, run_id: str) -> None:
        run_dir = self._resolve_run_dir(run_id)
        if run_dir is None:
            _send_error(handler, HTTPStatus.NOT_FOUND, "Run not found")
            return
        metadata = _load_metadata(run_dir)
        if metadata is None:
            _send_error(handler, HTTPStatus.NOT_FOUND, "Run not found")
            return
        preview_path = run_dir / "outputs" / metadata["preview_html"]
        _serve_file(handler, preview_path)

    def _serve_download(self, handler: BaseHTTPRequestHandler, run_id: str, filename: str) -> None:
        run_dir = self._resolve_run_dir(run_id)
        if run_dir is None:
            _send_error(handler, HTTPStatus.NOT_FOUND, "Run not found")
            return
        metadata = _load_metadata(run_dir)
        if metadata is None:
            _send_error(handler, HTTPStatus.NOT_FOUND, "Run not found")
            return
        if filename not in set(metadata.get("files", {}).values()):
            _send_error(handler, HTTPStatus.NOT_FOUND, "File not found")
            return
        _serve_file(handler, run_dir / "outputs" / filename, as_attachment=True)

    def _serve_export_clean(self, handler: BaseHTTPRequestHandler, run_id: str) -> None:
        try:
            run_dir = self._resolve_run_dir(run_id)
            if not run_dir:
                _send_error(handler, HTTPStatus.NOT_FOUND, "Run not found")
                return
            
            metadata_path = run_dir / "metadata.json"
            if not metadata_path.exists():
                _send_error(handler, HTTPStatus.NOT_FOUND, "Metadata not found")
                return
            
            import json
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            original_path = run_dir / "inputs" / metadata["original_name"]
            revised_path = run_dir / "inputs" / metadata["revised_name"]
            
            options = build_compare_options_from_settings(
                profile=metadata.get("profile_name", "default"),
                detect_moves=metadata.get("detect_moves", True)
            )
            
            from .core import generate_report, write_docx_report
            report = generate_report(original_path, revised_path, options=options)
            
            decisions_path = run_dir / "decisions.json"
            decisions = {}
            if decisions_path.exists():
                decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
                
            out_path = run_dir / "final_clean_export.docx"
            write_docx_report(report, out_path, template_path=None, decisions=decisions)
            
            content = out_path.read_bytes()
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            handler.send_header("Content-Disposition", 'attachment; filename="Clean_Decided_Document.docx"')
            handler.send_header("Content-Length", str(len(content)))
            handler.end_headers()
            handler.wfile.write(content)
        except Exception as exc:  # noqa: BLE001
            _send_error(handler, HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _resolve_run_dir(self, run_id: str) -> Path | None:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            return None
        run_dir = (self.runs_root / run_id).resolve()
        if self.runs_root not in run_dir.parents:
            return None
        return run_dir


def create_review_run(workspace_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    original_name = _safe_filename(payload.get("original_name"), default="original.txt")
    revised_name = _safe_filename(payload.get("revised_name"), default="revised.txt")
    original_bytes = _decode_file_payload(payload.get("original_content"))
    revised_bytes = _decode_file_payload(payload.get("revised_content"))
    requested_formats = normalize_formats(",".join(payload.get("formats") or ["html", "docx", "pdf", "json"]))
    if not requested_formats:
        requested_formats = {"html"}

    run_id = _new_run_id()
    run_dir = workspace_root / "runs" / run_id
    input_dir = run_dir / "inputs"
    output_dir = run_dir / "outputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    original_path = input_dir / original_name
    revised_path = input_dir / revised_name
    original_path.write_bytes(original_bytes)
    revised_path.write_bytes(revised_bytes)

    options = build_compare_options_from_settings(
        profile=payload.get("profile") or "default",
        strict_legal=bool(payload.get("strict_legal")),
        ignore_case=bool(payload.get("ignore_case")),
        ignore_whitespace=bool(payload.get("ignore_whitespace")),
        ignore_smart_punctuation=bool(payload.get("ignore_smart_punctuation")),
        ignore_punctuation=bool(payload.get("ignore_punctuation")),
        ignore_numbering=bool(payload.get("ignore_numbering")),
        detect_moves=bool(payload.get("detect_moves", True)),
    )
    base_name = _safe_base_name(payload.get("base_name") or "blackline_report")

    result = generate_outputs(
        original_path,
        revised_path,
        output_dir,
        base_name=base_name,
        formats=requested_formats,
        options=options,
        ensure_html_preview=True,
    )

    metadata = {
        "run_id": run_id,
        "created_at": _iso_utc_now(),
        "original_name": original_name,
        "revised_name": revised_name,
        "profile_name": result.report.options.profile_name,
        "profile_summary": _report_profile_summary(result.report.options),
        "active_rules": _active_rule_labels(result.report.options),
        "detect_moves": result.report.options.detect_moves,
        "requested_formats": sorted(requested_formats),
        "structure_kinds": result.report.structure_kinds,
        "summary": asdict(result.report.summary),
        "preview_html": result.preview_html.name,
        "files": {fmt: path.name for fmt, path in sorted(result.files.items())},
        "sections": [
            {
                "index": section.index,
                "label": section.label,
                "kind": section.kind,
                "kind_label": section.kind_label,
                "block_kind": section.block_kind,
                "container": section.container,
                "location_kind": section.location_kind,
                "change_facets": section.change_facets,
                "format_change_facets": section.format_change_facets,
                "is_changed": section.is_changed,
                "original_label": section.original_label,
                "revised_label": section.revised_label,
                "move_from_label": section.move_from_label,
                "move_to_label": section.move_to_label,
                "original_text": section.original_text,
                "revised_text": section.revised_text,
            }
            for section in result.report.sections
        ],
    }
    _metadata_path(run_dir).write_text(json.dumps(metadata, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return metadata


def build_index_page() -> str:
    profile_options = "".join(
        f'<option value="{html.escape(profile)}"{" selected" if profile == "contract" else ""}>{html.escape(profile.title())}</option>'
        for profile in PROFILE_CHOICES
    )
    format_controls = "".join(
        f"""
        <label class="check-pill">
          <input type="checkbox" name="formats" value="{fmt}" {"checked" if fmt in {"html", "docx", "pdf", "json"} else ""}/>
          <span>{fmt.upper()}</span>
        </label>
        """
        for fmt in ("html", "docx", "pdf", "json")
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blackline Studio</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    :root {{
      --ink: #111827;
      --muted: #6b7280;
      --border-soft: rgba(229, 231, 235, 0.8);
      --canvas: #f3f4f6;
      --surface: #ffffff;
      --primary: #1e3a8a;
      --primary-hover: #1e40af;
      --primary-soft: rgba(30, 58, 138, 0.05);
      --shadow-lg: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
      --font-sans: 'Inter', system-ui, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; min-height: 100vh;
      font-family: var(--font-sans); color: var(--ink);
      background: radial-gradient(circle at 10% 10%, rgba(30,58,138,0.06) 0%, transparent 40%), var(--canvas);
      display: flex; justify-content: center; padding: 4rem 1.5rem;
    }}
    .shell {{ width: 100%; max-width: 900px; display: flex; flex-direction: column; gap: 2rem; }}
    h1 {{ font-size: 2.5rem; font-weight: 700; margin: 0; text-align: center; letter-spacing: -0.02em; }}
    p.subtitle {{ color: var(--muted); text-align: center; font-size: 1.125rem; margin-top: 0.5rem; }}
    
    .card {{
      background: rgba(255, 255, 255, 0.85); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
      border: 1px solid var(--border-soft); border-radius: 20px;
      padding: 2.5rem; box-shadow: var(--shadow-lg);
    }}
    .section-title {{ font-size: 0.875rem; font-weight: 600; color: var(--muted); text-transform: uppercase; margin-bottom: 1rem; letter-spacing: 0.05em; }}
    
    .upload-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2rem; }}
    .upload-zone {{
      position: relative; border: 2px dashed var(--border-soft); border-radius: 16px;
      padding: 2rem 1.5rem; text-align: center; cursor: pointer; transition: all 0.2s;
      background: rgba(249, 250, 251, 0.5);
    }}
    .upload-zone:hover, .upload-zone.dragover {{ border-color: var(--primary); background: var(--primary-soft); }}
    .upload-zone.has-file {{ border-style: solid; border-color: var(--primary); background: var(--surface); }}
    .upload-input {{ position: absolute; inset: 0; opacity: 0; cursor: pointer; }}
    .icon {{ width: 48px; height: 48px; border-radius: 24px; background: var(--canvas); display: flex; align-items: center; justify-content: center; margin: 0 auto 1rem; color: var(--muted); font-weight: 600; }}
    .upload-zone.has-file .icon {{ background: var(--primary); color: white; }}
    .lbl {{ font-weight: 600; font-size: 1.125rem; }}
    .sub {{ color: var(--muted); font-size: 0.875rem; margin-top: 0.25rem; }}
    .fname {{ color: var(--primary); font-weight: 500; font-size: 0.875rem; margin-top: 0.5rem; display: none; }}
    .upload-zone.has-file .fname {{ display: block; }}
    
    .field-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2rem; }}
    .field label {{ display: block; font-size: 0.875rem; font-weight: 500; margin-bottom: 0.5rem; }}
    input[type="text"], select {{
      width: 100%; border-radius: 12px; border: 1px solid var(--border-soft); padding: 0.875rem 1rem;
      font-family: inherit; font-size: 1rem; transition: border-color 0.2s;
    }}
    input[type="text"]:focus, select:focus {{ outline: none; border-color: var(--primary); }}
    
    .pill-group {{ display: flex; flex-wrap: wrap; gap: 0.75rem; margin-bottom: 1.5rem; }}
    .check-pill {{
      display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.5rem 1rem; border-radius: 999px;
      border: 1px solid var(--border-soft); font-size: 0.875rem; cursor: pointer; background: var(--surface); transition: 0.2s;
    }}
    .check-pill:hover {{ background: var(--canvas); }}
    .check-pill input {{ accent-color: var(--primary); }}
    
    details {{ margin-bottom: 2rem; }}
    summary {{ color: var(--primary); font-weight: 500; font-size: 0.875rem; cursor: pointer; list-style: none; user-select: none; }}
    summary::-webkit-details-marker {{ display: none; }}
    
    .btn {{
      width: 100%; background: var(--primary); color: white; border: none; border-radius: 12px;
      padding: 1.25rem; font-size: 1.125rem; font-weight: 600; cursor: pointer; transition: 0.2s;
      box-shadow: 0 4px 6px rgba(30,58,138,0.2);
    }}
    .btn:hover {{ background: var(--primary-hover); transform: translateY(-2px); box-shadow: 0 8px 12px rgba(30,58,138,0.25); }}
    .btn:disabled {{ opacity: 0.7; cursor: wait; transform: none; }}
    #status {{ text-align: center; margin-top: 1rem; font-size: 0.875rem; color: var(--muted); }}
    #status.error {{ color: #dc2626; }}
  </style>
</head>
<body>
  <div class="shell">
    <header><h1>Blackline Studio</h1><p class="subtitle">Drag & drop versions to generate native DOCX redlines.</p></header>
    <div class="card">
      <form id="compare-form">
        <div class="section-title">Step 1: Upload Documents</div>
        <div class="upload-grid">
          <label class="upload-zone" id="z-original">
            <input class="upload-input" type="file" id="original" name="original" required />
            <div class="icon">O</div>
            <div class="lbl">Original Draft</div><div class="sub">Baseline (.docx, .txt)</div>
            <div class="fname" id="n-original">Selected</div>
          </label>
          <label class="upload-zone" id="z-revised">
            <input class="upload-input" type="file" id="revised" name="revised" required />
            <div class="icon">R</div>
            <div class="lbl">Revised Draft</div><div class="sub">Latest edits (.docx, .txt)</div>
            <div class="fname" id="n-revised">Selected</div>
          </label>
        </div>
        
        <div class="section-title">Step 2: Settings</div>
        <div class="field-grid">
          <div class="field"><label>Comparison Profile</label><select name="profile">{profile_options}</select></div>
          <div class="field"><label>Output Name</label><input type="text" name="base_name" value="blackline_report" /></div>
        </div>
        
        <div class="section-title">Step 3: Outputs & Rules</div>
        <div class="pill-group">{format_controls}</div>
        
        <details>
          <summary>+ Advanced Rules</summary>
          <div class="pill-group" style="padding-top:1rem;">
            <label class="check-pill"><input type="checkbox" name="strict_legal" /> <span>Strict Legal</span></label>
            <label class="check-pill"><input type="checkbox" name="ignore_case" /> <span>Ignore Case</span></label>
            <label class="check-pill"><input type="checkbox" name="ignore_whitespace" /> <span>Ignore Whitespace</span></label>
            <label class="check-pill"><input type="checkbox" name="ignore_smart_punctuation" /> <span>Smart Punctuation</span></label>
            <label class="check-pill"><input type="checkbox" name="ignore_punctuation" /> <span>Ignore Punctuation</span></label>
            <label class="check-pill"><input type="checkbox" name="ignore_numbering" /> <span>Ignore Numbering</span></label>
            <label class="check-pill"><input type="checkbox" name="detect_moves" checked /> <span>Detect Moves</span></label>
          </div>
        </details>
        
        <button class="btn" type="submit" id="submit-btn">Generate Review Run</button>
        <div id="status"></div>
      </form>
    </div>
  </div>
  <script>
    ['original', 'revised'].forEach(id => {{
      const inp = document.getElementById(id), z = document.getElementById('z-'+id), n = document.getElementById('n-'+id);
      const upd = () => {{ if(inp.files[0]) {{ z.classList.add('has-file'); n.textContent=inp.files[0].name; }} else z.classList.remove('has-file'); }};
      inp.addEventListener('change', upd);
      z.addEventListener('dragover', e => {{ e.preventDefault(); z.classList.add('dragover'); }});
      z.addEventListener('dragleave', e => {{ e.preventDefault(); z.classList.remove('dragover'); }});
      z.addEventListener('drop', e => {{ e.preventDefault(); z.classList.remove('dragover'); if(e.dataTransfer.files.length){{ inp.files = e.dataTransfer.files; upd(); }} }});
    }});
    
    async function fileToBase64(file) {{
      const buffer = await file.arrayBuffer();
      let binary = ""; const bytes = new Uint8Array(buffer);
      for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      return btoa(binary);
    }}
    
    const form = document.getElementById('compare-form'), status = document.getElementById('status'), btn = document.getElementById('submit-btn');
    form.addEventListener("submit", async (e) => {{
      e.preventDefault();
      status.className = ""; status.textContent = "Processing documents..."; btn.disabled = true;
      try {{
        const formData = new FormData(form), orig = formData.get("original"), rev = formData.get("revised");
        if (!orig.name || !rev.name) throw new Error("Select both files.");
        const formats = formData.getAll("formats"); if (!formats.length) throw new Error("Select output format.");
        const payload = {{
          original_name: orig.name, original_content: await fileToBase64(orig),
          revised_name: rev.name, revised_content: await fileToBase64(rev),
          base_name: formData.get("base_name") || "blackline_report", profile: formData.get("profile") || "default", formats,
          strict_legal: formData.get("strict_legal") === "on", ignore_case: formData.get("ignore_case") === "on",
          ignore_whitespace: formData.get("ignore_whitespace") === "on", ignore_smart_punctuation: formData.get("ignore_smart_punctuation") === "on",
          ignore_punctuation: formData.get("ignore_punctuation") === "on", ignore_numbering: formData.get("ignore_numbering") === "on",
          detect_moves: formData.get("detect_moves") === "on"
        }};
        const res = await fetch("/api/compare", {{ method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify(payload) }});
        const result = await res.json();
        if(!res.ok) throw new Error(result.error || "Failed.");
        window.location.assign(result.run_url);
      }} catch (err) {{
        status.className = "error"; status.textContent = err.message || String(err); btn.disabled = false;
      }}
    }});
  </script>
</body>
</html>
"""


def build_review_shell(run_id: str) -> str:
    escaped_run_id = html.escape(run_id)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Review Run {escaped_run_id}</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    :root {{
      --ink: #111827; --muted: #6b7280; --muted-light: #9ca3af;
      --surface: rgba(255, 255, 255, 0.85); --surface-solid: #ffffff;
      --canvas: #f3f4f6; --canvas-zen: #111827;
      --primary: #1e3a8a; --primary-hover: #1e40af;
      --focus-ring: rgba(30, 58, 138, 0.18);
      --accept-soft: rgba(16, 185, 129, 0.1);
      --reject-soft: rgba(239, 68, 68, 0.08);
      --shadow-float: 0 20px 25px -5px rgba(0,0,0,0.1), 0 10px 10px -5px rgba(0,0,0,0.04);
      --border-soft: rgba(229, 231, 235, 0.5);
      --font-sans: 'Inter', system-ui, sans-serif;
      --ins: #10b981; --del: #ef4444; --rep: #f59e0b; --mov: #3b82f6;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 0; width: 100vw; height: 100vh; overflow: hidden; font-family: var(--font-sans); background: var(--canvas); transition: 0.4s; }}
    body.zen-mode {{ background: var(--canvas-zen); }}
    
    .stage {{ position: absolute; top: 56px; left: 0; right: 0; bottom: 0; transition: top 0.4s; }}
    body.zen-mode .stage {{ top: 0; }}
    iframe {{ width: calc(100% - 28px); height: 100%; border: none; }}
    
    @keyframes slideUpFade {{
      0% {{ opacity: 0; transform: translateY(16px) scale(0.98); }}
      100% {{ opacity: 1; transform: translateY(0) scale(1); }}
    }}
    
    .slim-header {{
      position: absolute; top: 0; left: 0; right: 0; height: 56px; padding: 0 1rem;
      background: var(--surface); backdrop-filter: blur(24px); border-bottom: 1px solid var(--border-soft);
      display: flex; align-items: center; justify-content: space-between; z-index: 100; transition: 0.4s;
    }}
    body.zen-mode .slim-header {{ transform: translateY(-100%); }}
    .header-left, .header-right {{ display: flex; align-items: center; gap: 1rem; }}
    .icon-btn {{ width: 36px; height: 36px; border-radius: 8px; border: none; background: transparent; cursor: pointer; transition: 0.2s; }}
    .icon-btn:hover {{ background: rgba(0,0,0,0.05); }}
    .pill-btn {{ padding: 0.5rem 1rem; border-radius: 999px; font-weight: 500; font-size: 0.875rem; border: 1px solid var(--border-soft); background: var(--surface-solid); cursor: pointer; text-decoration: none; color: var(--ink); }}
    .pill-btn:hover {{ background: rgba(0,0,0,0.02); }}
    .primary-btn {{ padding: 0.5rem 1rem; border-radius: 999px; font-weight: 600; font-size: 0.875rem; background: var(--primary); color: white; border: none; cursor: pointer; text-decoration: none; }}
    .dl-pill {{ padding: 0.5rem 1rem; border-radius: 999px; font-weight: 600; font-size: 0.875rem; border: 1px solid var(--primary); color: var(--primary); background: var(--surface-solid); cursor: pointer; text-decoration: none; transition: 0.2s; box-shadow: 0 1px 2px rgba(0,0,0,0.05); }}
    .dl-pill:hover {{ background: rgba(30,58,138,0.05); }}
    .sec-pill {{
      margin-left: 0.5rem;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      font-size: 0.72rem;
      color: var(--muted);
      border: 1px solid var(--border-soft);
      background: var(--surface-solid);
    }}
    .nav-progress {{
      margin-left: 0.45rem;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      font-size: 0.72rem;
      color: #1e3a8a;
      border: 1px solid rgba(30, 58, 138, 0.22);
      background: rgba(30, 58, 138, 0.08);
    }}
    
    .floating-navigator {{
      position: absolute; top: 1rem; left: 1rem; bottom: 1rem; width: 340px;
      background: var(--surface); backdrop-filter: blur(24px); border: 1px solid var(--border-soft);
      border-radius: 20px; box-shadow: var(--shadow-float); display: flex; flex-direction: column;
      transition: 0.4s; z-index: 50;
    }}
    body.zen-mode .floating-navigator, body.nav-hidden .floating-navigator {{ transform: translateX(calc(-100% - 2rem)); opacity: 0; }}
    
    .nav-search {{ padding: 1rem; border-bottom: 1px solid var(--border-soft); }}
    .nav-search input {{ width: 100%; border-radius: 8px; border: 1px solid var(--border-soft); padding: 0.6rem; font-family: inherit; }}
    .jump-row {{ margin-top: 0.6rem; display: flex; gap: 0.5rem; align-items: center; }}
    .jump-row input {{
      width: 100%;
      border-radius: 8px;
      border: 1px solid var(--border-soft);
      padding: 0.5rem 0.6rem;
      font-family: inherit;
      font-size: 0.8rem;
    }}
    .jump-row button {{
      border-radius: 8px;
      border: 1px solid var(--border-soft);
      background: var(--surface-solid);
      padding: 0.48rem 0.7rem;
      cursor: pointer;
      font-size: 0.76rem;
      font-weight: 600;
      white-space: nowrap;
    }}
    
    /* Distribution Bar */
    .dist-bar {{ display: flex; height: 6px; border-radius: 3px; overflow: hidden; margin-top: 0.5rem; }}
    .dist-segment {{ height: 100%; }}
    .dist-ins {{ background: var(--ins); }} .dist-del {{ background: var(--del); }}
    .dist-rep {{ background: var(--rep); }} .dist-mov {{ background: var(--mov); }} .dist-unc {{ background: #e5e7eb; }}
    .quick-row {{ margin-top: 0.6rem; display: flex; align-items: center; gap: 0.5rem; }}
    .quick-btn {{
      border-radius: 999px;
      border: 1px solid var(--border-soft);
      background: var(--surface-solid);
      padding: 0.28rem 0.62rem;
      font-size: 0.72rem;
      font-weight: 700;
      cursor: pointer;
      white-space: nowrap;
    }}
    .quick-btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    .quick-btn.active {{ background: #0f766e; border-color: #0f766e; color: #fff; }}
    .quick-btn.subtle {{ font-weight: 600; }}
    .quick-count {{ font-size: 0.72rem; color: var(--muted); }}
    
    .filter-group {{ border-bottom: 1px solid var(--border-soft); }}
    .filter-group:last-of-type {{ border-bottom: 1px solid var(--border-soft); }}
    .filter-label {{
      padding: 0.42rem 1rem 0.14rem;
      font-size: 0.65rem;
      font-weight: 700;
      letter-spacing: 0.07em;
      color: var(--muted-light);
      text-transform: uppercase;
    }}
    .filters-scroll {{ padding: 0.45rem 1rem 0.68rem; display: flex; gap: 0.4rem; overflow-x: auto; scrollbar-width: none; }}
    .filter-btn, .facet-filter-btn, .decision-filter-btn {{
      border-radius: 999px;
      border: 1px solid var(--border-soft);
      background: var(--surface-solid);
      cursor: pointer;
      white-space: nowrap;
      transition: background 0.18s ease, border-color 0.18s ease, color 0.18s ease, box-shadow 0.18s ease;
    }}
    .filter-btn {{ padding: 0.3rem 0.6rem; font-size: 0.75rem; }}
    .facet-filter-btn, .decision-filter-btn {{ padding: 0.28rem 0.55rem; font-size: 0.72rem; }}
    .filter-btn:hover, .facet-filter-btn:hover, .decision-filter-btn:hover {{ border-color: rgba(107, 114, 128, 0.38); }}
    .filter-btn:focus-visible, .facet-filter-btn:focus-visible, .decision-filter-btn:focus-visible {{
      outline: none;
      box-shadow: 0 0 0 3px var(--focus-ring);
    }}
    .filter-btn.active {{ background: var(--ink); color: white; box-shadow: 0 2px 8px rgba(17, 24, 39, 0.2); }}
    .facet-filter-btn.active {{ background: #0f766e; color: white; border-color: #0f766e; box-shadow: 0 2px 8px rgba(15, 118, 110, 0.25); }}
    .decision-filter-btn.active {{ background: #1e3a8a; color: white; border-color: #1e3a8a; box-shadow: 0 2px 8px rgba(30, 58, 138, 0.24); }}
    .bulk-row {{
      padding: 0.65rem 1rem;
      border-bottom: 1px solid var(--border-soft);
      display: flex;
      gap: 0.45rem;
      align-items: center;
      flex-wrap: wrap;
    }}
    .bulk-btn {{
      border-radius: 8px;
      border: 1px solid var(--border-soft);
      background: var(--surface-solid);
      padding: 0.38rem 0.58rem;
      font-size: 0.72rem;
      font-weight: 600;
      cursor: pointer;
    }}
    .bulk-btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    .bulk-btn:hover {{ background: rgba(0,0,0,0.03); }}
    .bulk-status {{
      margin-left: auto;
      font-size: 0.72rem;
      color: var(--muted);
      min-height: 1rem;
    }}
    .bulk-status.error {{ color: #b91c1c; }}
    .decision-guide {{
      padding: 0.5rem 1rem 0.62rem;
      border-bottom: 1px solid var(--border-soft);
      display: flex;
      gap: 0.45rem;
      align-items: center;
      flex-wrap: wrap;
    }}
    .decision-guide-note {{
      font-size: 0.71rem;
      color: var(--muted);
      line-height: 1.2;
    }}
    .decision-summary {{
      margin-left: 0.5rem;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      font-size: 0.72rem;
      color: #0f766e;
      border: 1px solid rgba(15, 118, 110, 0.25);
      background: rgba(16, 185, 129, 0.08);
    }}
    
    .change-list {{ flex: 1; overflow-y: auto; padding: 0.6rem 0.55rem 0.8rem; scroll-behavior: smooth; }}
    .detail-card {{
      padding: 0.78rem;
      border-radius: 13px;
      margin-bottom: 0.38rem;
      cursor: pointer;
      transition: 0.2s cubic-bezier(0.2, 0.8, 0.2, 1);
      border: 1px solid transparent;
      border-left: 4px solid transparent;
      background: rgba(255,255,255,0.72);
      animation: slideUpFade 0.5s cubic-bezier(0.2, 0.8, 0.2, 1) backwards;
    }}
    .detail-card:hover {{ background: rgba(255,255,255,0.92); border-color: rgba(148, 163, 184, 0.24); transform: translateX(2px); }}
    .detail-card.active {{
      background: linear-gradient(135deg, #ffffff 0%, #f8fafc 100%);
      border-left-color: var(--primary);
      border-color: rgba(30, 58, 138, 0.26);
      box-shadow: 0 8px 20px rgba(15, 23, 42, 0.1);
      transform: translateX(3px);
      z-index: 10;
    }}
    .detail-card.decision-accept {{ border-right: 4px solid var(--ins); background: linear-gradient(135deg, var(--accept-soft) 0%, rgba(255,255,255,0.92) 48%); }}
    .detail-card.decision-reject {{ border-right: 4px solid var(--del); background: linear-gradient(135deg, var(--reject-soft) 0%, rgba(255,255,255,0.92) 48%); }}
    .detail-title {{ font-size: 0.84rem; font-weight: 600; color: #111827; line-height: 1.28; margin-bottom: 0.36rem; }}
    .detail-meta {{ display: flex; align-items: center; gap: 0.35rem; flex-wrap: wrap; font-size: 0.7rem; color: var(--muted-light); margin-bottom: 0.45rem; text-transform: uppercase; letter-spacing: 0.03em; }}
    .detail-kind {{ color: #6b7280; font-weight: 700; }}
    .detail-dot {{ color: #cbd5e1; }}
    .detail-sec {{
      display: inline-block;
      padding: 0.08rem 0.34rem;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.38);
      background: rgba(255, 255, 255, 0.9);
      color: #475569;
      font-weight: 700;
    }}
    .detail-excerpt {{
      font-size: 0.79rem;
      color: #4b5563;
      line-height: 1.32;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      min-height: 2.1em;
    }}
    .facet-badges {{ margin-top: 0.4rem; display: flex; flex-wrap: wrap; gap: 0.3rem; }}
    .facet-badge {{
      display: inline-block;
      padding: 0.08rem 0.36rem;
      border-radius: 999px;
      font-size: 0.62rem;
      font-weight: 700;
      letter-spacing: 0.02em;
      border: 1px solid var(--border-soft);
      color: #374151;
      background: #f8fafc;
      text-transform: uppercase;
    }}
    .facet-badge.format-only {{ color: #0f766e; border-color: rgba(15, 118, 110, 0.28); background: rgba(16, 185, 129, 0.12); }}
    .decision-tag {{
      display: inline-block;
      padding: 0.11rem 0.45rem;
      border-radius: 999px;
      font-size: 0.62rem;
      font-weight: 700;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      border: 1px solid transparent;
    }}
    .decision-tag.pending {{ color: var(--muted); border-color: var(--border-soft); background: #f8fafc; }}
    .decision-tag.accept {{ color: #0f766e; border-color: rgba(15, 118, 110, 0.25); background: rgba(16, 185, 129, 0.08); }}
    .decision-tag.reject {{ color: #b91c1c; border-color: rgba(185, 28, 28, 0.25); background: rgba(239, 68, 68, 0.08); }}
    .decision-state {{
      display: inline-block;
      padding: 0.11rem 0.45rem;
      border-radius: 999px;
      font-size: 0.61rem;
      font-weight: 700;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      border: 1px solid transparent;
    }}
    .decision-state.saving {{ color: #92400e; border-color: rgba(146, 64, 14, 0.25); background: rgba(245, 158, 11, 0.12); }}
    .decision-state.saved {{ color: #0f766e; border-color: rgba(15, 118, 110, 0.25); background: rgba(16, 185, 129, 0.12); }}
    .decision-state.error {{ color: #b91c1c; border-color: rgba(185, 28, 28, 0.25); background: rgba(239, 68, 68, 0.12); }}
    
    .floating-inspector {{
      position: absolute; bottom: 2rem; right: 2rem; width: 465px; max-height: 56vh;
      background: var(--surface); backdrop-filter: blur(24px); border: 1px solid var(--border-soft);
      border-radius: 20px; box-shadow: var(--shadow-float); display: flex; flex-direction: column;
      z-index: 60; transform: translateY(20px); opacity: 0; pointer-events: none; transition: 0.3s;
    }}
    .floating-inspector.visible {{ transform: translateY(0); opacity: 1; pointer-events: auto; }}
    body.zen-mode .floating-inspector {{ transform: translateY(20px)!important; opacity: 0!important; pointer-events: none!important; }}
    
    .insp-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 0.75rem; padding: 0.92rem 1rem; border-bottom: 1px solid var(--border-soft); }}
    .insp-head h3 {{ margin: 0; font-size: 0.9rem; }}
    .insp-subtitle {{ margin-top: 0.22rem; font-size: 0.68rem; letter-spacing: 0.03em; text-transform: uppercase; color: var(--muted-light); font-weight: 700; }}
    .insp-body {{ padding: 0.95rem 1rem 1.05rem; overflow-y: auto; }}
    .insp-label {{ font-size: 0.74rem; color: var(--muted); margin-bottom: 0.7rem; }}
    .diff-block {{ background: var(--surface-solid); border: 1px solid var(--border-soft); border-radius: 12px; margin-bottom: 1rem; }}
    .diff-hdr {{ padding: 0.5rem 0.75rem; background: rgba(0,0,0,0.02); font-size: 0.7rem; font-weight: 600; color: var(--muted); text-transform: uppercase; border-bottom: 1px solid var(--border-soft); }}
    .diff-content {{ padding: 0.75rem; font-size: 0.84rem; line-height: 1.42; white-space: pre-wrap; }}
    .insp-facets {{ margin-bottom: 0.84rem; display: flex; flex-wrap: wrap; gap: 0.35rem; }}
    
    .zen-exit {{ position: absolute; top: 1rem; left: 50%; transform: translateX(-50%); padding: 0.5rem 1rem; border-radius: 999px; background: rgba(255,255,255,0.1); backdrop-filter: blur(8px); border: 1px solid rgba(255,255,255,0.2); color: white; cursor: pointer; z-index: 200; display: none; opacity: 0; transition: 0.3s; font-size: 0.8rem; }}
    body.zen-mode .zen-exit {{ display: block; opacity: 1; }}
    .zen-exit:hover {{ background: rgba(255,255,255,0.2); }}

    /* Keyboard Shortcuts Overlay */
    .kbd-hints {{ position: absolute; bottom: 1rem; left: 50%; transform: translateX(-50%); display: flex; gap: 1rem; background: var(--surface); backdrop-filter: blur(24px); border: 1px solid var(--border-soft); padding: 0.5rem 1rem; border-radius: 999px; z-index: 100; box-shadow: var(--shadow-float); transition: 0.4s; }}
    body.zen-mode .kbd-hints {{ opacity: 0; pointer-events: none; }}
    .kbd-hint {{ font-size: 0.75rem; color: var(--muted); display: flex; align-items: center; gap: 0.4rem; }}
    kbd {{ background: var(--surface-solid); border: 1px solid var(--border-soft); border-radius: 4px; padding: 0.1rem 0.4rem; font-family: monospace; font-weight: 600; color: var(--ink); }}
    
    .minimap {{
      position: absolute; right: 0; top: 0; bottom: 0; width: 28px;
      background: rgba(255,255,255,0.6); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
      border-left: 1px solid var(--border-soft); z-index: 50; cursor: crosshair;
      transition: transform 0.4s;
    }}
    body.zen-mode .minimap {{ transform: translateX(100%); }}
    body.nav-hidden .minimap {{ transform: translateX(100%); }}
    .minimap-tick {{
      position: absolute; width: 100%; height: 3px; left: 0; opacity: 0.6;
      transition: transform 0.1s, opacity 0.2s;
    }}
    .minimap-tick:hover {{ opacity: 1; transform: scaleY(3); }}
    .minimap-tick.ins {{ background: var(--ins); }}
    .minimap-tick.del {{ background: var(--del); }}
    .minimap-tick.replace {{ background: var(--rep); }}
    .minimap-tick.move {{ background: var(--mov); }}
  </style>
</head>
<body>
  <header class="slim-header" id="header">
    <div class="header-left">
      <button id="btn-nav" class="icon-btn">☰</button>
      <div style="font-size:0.9rem; font-weight:500;">Review Run <span style="color:#6b7280; margin-left:0.2rem">/ <span id="r-title">...</span></span><span id="sec-pill" class="sec-pill">sec -</span><span id="nav-progress" class="nav-progress">0/0 visible</span><span id="decision-summary" class="decision-summary">0/0 decided</span></div>
    </div>
    <div class="header-right">
      <button id="btn-export" class="primary-btn" style="background:#10b981;">Export Final Doc</button>
      <div id="dl-group" style="display:flex; gap:0.5rem; margin-right:0.5rem;"></div>
      <button id="btn-split" class="pill-btn">View: Inline</button>
      <button id="btn-zen" class="primary-btn">Zen Mode</button>
    </div>
  </header>

  <main class="stage">
    <iframe id="frame"></iframe>
    <div class="minimap" id="minimap"></div>
    <aside class="floating-navigator">
      <div class="nav-search">
        <input id="search" type="search" placeholder="Search changes... (/)" />
        <div class="jump-row">
          <input id="jump-index" type="number" min="1" step="1" placeholder="Go to section #"/>
          <button id="jump-btn">Go</button>
        </div>
        <div class="dist-bar" id="dist-bar"></div>
        <div class="quick-row">
          <button id="format-only-toggle" class="quick-btn">Formatting-only</button>
          <span id="format-only-count" class="quick-count">0/0 fmt-only</span>
        </div>
        <div class="quick-row">
          <button id="next-pending-btn" class="quick-btn subtle">Next Pending (N)</button>
          <button id="next-format-btn" class="quick-btn subtle">Next Fmt-only (M)</button>
          <button id="next-changed-btn" class="quick-btn subtle">Next Changed (C)</button>
        </div>
      </div>
      <div class="filter-group">
        <div class="filter-label">Type Filters</div>
        <div id="filter-row" class="filters-scroll"></div>
      </div>
      <div class="filter-group">
        <div class="filter-label">Facet Filters</div>
        <div id="facet-row" class="filters-scroll"></div>
      </div>
      <div class="filter-group">
        <div class="filter-label">Decision Filters</div>
        <div id="decision-row" class="filters-scroll"></div>
      </div>
      <div class="bulk-row">
        <button id="bulk-accept" class="bulk-btn">Accept Visible</button>
        <button id="bulk-reject" class="bulk-btn">Reject Visible</button>
        <button id="bulk-clear" class="bulk-btn">Clear Visible</button>
        <button id="bulk-undo" class="bulk-btn">Undo Last</button>
        <span id="bulk-status" class="bulk-status"></span>
      </div>
      <div class="decision-guide">
        <button id="next-undecided-btn" class="quick-btn subtle">Next Undecided</button>
        <span id="next-undecided-note" class="decision-guide-note">Pending guidance unavailable.</span>
      </div>
      <div id="detail-list" class="change-list"></div>
    </aside>
    <div class="floating-inspector" id="inspector">
      <div class="insp-head"><div><h3 id="insp-title">Change</h3><div id="insp-subtitle" class="insp-subtitle">Section Details</div></div><button id="close-insp" class="icon-btn" style="width:24px;height:24px;">✕</button></div>
      <div id="insp-body" class="insp-body"></div>
    </div>
    <button id="btn-exit-zen" class="zen-exit">Exit Zen Mode (Esc)</button>
    <div class="kbd-hints">
      <div class="kbd-hint"><kbd>J</kbd> / <kbd>K</kbd> Prev/Next</div>
      <div class="kbd-hint"><kbd>A</kbd> Accept</div>
      <div class="kbd-hint"><kbd>R</kbd> Reject</div>
      <div class="kbd-hint"><kbd>U</kbd> Clear</div>
      <div class="kbd-hint"><kbd>N</kbd> Next Pending</div>
      <div class="kbd-hint"><kbd>M</kbd> Next Fmt-only</div>
      <div class="kbd-hint"><kbd>C</kbd> Next Changed</div>
      <div class="kbd-hint"><kbd>Ctrl/Cmd+Z</kbd> Undo Last</div>
      <div class="kbd-hint"><kbd>F</kbd> Fmt-only</div>
      <div class="kbd-hint"><kbd>Ctrl/Cmd+K</kbd> Search</div>
      <div class="kbd-hint"><kbd>S</kbd> Cycle View</div>
      <div class="kbd-hint"><kbd>/</kbd> Search</div>
      <div class="kbd-hint"><kbd>G</kbd> Go to #</div>
      <div class="kbd-hint"><kbd>Z</kbd> Zen</div>
      <div class="kbd-hint"><kbd>B</kbd> Nav</div>
    </div>
  </main>
  
  <script>
    const runId = {json.dumps(run_id)};
    const s = {{
      meta: null,
      kindFilter: "changed",
      facetFilters: new Set(),
      decisionFilter: "any",
      formatOnlyFilter: false,
      q: "",
      sel: null,
      navOff: false,
      zen: false,
      insp: false,
      viewMode: "inline",
      iframe: null,
      syncLockUntil: 0,
      unbindFrameScroll: null,
      decisionStatusByIndex: {{}},
      decisionStatusTimers: {{}},
      decisionUndoStack: [],
      decisionBusy: false,
    }};
    const VIEW_ORDER = ["inline", "split", "tri"];
    const VIEW_LABELS = {{ inline: "Inline", split: "Split", tri: "Tri-pane" }};
    const DECISION_STATE_LABELS = {{ saving: "Saving", saved: "Saved", error: "Error" }};
    const TEXTUAL_FACETS = new Set(["content", "numbering", "capitalization", "punctuation", "whitespace"]);
    const FORMAT_FACETS = new Set(["formatting", "style", "alignment", "layout", "indentation", "spacing", "pagination"]);
    const FACET_ORDER = ["content", "formatting", "style", "alignment", "layout", "indentation", "spacing", "pagination", "numbering", "capitalization", "punctuation", "whitespace", "header", "footer", "table", "textbox", "footnote", "endnote"];
    const FACET_LABELS = {{
      content: "Content",
      formatting: "Formatting",
      style: "Style",
      alignment: "Alignment",
      layout: "Layout",
      indentation: "Indentation",
      spacing: "Spacing",
      pagination: "Pagination",
      numbering: "Numbering",
      capitalization: "Capitalization",
      punctuation: "Punctuation",
      whitespace: "Whitespace",
      header: "Header",
      footer: "Footer",
      table: "Table",
      textbox: "Text Box",
      footnote: "Footnote",
      endnote: "Endnote",
    }};
    const D = document;
    const body = D.body, frame = D.getElementById("frame"), nList = D.getElementById("detail-list");
    const insp = D.getElementById("inspector"), filterRow = D.getElementById("filter-row"), facetRow = D.getElementById("facet-row"), decisionRow = D.getElementById("decision-row"), search = D.getElementById("search");
    const secPill = D.getElementById("sec-pill");
    const decisionSummary = D.getElementById("decision-summary");
    const jumpInput = D.getElementById("jump-index");
    const jumpBtn = D.getElementById("jump-btn");
    const bulkStatus = D.getElementById("bulk-status");
    const bulkAcceptBtn = D.getElementById("bulk-accept");
    const bulkRejectBtn = D.getElementById("bulk-reject");
    const bulkClearBtn = D.getElementById("bulk-clear");
    const bulkUndoBtn = D.getElementById("bulk-undo");
    const formatOnlyToggle = D.getElementById("format-only-toggle");
    const formatOnlyCount = D.getElementById("format-only-count");
    const navProgress = D.getElementById("nav-progress");
    const nextPendingBtn = D.getElementById("next-pending-btn");
    const nextFormatBtn = D.getElementById("next-format-btn");
    const nextChangedBtn = D.getElementById("next-changed-btn");
    const nextUndecidedBtn = D.getElementById("next-undecided-btn");
    const nextUndecidedNote = D.getElementById("next-undecided-note");

    function sectionFacets(sec) {{
      return Array.isArray(sec.change_facets) ? sec.change_facets : [];
    }}

    function sectionFormatFacets(sec) {{
      if (Array.isArray(sec.format_change_facets) && sec.format_change_facets.length) {{
        return sec.format_change_facets;
      }}
      return sectionFacets(sec).filter(facet => FORMAT_FACETS.has(facet));
    }}

    function isFormattingOnlySection(sec) {{
      if (!sec || !sec.is_changed) return false;
      const facets = new Set(sectionFacets(sec));
      if (!facets.has("formatting")) return false;
      for (const facet of TEXTUAL_FACETS) {{
        if (facets.has(facet)) return false;
      }}
      return true;
    }}

    function sectionMatchesKind(sec) {{
      if (s.kindFilter === "all") return true;
      if (s.kindFilter === "changed") return !!sec.is_changed;
      return sec.kind === s.kindFilter;
    }}

    function sectionMatchesFacets(sec) {{
      if (!s.facetFilters.size) return true;
      const facets = new Set(sectionFacets(sec));
      for (const facet of s.facetFilters) {{
        if (!facets.has(facet)) return false;
      }}
      return true;
    }}

    function decisionForSection(sec) {{
      if (!s.meta) return "pending";
      const decisions = s.meta.decisions || {{}};
      const val = decisions[String(sec.index)];
      if (val === "accept" || val === "reject") return val;
      return "pending";
    }}

    function decisionSaveStateForIndex(idx) {{
      const state = s.decisionStatusByIndex[String(idx)];
      if (state === "saving" || state === "saved" || state === "error") return state;
      return null;
    }}

    function clearDecisionSaveTimer(idx) {{
      const key = String(idx);
      const timer = s.decisionStatusTimers[key];
      if (timer) {{
        clearTimeout(timer);
        delete s.decisionStatusTimers[key];
      }}
    }}

    function setDecisionSaveState(indexes, state, options = {{}}) {{
      const unique = Array.from(new Set(indexes || []));
      unique.forEach(idx => {{
        if (!Number.isInteger(idx)) return;
        clearDecisionSaveTimer(idx);
        if (!state) {{
          delete s.decisionStatusByIndex[String(idx)];
          return;
        }}
        s.decisionStatusByIndex[String(idx)] = state;
        if (state === "saved" && options.autoClear) {{
          const timer = setTimeout(() => {{
            delete s.decisionStatusByIndex[String(idx)];
            delete s.decisionStatusTimers[String(idx)];
            renderSections();
          }}, options.clearDelayMs || 1400);
          s.decisionStatusTimers[String(idx)] = timer;
        }}
      }});
    }}

    function decisionStatusBadgeMarkup(idx) {{
      const state = decisionSaveStateForIndex(idx);
      if (!state) return "";
      const label = DECISION_STATE_LABELS[state] || state;
      return ` <span class="decision-state ${{state}}">${{label}}</span>`;
    }}

    function setDecisionBusy(busy) {{
      s.decisionBusy = !!busy;
      [bulkAcceptBtn, bulkRejectBtn, bulkClearBtn, nextUndecidedBtn].forEach(btn => {{
        if (!btn) return;
        btn.disabled = s.decisionBusy;
      }});
    }}

    function updateUndoUi() {{
      if (!bulkUndoBtn) return;
      bulkUndoBtn.disabled = s.decisionBusy || s.decisionUndoStack.length === 0;
      bulkUndoBtn.textContent = s.decisionUndoStack.length ? `Undo Last (${{s.decisionUndoStack.length}})` : "Undo Last";
    }}

    function pushDecisionUndo(entries) {{
      const normalized = entries
        .filter(entry => entry && Number.isInteger(entry.idx))
        .filter(entry => entry.previous !== entry.next);
      if (!normalized.length) return;
      s.decisionUndoStack.push({{entries: normalized}});
      if (s.decisionUndoStack.length > 25) {{
        s.decisionUndoStack.shift();
      }}
      updateUndoUi();
    }}

    function sectionMatchesDecision(sec) {{
      if (s.decisionFilter === "any") return true;
      if (!sec.is_changed) return false;
      return decisionForSection(sec) === s.decisionFilter;
    }}

    function sectionMatchesNonFormatFilters(sec) {{
      return sectionMatchesKind(sec) && sectionMatchesFacets(sec) && sectionMatchesDecision(sec);
    }}

    function sectionMatchesFormatOnly(sec) {{
      if (!s.formatOnlyFilter) return true;
      return isFormattingOnlySection(sec);
    }}

    function sectionMatchesFilters(sec) {{
      return sectionMatchesNonFormatFilters(sec) && sectionMatchesFormatOnly(sec);
    }}

    function updateSectionPill() {{
      secPill.textContent = s.sel ? `sec ${{s.sel}}` : "sec -";
    }}

    function updateNavProgress() {{
      const sections = fSec();
      if (!sections.length) {{
        navProgress.textContent = "0/0 visible";
        return;
      }}
      const changedVisible = sections.filter(sec => sec.is_changed).length;
      const selIndex = sections.findIndex(sec => sec.index === s.sel);
      const currentVisible = selIndex >= 0 ? selIndex + 1 : 1;
      navProgress.textContent = `${{currentVisible}}/${{sections.length}} visible · ${{changedVisible}} changed`;
    }}

    function pendingVisibleSections() {{
      return fSec().filter(sec => sec.is_changed && decisionForSection(sec) === "pending");
    }}

    function nextItemFromList(items) {{
      if (!items.length) return null;
      const current = items.findIndex(sec => sec.index === s.sel);
      if (current < 0 || current >= items.length - 1) return items[0];
      return items[current + 1];
    }}

    function updateNextUndecidedGuide() {{
      if (!nextUndecidedBtn || !nextUndecidedNote) return;
      const pending = pendingVisibleSections();
      const next = nextItemFromList(pending);
      nextUndecidedBtn.disabled = s.decisionBusy || !pending.length;
      if (!pending.length) {{
        nextUndecidedNote.textContent = "All changed sections in scope are decided.";
        return;
      }}
      nextUndecidedNote.textContent = `${{pending.length}} pending in scope · next sec ${{next.index}}.`;
    }}

    function showBulkStatus(message, isError = false) {{
      bulkStatus.textContent = message || "";
      bulkStatus.classList.toggle("error", !!isError);
    }}

    function updateFormatOnlyUi() {{
      if (!s.meta) {{
        formatOnlyCount.textContent = "0/0 fmt-only";
        formatOnlyToggle.classList.remove("active");
        return;
      }}
      const allSections = s.meta.sections || [];
      const totalFmtOnly = allSections.filter(sec => sec.is_changed && isFormattingOnlySection(sec)).length;
      const visibleFmtOnly = allSections.filter(
        sec => sec.is_changed && isFormattingOnlySection(sec) && sectionMatchesNonFormatFilters(sec)
      ).length;
      formatOnlyCount.textContent = `${{visibleFmtOnly}}/${{totalFmtOnly}} fmt-only`;
      formatOnlyToggle.classList.toggle("active", s.formatOnlyFilter);
    }}

    function toggleFormatOnlyFilter() {{
      s.formatOnlyFilter = !s.formatOnlyFilter;
      updateFormatOnlyUi();
      renderSections();
    }}

    function sectionBadgeMarkup(sec) {{
      const badges = [];
      if (isFormattingOnlySection(sec)) {{
        badges.push('<span class="facet-badge format-only">FMT-only</span>');
      }}
      const formatFacets = sectionFormatFacets(sec).filter(facet => facet !== "formatting");
      formatFacets.slice(0, 3).forEach(facet => {{
        badges.push(`<span class="facet-badge">${{enc(FACET_LABELS[facet] || facet)}}</span>`);
      }});
      return badges.join("");
    }}
    
    function applyViewMode(mode) {{
      if (!VIEW_ORDER.includes(mode)) return;
      s.viewMode = mode;
      D.getElementById("btn-split").textContent = `View: ${{VIEW_LABELS[mode] || mode}}`;
      if (s.iframe && s.iframe.body) {{
        s.iframe.body.className = `view-${{mode}}`;
      }}
    }}

    function cycleViewMode() {{
      const currentIndex = VIEW_ORDER.indexOf(s.viewMode);
      const nextMode = VIEW_ORDER[(currentIndex + 1) % VIEW_ORDER.length];
      applyViewMode(nextMode);
      if (s.sel) syncFrame(s.sel);
    }}

    // Commands
    function z() {{ s.zen = !s.zen; body.className = s.zen ? "zen-mode" : (s.navOff ? "nav-hidden" : ""); if(s.zen) insp.classList.remove("visible"); else if(s.insp) insp.classList.add("visible"); }}
    function n() {{ if(s.zen) z(); s.navOff = !s.navOff; body.classList.toggle("nav-hidden", s.navOff); }}
    
    D.getElementById("btn-zen").onclick = z; D.getElementById("btn-exit-zen").onclick = z; D.getElementById("btn-nav").onclick = n;
    D.getElementById("btn-split").onclick = cycleViewMode;
    D.getElementById("close-insp").onclick = () => {{ s.insp = false; insp.classList.remove("visible"); }};
    D.getElementById("btn-export").onclick = () => {{ window.open(`/api/runs/${{encodeURIComponent(runId)}}/export-clean`, "_blank"); }};
    bulkAcceptBtn.onclick = () => applyBulkDecision("accept");
    bulkRejectBtn.onclick = () => applyBulkDecision("reject");
    bulkClearBtn.onclick = () => applyBulkDecision("pending");
    bulkUndoBtn.onclick = () => undoLastDecisionChange();
    formatOnlyToggle.onclick = toggleFormatOnlyFilter;
    nextPendingBtn.onclick = () => nextPendingSection();
    nextFormatBtn.onclick = () => nextFormattingOnlySection();
    nextChangedBtn.onclick = () => nextChangedSection();
    nextUndecidedBtn.onclick = () => nextPendingSection();
    jumpBtn.onclick = jumpToSection;
    jumpInput.onkeydown = (e) => {{
      if (e.key === "Enter") {{
        e.preventDefault();
        jumpToSection();
      }}
    }};
    
    // Iframe Scroll Sync
    function syncFrame(idx) {{
      if(!s.iframe) return;
      const el = s.iframe.getElementById("section-" + idx);
      if(el) {{
        s.syncLockUntil = performance.now() + 650;
        el.scrollIntoView({{behavior: "smooth", block: "center"}});
        // Add a visual flash
        const origBg = el.style.backgroundColor;
        el.style.backgroundColor = "rgba(255, 230, 0, 0.4)";
        setTimeout(() => el.style.backgroundColor = origBg, 1500);
      }}
    }}

    function bestVisibleSectionIndex() {{
      if (!s.iframe || !frame.contentWindow || !s.meta) return null;
      const allowed = new Set(fSec().map(sec => sec.index));
      const rows = Array.from(s.iframe.querySelectorAll(".doc-row[data-section-index]"));
      if (!rows.length || !allowed.size) return null;
      const viewportHeight = frame.contentWindow.innerHeight || 1;
      const anchor = viewportHeight * 0.33;
      let bestIndex = null;
      let bestDistance = Number.POSITIVE_INFINITY;
      for (const row of rows) {{
        const idx = Number.parseInt(row.dataset.sectionIndex || "", 10);
        if (!Number.isInteger(idx) || !allowed.has(idx)) continue;
        const rect = row.getBoundingClientRect();
        if (rect.bottom <= 0 || rect.top >= viewportHeight) continue;
        const distance = Math.abs(rect.top - anchor);
        if (distance < bestDistance) {{
          bestDistance = distance;
          bestIndex = idx;
        }}
      }}
      return bestIndex;
    }}

    function bindIframeScrollSync() {{
      if (!s.iframe || !frame.contentWindow) return;
      if (typeof s.unbindFrameScroll === "function") {{
        s.unbindFrameScroll();
      }}
      const win = frame.contentWindow;
      let rafId = 0;
      const onScroll = () => {{
        if (performance.now() < s.syncLockUntil) return;
        if (rafId) return;
        rafId = win.requestAnimationFrame(() => {{
          rafId = 0;
          const idx = bestVisibleSectionIndex();
          if (idx && idx !== s.sel) setSel(idx, {{fromFrame: true}});
        }});
      }};
      win.addEventListener("scroll", onScroll, {{passive: true}});
      s.unbindFrameScroll = () => win.removeEventListener("scroll", onScroll);
    }}

    function applyDecisionClass(idx, decision) {{
      if (!s.iframe) return;
      const el = s.iframe.getElementById("section-" + idx);
      if (!el) return;
      el.classList.remove("decided-accept", "decided-reject");
      if (decision === "accept" || decision === "reject") {{
        el.classList.add("decided-" + decision);
      }}
    }}

    function applyDecisionsToFrame() {{
      if (!s.iframe || !s.meta) return;
      for (const section of s.meta.sections || []) {{
        applyDecisionClass(section.index, decisionForSection(section));
      }}
    }}
    
    frame.onload = () => {{
      s.iframe = frame.contentDocument;
      applyViewMode(s.viewMode);
      applyDecisionsToFrame();
      bindIframeScrollSync();
      drawMinimap();
      if (s.sel) {{
        setSel(s.sel, {{fromFrame: true}});
      }} else {{
        const initial = bestVisibleSectionIndex() || (fSec()[0] && fSec()[0].index);
        if (initial) setSel(initial, {{fromFrame: true}});
      }}
    }};
    
    function slug(v) {{ return String(v||"").toLowerCase(); }}
    function ex(sec) {{ const v = sec.revised_text || sec.original_text || ""; return v.length > 80 ? v.slice(0, 80)+"…" : v; }}
    function enc(v) {{ return String(v).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"); }}
    
    function fSec() {{
      if(!s.meta) return [];
      const trm = slug(s.q).trim();
      return s.meta.sections.filter(x => {{
        if(!sectionMatchesFilters(x)) return false;
        if(!trm) return true;
        return slug([x.label, x.kind, x.original_text, x.revised_text].join(" ")).includes(trm);
      }});
    }}
    
    function renderInsp() {{
      if(!s.meta) {{ s.insp = false; insp.classList.remove("visible"); return; }}
      const a = s.meta.sections.find(x => x.index === s.sel);
      if(!a) {{ s.insp = false; insp.classList.remove("visible"); return; }}
      s.insp = true; if(!s.zen) insp.classList.add("visible");
      D.getElementById("insp-title").textContent = a.kind_label || a.kind;
      const decision = a.is_changed ? decisionForSection(a) : "pending";
      const decisionLabel = decision.charAt(0).toUpperCase() + decision.slice(1);
      const saveState = decisionSaveStateForIndex(a.index);
      const saveLabel = saveState ? ` · ${{DECISION_STATE_LABELS[saveState] || saveState}}` : "";
      D.getElementById("insp-subtitle").textContent = `Section ${{a.index}} · ${{decisionLabel}}${{saveLabel}}`;
      const formatFacets = sectionFormatFacets(a).filter(facet => facet !== "formatting");
      const facetBadges = sectionBadgeMarkup(a);
      const formattingBlock = formatFacets.length
        ? `<div class="diff-block"><div class="diff-hdr">Formatting Deltas</div><div class="diff-content">${{enc(formatFacets.map(facet => FACET_LABELS[facet] || facet).join(", "))}}</div></div>`
        : "";
      D.getElementById("insp-body").innerHTML = `
        <div class="insp-label">${{enc(a.label)}}</div>
        <div class="insp-facets">${{facetBadges || '<span class="facet-badge">No Facets</span>'}}</div>
        ${{formattingBlock}}
        <div class="diff-block"><div class="diff-hdr">Original</div><div class="diff-content">${{enc(a.original_text||"—")}}</div></div>
        <div class="diff-block"><div class="diff-hdr">Revised</div><div class="diff-content">${{enc(a.revised_text||"—")}}</div></div>
      `;
    }}
    
    function setSel(idx, opts = {{}}) {{
      const fromFrame = !!opts.fromFrame;
      if (!s.meta || !s.meta.sections.some(x => x.index === idx)) return;
      if (s.sel && s.iframe) {{
         const old = s.iframe.getElementById("section-" + s.sel);
         if (old) old.classList.remove("active");
      }}
      s.sel = idx;
      updateSectionPill();
      jumpInput.value = String(idx);
      if (s.iframe) {{
         const cur = s.iframe.getElementById("section-" + idx);
         if (cur) cur.classList.add("active");
      }}
      renderSections();
      if (!fromFrame) syncFrame(idx);
      const card = D.querySelector(`.detail-card[data-index="${{idx}}"]`);
      if(card) card.scrollIntoView({{behavior: fromFrame ? "auto" : "smooth", block: "nearest"}});
    }}
    
    function drawMinimap() {{
       const mmap = D.getElementById("minimap");
       if (!s.meta || !s.meta.sections || !s.iframe) return;
       const visibleSectionIndexes = new Set(fSec().map(sec => sec.index));
       const docs = Array.from(s.iframe.querySelectorAll(".doc-row[data-section-index]"));
       if (!docs.length) return;
       const first = docs[0].offsetTop, last = docs[docs.length-1].offsetTop + docs[docs.length-1].offsetHeight;
       const tot = Math.max(last - first, 1);
       
       let html = "";
       for (let d of docs) {{
          const idx = d.dataset.sectionIndex;
          const sec = s.meta.sections.find(x => x.index === parseInt(idx));
          if (!sec || !sec.is_changed || !visibleSectionIndexes.has(sec.index)) continue;
          
          const topPc = ((d.offsetTop - first) / tot) * 100;
          html += `<div class="minimap-tick ${{sec.kind}}" style="top:${{topPc}}%" onclick="setSel(${{sec.index}}); syncFrame(${{sec.index}})"></div>`;
       }}
       mmap.innerHTML = html;
    }}
    
    function renderSections() {{
      const secs = fSec();
      refreshDecisionUi();
      updateFormatOnlyUi();
      updateNavProgress();
      if(!secs.length) {{
        nList.innerHTML = '<div style="padding: 2rem 1rem; text-align:center; color:gray;">Empty</div>';
        s.sel = null;
        updateSectionPill();
        updateNavProgress();
        jumpInput.value = "";
        drawMinimap();
        renderInsp();
        return;
      }}
      if (!secs.some(x => x.index === s.sel)) {{
        s.sel = secs[0].index;
        updateSectionPill();
        jumpInput.value = String(s.sel);
      }}
      nList.innerHTML = secs.map((x, i) => `
        <div class="detail-card ${{x.index === s.sel ? 'active':''}} ${{decisionForSection(x) !== 'pending' ? 'decision-'+decisionForSection(x) : ''}}" data-index="${{x.index}}" style="animation-delay: ${{Math.min(i*0.03, 0.4)}}s">
          <div class="detail-title">${{enc(x.label||"Section "+x.index)}}</div>
          <div class="detail-meta"><span class="detail-kind">${{enc(x.kind_label || x.kind || "Section")}}</span><span class="detail-dot">•</span><span class="detail-sec">sec ${{x.index}}</span>${{x.is_changed ? ` <span class="decision-tag ${{decisionForSection(x)}}">${{decisionForSection(x)}}</span>${{decisionStatusBadgeMarkup(x.index)}}` : ""}}</div>
          <div class="detail-excerpt">${{enc(ex(x))}}</div>
          <div class="facet-badges">${{sectionBadgeMarkup(x)}}</div>
        </div>
      `).join("");
      drawMinimap();
      nList.querySelectorAll(".detail-card").forEach(c => c.onclick = () => setSel(Number(c.dataset.index)));
      renderInsp();
    }}
    
    function buildDistBar(c) {{
      const b = D.getElementById("dist-bar"), tot = c.all||1;
      const pc = (k) => ((c[k]||0)/tot*100).toFixed(1)+'%';
      b.innerHTML = `
        <div class="dist-segment dist-ins" style="width:${{pc('insert')}}"></div>
        <div class="dist-segment dist-del" style="width:${{pc('delete')}}"></div>
        <div class="dist-segment dist-rep" style="width:${{pc('replace')}}"></div>
        <div class="dist-segment dist-mov" style="width:${{pc('move')}}"></div>
        <div class="dist-segment dist-unc" style="width:${{((tot - c.changed)/tot*100).toFixed(1)}}%"></div>
      `;
    }}

    function buildKindCounts(sections) {{
      const c = {{all: sections.length, changed: 0, move: 0, replace: 0, insert: 0, delete: 0}};
      sections.forEach(sec => {{
        if (sec.is_changed) c.changed++;
        if (c[sec.kind] !== undefined) c[sec.kind]++;
      }});
      return c;
    }}

    function buildFacetCounts(sections) {{
      const counts = {{}};
      sections.forEach(sec => {{
        if (!sec.is_changed) return;
        sectionFacets(sec).forEach(facet => {{
          counts[facet] = (counts[facet] || 0) + 1;
        }});
      }});
      return counts;
    }}

    function buildDecisionCounts(sections) {{
      const counts = {{any: 0, pending: 0, accept: 0, reject: 0}};
      sections.forEach(sec => {{
        if (!sec.is_changed) return;
        counts.any += 1;
        counts[decisionForSection(sec)] += 1;
      }});
      return counts;
    }}

    function renderDecisionSummary(decisionCounts) {{
      const decided = decisionCounts.accept + decisionCounts.reject;
      decisionSummary.textContent = `${{decided}}/${{decisionCounts.any}} decided`;
    }}

    function renderDecisionFilters(decisionCounts) {{
      const flts = [
        ["any", "Any Decision", decisionCounts.any],
        ["pending", "Pending", decisionCounts.pending],
        ["accept", "Accepted", decisionCounts.accept],
        ["reject", "Rejected", decisionCounts.reject],
      ];
      decisionRow.innerHTML = flts
        .map(x => `<button class="decision-filter-btn ${{s.decisionFilter===x[0]?'active':''}}" data-decision="${{x[0]}}">${{x[1]}} (${{x[2]}})</button>`)
        .join("");
      decisionRow.querySelectorAll(".decision-filter-btn").forEach(btn => btn.onclick = () => {{
        s.decisionFilter = btn.dataset.decision;
        renderDecisionFilters(decisionCounts);
        renderSections();
      }});
    }}

    function refreshDecisionUi() {{
      if (!s.meta) {{
        decisionSummary.textContent = "0/0 decided";
        decisionRow.innerHTML = "";
        if (nextUndecidedNote) nextUndecidedNote.textContent = "Pending guidance unavailable.";
        if (nextUndecidedBtn) nextUndecidedBtn.disabled = true;
        updateUndoUi();
        return;
      }}
      const decisionCounts = buildDecisionCounts(s.meta.sections || []);
      renderDecisionSummary(decisionCounts);
      renderDecisionFilters(decisionCounts);
      updateNextUndecidedGuide();
      updateUndoUi();
    }}

    function renderKindFilters(kindCounts) {{
      const flts = [
        ["changed", "Changes", kindCounts.changed],
        ["move", "Moves", kindCounts.move],
        ["replace", "Replaced", kindCounts.replace],
        ["insert", "Inserts", kindCounts.insert],
        ["delete", "Deletes", kindCounts.delete],
        ["all", "All", kindCounts.all],
      ];
      filterRow.innerHTML = flts.map(x => `<button class="filter-btn ${{s.kindFilter===x[0]?'active':''}}" data-f="${{x[0]}}">${{x[1]}} (${{x[2]}})</button>`).join("");
      filterRow.querySelectorAll(".filter-btn").forEach(btn => btn.onclick = () => {{
        s.kindFilter = btn.dataset.f;
        renderKindFilters(kindCounts);
        renderSections();
      }});
    }}

    function renderFacetFilters(facetCounts, kindCounts) {{
      const knownFacets = FACET_ORDER.filter(facet => facetCounts[facet]);
      const extraFacets = Object.keys(facetCounts).filter(facet => !FACET_ORDER.includes(facet)).sort();
      const allFacets = [...knownFacets, ...extraFacets];
      for (const selected of Array.from(s.facetFilters)) {{
        if (!facetCounts[selected]) s.facetFilters.delete(selected);
      }}
      const clearLabel = `Any Facet (${{kindCounts.changed}})`;
      facetRow.innerHTML = `
        <button class="facet-filter-btn ${{s.facetFilters.size===0 ? 'active' : ''}}" data-facet="__any">${{clearLabel}}</button>
        ${{allFacets.map(facet => `<button class="facet-filter-btn ${{s.facetFilters.has(facet) ? 'active' : ''}}" data-facet="${{facet}}">${{enc(FACET_LABELS[facet] || facet)}} (${{facetCounts[facet]}})</button>`).join("")}}
      `;
      facetRow.querySelectorAll(".facet-filter-btn").forEach(btn => btn.onclick = () => {{
        const facet = btn.dataset.facet;
        if (facet === "__any") {{
          s.facetFilters.clear();
        }} else if (s.facetFilters.has(facet)) {{
          s.facetFilters.delete(facet);
        }} else {{
          s.facetFilters.add(facet);
        }}
        renderFacetFilters(facetCounts, kindCounts);
        renderSections();
      }});
    }}

    function jumpToSection() {{
      if (!s.meta) return;
      const idx = Number.parseInt(jumpInput.value, 10);
      if (!Number.isInteger(idx) || idx < 1) {{
        jumpInput.setCustomValidity("Enter a valid section number.");
        jumpInput.reportValidity();
        return false;
      }}
      const hasSection = s.meta.sections.some(sec => sec.index === idx);
      if (!hasSection) {{
        jumpInput.setCustomValidity("Section number not found in this run.");
        jumpInput.reportValidity();
        return false;
      }}
      jumpInput.setCustomValidity("");
      setSel(idx);
      jumpInput.blur();
      return true;
    }}

    function selectNextFromList(items, emptyMessage) {{
      if (!items.length) {{
        showBulkStatus(emptyMessage);
        return false;
      }}
      const current = items.findIndex(sec => sec.index === s.sel);
      const next = (current < 0 || current >= items.length - 1) ? items[0] : items[current + 1];
      setSel(next.index);
      showBulkStatus("");
      return true;
    }}

    function selectPrevFromList(items, emptyMessage) {{
      if (!items.length) {{
        showBulkStatus(emptyMessage);
        return false;
      }}
      const current = items.findIndex(sec => sec.index === s.sel);
      const prev = current <= 0 ? items[items.length - 1] : items[current - 1];
      setSel(prev.index);
      showBulkStatus("");
      return true;
    }}

    function nextPendingSection() {{
      const pending = fSec().filter(sec => sec.is_changed && decisionForSection(sec) === "pending");
      return selectNextFromList(pending, "No pending visible sections.");
    }}

    function nextFormattingOnlySection() {{
      const fmtOnly = fSec().filter(sec => sec.is_changed && isFormattingOnlySection(sec));
      return selectNextFromList(fmtOnly, "No formatting-only visible sections.");
    }}

    function nextChangedSection() {{
      const changed = fSec().filter(sec => sec.is_changed);
      return selectNextFromList(changed, "No changed visible sections.");
    }}

    function nextVisibleSection() {{
      return selectNextFromList(fSec(), "No visible sections.");
    }}

    function previousVisibleSection() {{
      return selectPrevFromList(fSec(), "No visible sections.");
    }}

    function focusSearch() {{
      search.focus();
      search.select();
    }}

    function focusFilterRow(row) {{
      if (!row) return;
      const btn = row.querySelector("button");
      if (btn) btn.focus();
    }}

    function bindFilterRowKeys(row, buttonSelector) {{
      row.addEventListener("keydown", (e) => {{
        const target = e.target;
        if (!(target instanceof HTMLElement) || !target.matches(buttonSelector)) return;
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        const buttons = Array.from(row.querySelectorAll(buttonSelector));
        const idx = buttons.indexOf(target);
        if (idx < 0) return;
        e.preventDefault();
        const delta = e.key === "ArrowRight" ? 1 : -1;
        const nextIndex = (idx + delta + buttons.length) % buttons.length;
        buttons[nextIndex].focus();
      }});
    }}

    function visibleChangedSectionIndexes() {{
      return fSec().filter(sec => sec.is_changed).map(sec => sec.index);
    }}

    function setLocalDecision(idx, decision) {{
      if (!s.meta) return;
      s.meta.decisions = s.meta.decisions || {{}};
      if (decision === "pending") {{
        delete s.meta.decisions[String(idx)];
      }} else {{
        s.meta.decisions[String(idx)] = decision;
      }}
      applyDecisionClass(idx, decision);
    }}

    async function persistDecision(idx, decision) {{
      const res = await fetch(`/api/runs/${{encodeURIComponent(runId)}}/decisions`, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{section_index: idx, decision}})
      }});
      const payload = await res.json();
      if (!res.ok || payload.error) throw new Error(payload.error || "Failed to save decision.");
      s.meta.decisions = payload.decisions || {{}};
    }}

    async function persistBatchDecision(indexes, decision) {{
      const res = await fetch(`/api/runs/${{encodeURIComponent(runId)}}/decisions/batch`, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{section_indexes: indexes, decision}})
      }});
      const payload = await res.json();
      if (!res.ok || payload.error) throw new Error(payload.error || "Failed to save bulk decision.");
      s.meta.decisions = payload.decisions || {{}};
      return payload.updated || 0;
    }}

    async function persistDecisionGroups(groups) {{
      for (const [decision, indexes] of Object.entries(groups)) {{
        if (!indexes.length) continue;
        if (indexes.length === 1) {{
          await persistDecision(indexes[0], decision);
        }} else {{
          await persistBatchDecision(indexes, decision);
        }}
      }}
    }}

    async function undoLastDecisionChange() {{
      if (!s.meta) return;
      if (s.decisionBusy) return;
      const action = s.decisionUndoStack.pop();
      if (!action || !Array.isArray(action.entries) || !action.entries.length) {{
        showBulkStatus("No recent decision changes to undo.");
        updateUndoUi();
        return;
      }}
      updateUndoUi();
      const indexes = action.entries.map(entry => entry.idx).filter(Number.isInteger);
      if (!indexes.length) {{
        showBulkStatus("No recent decision changes to undo.");
        return;
      }}

      const snapshot = {{...(s.meta.decisions || {{}})}};
      const groups = {{accept: [], reject: [], pending: []}};
      action.entries.forEach(entry => {{
        if (entry.previous === "accept" || entry.previous === "reject" || entry.previous === "pending") {{
          groups[entry.previous].push(entry.idx);
        }}
      }});

      setDecisionBusy(true);
      setDecisionSaveState(indexes, "saving");
      action.entries.forEach(entry => setLocalDecision(entry.idx, entry.previous));
      renderSections();
      showBulkStatus("Undoing decision changes...");

      try {{
        await persistDecisionGroups(groups);
        applyDecisionsToFrame();
        setDecisionSaveState(indexes, "saved", {{autoClear: true}});
        renderSections();
        showBulkStatus(`Undid ${{action.entries.length}} decision change${{action.entries.length === 1 ? "" : "s"}}.`);
      }} catch (err) {{
        s.meta.decisions = snapshot;
        applyDecisionsToFrame();
        setDecisionSaveState(indexes, "error");
        s.decisionUndoStack.push(action);
        renderSections();
        showBulkStatus(err.message || String(err), true);
      }} finally {{
        setDecisionBusy(false);
        updateUndoUi();
        updateNextUndecidedGuide();
      }}
    }}

    async function applyBulkDecision(decision) {{
      if (!s.meta || s.decisionBusy) return;
      const indexes = visibleChangedSectionIndexes();
      if (!indexes.length) {{
        showBulkStatus("No visible changed sections.");
        return;
      }}

      const entries = indexes.map(idx => {{
        const section = s.meta.sections.find(sec => sec.index === idx);
        const previous = section ? decisionForSection(section) : "pending";
        return {{idx, previous, next: decision}};
      }});
      const changedEntries = entries.filter(entry => entry.previous !== entry.next);
      if (!changedEntries.length) {{
        showBulkStatus("Visible changed sections already match this decision.");
        return;
      }}

      const snapshot = {{...(s.meta.decisions || {{}})}};
      setDecisionBusy(true);
      setDecisionSaveState(indexes, "saving");
      indexes.forEach(idx => setLocalDecision(idx, decision));
      renderSections();
      showBulkStatus("Saving bulk decisions...");

      try {{
        const updatedCount = await persistBatchDecision(indexes, decision);
        pushDecisionUndo(changedEntries);
        applyDecisionsToFrame();
        setDecisionSaveState(indexes, "saved", {{autoClear: true}});
        renderSections();
        showBulkStatus(`Updated ${{updatedCount}} visible sections.`);
      }} catch (err) {{
        s.meta.decisions = snapshot;
        applyDecisionsToFrame();
        setDecisionSaveState(indexes, "error");
        renderSections();
        showBulkStatus(err.message || String(err), true);
      }} finally {{
        setDecisionBusy(false);
        updateUndoUi();
        updateNextUndecidedGuide();
      }}
    }}
    
    function init(m) {{
      s.meta = m; D.getElementById("r-title").textContent = m.original_name + " → " + m.revised_name;
      s.meta.decisions = s.meta.decisions || {{}};
      s.decisionStatusByIndex = {{}};
      Object.values(s.decisionStatusTimers).forEach(timer => clearTimeout(timer));
      s.decisionStatusTimers = {{}};
      s.decisionUndoStack = [];
      setDecisionBusy(false);
      applyViewMode(s.viewMode);
      frame.src = m.preview_url;
      updateSectionPill();
      showBulkStatus("");
      
      const dlGroup = D.getElementById("dl-group");
      if (m.downloads) {{
         dlGroup.innerHTML = Object.entries(m.downloads).map(([fmt, url]) => 
            `<a href="${{url}}" class="dl-pill" target="_blank" download>Download ${{fmt.toUpperCase()}}</a>`
         ).join("");
      }}
      const kindCounts = buildKindCounts(m.sections || []);
      const facetCounts = buildFacetCounts(m.sections || []);
      buildDistBar(kindCounts);
      renderKindFilters(kindCounts);
      renderFacetFilters(facetCounts, kindCounts);
      renderSections();
    }}

    bindFilterRowKeys(filterRow, ".filter-btn");
    bindFilterRowKeys(facetRow, ".facet-filter-btn");
    bindFilterRowKeys(decisionRow, ".decision-filter-btn");
    
    D.addEventListener('keydown', e => {{
      if(e.target.tagName==="INPUT") {{
        if(e.key==="Escape") {{
          e.target.blur();
          if (e.target === jumpInput) jumpInput.setCustomValidity("");
        }}
        return;
      }}
      if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) {{
        e.preventDefault();
        undoLastDecisionChange();
        return;
      }}
      if(e.key === "z" || e.key === "Z") z();
      if(e.key === "Escape" && s.zen) z();
      if(e.key === "b" || e.key === "B") n();
      if(e.key === "s" || e.key === "S") cycleViewMode();
      if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {{
        e.preventDefault();
        focusSearch();
        return;
      }}
      if(e.key === "/") {{ e.preventDefault(); focusSearch(); }}
      if(e.key === "g" || e.key === "G") {{ e.preventDefault(); jumpInput.focus(); jumpInput.select(); }}
      if(e.key === "a" || e.key === "A") {{ if (s.sel) makeDecision(s.sel, "accept"); }}
      if(e.key === "r" || e.key === "R") {{ if (s.sel) makeDecision(s.sel, "reject"); }}
      if(e.key === "u" || e.key === "U") {{ if (s.sel) makeDecision(s.sel, "pending"); }}
      if(e.key === "n" || e.key === "N") {{ nextPendingSection(); }}
      if(e.key === "m" || e.key === "M") {{ nextFormattingOnlySection(); }}
      if(e.key === "c" || e.key === "C") {{ nextChangedSection(); }}
      if(e.key === "f" || e.key === "F") {{ toggleFormatOnlyFilter(); }}
      if(e.key === "1") {{ focusFilterRow(filterRow); }}
      if(e.key === "2") {{ focusFilterRow(facetRow); }}
      if(e.key === "3") {{ focusFilterRow(decisionRow); }}
      if(e.key === "j" || e.key === "J" || e.key === "ArrowDown") {{
        nextVisibleSection();
      }}
      if(e.key === "k" || e.key === "K" || e.key === "ArrowUp") {{
        previousVisibleSection();
      }}
    }});

    async function makeDecision(idx, decision) {{
      if (!s.meta || s.decisionBusy) return;
      const section = s.meta.sections.find(x => x.index === idx);
      if (!section || !section.is_changed) return;

      const previous = decisionForSection(section);
      if (previous === decision) {{
        showBulkStatus(`Section ${{idx}} already ${{
          decision === "pending" ? "cleared" : decision + "ed"
        }}.`);
        return;
      }}
      const snapshot = {{...(s.meta.decisions || {{}})}};
      setDecisionBusy(true);
      setDecisionSaveState([idx], "saving");
      setLocalDecision(idx, decision);
      renderSections();
      showBulkStatus("Saving decision...");

      try {{
        await persistDecision(idx, decision);
        pushDecisionUndo([{{idx, previous, next: decision}}]);
        applyDecisionsToFrame();
        setDecisionSaveState([idx], "saved", {{autoClear: true}});
        renderSections();
        showBulkStatus("Decision saved.");
      }} catch (err) {{
        s.meta.decisions = snapshot;
        applyDecisionsToFrame();
        setDecisionSaveState([idx], "error");
        renderSections();
        showBulkStatus(err.message || String(err), true);
        return;
      }} finally {{
        setDecisionBusy(false);
        updateUndoUi();
        updateNextUndecidedGuide();
      }}

      if (decision !== "pending") {{
        setTimeout(() => {{
          const sc = fSec();
          const c = sc.findIndex(x => x.index === s.sel);
          if (c >= 0 && c < sc.length - 1) setSel(sc[c + 1].index);
        }}, 120);
      }}
    }}
    
    search.oninput = () => {{ s.q = search.value; renderSections(); }};
    search.onkeydown = (e) => {{
      if (e.key === "Enter") {{
        e.preventDefault();
        if (e.shiftKey) previousVisibleSection();
        else nextVisibleSection();
      }}
      if (e.key === "ArrowDown") {{
        e.preventDefault();
        nextVisibleSection();
      }}
      if (e.key === "ArrowUp") {{
        e.preventDefault();
        previousVisibleSection();
      }}
    }};
    
    fetch(`/api/runs/${{encodeURIComponent(runId)}}`).then(r => r.json()).then(init).catch(e => console.error(e));
  </script>
</body>
</html>
"""


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_length = handler.headers.get("Content-Length")
    if not raw_length:
        raise ValueError("Missing request body.")
    body = handler.rfile.read(int(raw_length))
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    return payload


def _send_html(handler: BaseHTTPRequestHandler, body: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
    payload = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _send_json(handler: BaseHTTPRequestHandler, body: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
    payload = (json.dumps(body, ensure_ascii=True) + "\n").encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _send_error(handler: BaseHTTPRequestHandler, status: HTTPStatus, message: str) -> None:
    _send_json(handler, {"error": message}, status=status)


def _serve_file(handler: BaseHTTPRequestHandler, path: Path, *, as_attachment: bool = False) -> None:
    if not path.exists() or not path.is_file():
        _send_error(handler, HTTPStatus.NOT_FOUND, "File not found")
        return
    payload = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    if as_attachment:
        handler.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
    handler.end_headers()
    handler.wfile.write(payload)


def _safe_filename(value: Any, *, default: str) -> str:
    candidate = Path(str(value or default)).name.strip()
    if not candidate:
        return default
    return candidate


def _safe_base_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return sanitized or "blackline_report"


def _decode_file_payload(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("Missing file payload.")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Invalid base64 file payload.") from exc


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def _metadata_path(run_dir: Path | None) -> Path:
    if run_dir is None:
        return Path("/nonexistent/metadata.json")
    return run_dir / "metadata.json"


def _load_metadata(run_dir: Path) -> dict[str, Any] | None:
    metadata_path = _metadata_path(run_dir)
    if not metadata_path.exists():
        return None
    return json.loads(metadata_path.read_text(encoding="utf-8"))
