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
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=Source+Serif+4:wght@600&display=swap');
    :root {{
      --ink: #10203a;
      --ink-soft: #2b3954;
      --muted: #5c6980;
      --muted-light: #8894aa;
      --canvas-0: #e7edf5;
      --canvas-1: #f6f8fc;
      --canvas-zen: #0d1828;
      --surface: rgba(255, 255, 255, 0.86);
      --surface-solid: #ffffff;
      --surface-deep: rgba(255, 255, 255, 0.93);
      --border-soft: rgba(131, 146, 171, 0.26);
      --border-strong: rgba(74, 95, 129, 0.32);
      --primary: #1e4a87;
      --primary-hover: #183f73;
      --accent: #0f5f87;
      --focus-ring: rgba(22, 81, 150, 0.24);
      --accept-soft: rgba(24, 123, 101, 0.12);
      --reject-soft: rgba(188, 65, 54, 0.1);
      --shadow-float: 0 28px 48px -30px rgba(13, 24, 40, 0.62), 0 14px 24px -18px rgba(13, 24, 40, 0.44);
      --shadow-soft: 0 14px 28px -24px rgba(16, 32, 58, 0.32);
      --font-ui: 'IBM Plex Sans', 'Avenir Next', 'Segoe UI', sans-serif;
      --font-display: 'Source Serif 4', 'Georgia', serif;
      --ins: #137f6f;
      --del: #c64b40;
      --rep: #b87b16;
      --mov: #1f5ea3;
      --timing: 220ms cubic-bezier(0.2, 0.74, 0.24, 1);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 0;
      width: 100vw;
      height: 100vh;
      overflow: hidden;
      font-family: var(--font-ui);
      color: var(--ink);
      background:
        radial-gradient(1120px 720px at -9% 120%, rgba(68, 108, 170, 0.14) 0%, transparent 56%),
        radial-gradient(900px 620px at 108% -16%, rgba(15, 95, 135, 0.16) 0%, transparent 52%),
        linear-gradient(170deg, var(--canvas-0) 0%, var(--canvas-1) 52%, #f3f6fb 100%);
      transition: background var(--timing), color var(--timing);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(255, 255, 255, 0.35) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255, 255, 255, 0.25) 1px, transparent 1px);
      background-size: 48px 48px;
      opacity: 0.38;
      z-index: 0;
    }}
    body.zen-mode {{
      background:
        radial-gradient(920px 520px at -8% 120%, rgba(29, 92, 132, 0.22) 0%, transparent 58%),
        radial-gradient(720px 460px at 108% -15%, rgba(34, 80, 128, 0.26) 0%, transparent 52%),
        linear-gradient(168deg, #0d1828 0%, #101f33 50%, #14253c 100%);
    }}
    body.zen-mode::before {{ opacity: 0.18; }}

    .stage {{
      position: absolute;
      top: 58px;
      left: 0;
      right: 0;
      bottom: 0;
      padding: 0.9rem 0.95rem 0.92rem;
      transition: top var(--timing);
      z-index: 1;
    }}
    body.zen-mode .stage {{ top: 0; }}
    .preview-shell {{
      position: relative;
      width: 100%;
      height: 100%;
      border-radius: 24px;
      border: 1px solid rgba(120, 141, 171, 0.3);
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.82) 0%, rgba(245, 250, 255, 0.72) 100%),
        radial-gradient(600px 300px at 10% -6%, rgba(73, 116, 180, 0.12) 0%, transparent 60%);
      box-shadow: 0 30px 48px -34px rgba(16, 32, 58, 0.64), 0 18px 30px -24px rgba(16, 32, 58, 0.48);
      overflow: hidden;
      isolation: isolate;
    }}
    body.zen-mode .preview-shell {{
      border-color: rgba(137, 164, 205, 0.26);
      background:
        linear-gradient(180deg, rgba(14, 27, 44, 0.74) 0%, rgba(16, 31, 52, 0.7) 100%),
        radial-gradient(620px 360px at 16% -5%, rgba(42, 97, 162, 0.24) 0%, transparent 58%);
      box-shadow: 0 30px 50px -34px rgba(3, 9, 20, 0.82), 0 20px 34px -28px rgba(3, 9, 20, 0.68);
    }}
    .preview-shell::before {{
      content: "";
      position: absolute;
      inset: 0;
      border-radius: inherit;
      border: 1px solid rgba(255, 255, 255, 0.45);
      pointer-events: none;
      mix-blend-mode: screen;
      opacity: 0.58;
      z-index: 0;
    }}
    body.zen-mode .preview-shell::before {{
      border-color: rgba(201, 221, 255, 0.16);
      opacity: 0.42;
      mix-blend-mode: normal;
    }}
    .preview-chrome {{
      position: relative;
      z-index: 1;
      height: 40px;
      padding: 0 0.82rem 0 0.74rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.66rem;
      border-bottom: 1px solid rgba(130, 148, 174, 0.32);
      background: linear-gradient(178deg, rgba(255, 255, 255, 0.8) 0%, rgba(244, 249, 255, 0.62) 100%);
    }}
    body.zen-mode .preview-chrome {{
      border-bottom-color: rgba(133, 159, 198, 0.24);
      background: linear-gradient(178deg, rgba(20, 40, 66, 0.74) 0%, rgba(17, 33, 55, 0.62) 100%);
    }}
    .preview-title {{
      display: inline-flex;
      align-items: center;
      gap: 0.42rem;
      font-size: 0.68rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #627592;
      font-weight: 700;
      white-space: nowrap;
    }}
    body.zen-mode .preview-title {{ color: #a7bcdd; }}
    .preview-dot {{
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 50%;
      background: linear-gradient(140deg, #2e6fbf 0%, #5da0e5 100%);
      box-shadow: 0 0 0 2px rgba(77, 133, 203, 0.2);
    }}
    .preview-mode {{
      display: inline-flex;
      align-items: center;
      gap: 0.34rem;
      padding: 0.16rem 0.48rem;
      border-radius: 999px;
      border: 1px solid rgba(108, 128, 158, 0.34);
      background: rgba(255, 255, 255, 0.8);
      color: #52647f;
      font-size: 0.62rem;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .preview-mode strong {{
      color: #1d447d;
      font-weight: 700;
      letter-spacing: 0.02em;
      text-transform: none;
      font-size: 0.68rem;
    }}
    body.zen-mode .preview-mode {{
      border-color: rgba(136, 163, 201, 0.32);
      background: rgba(22, 45, 73, 0.68);
      color: #a8bfdd;
    }}
    body.zen-mode .preview-mode strong {{ color: #d4e4fb; }}
    .preview-body {{
      position: relative;
      z-index: 1;
      height: calc(100% - 40px);
      padding-right: 30px;
      border-radius: 0 0 24px 24px;
      overflow: hidden;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.94) 0%, rgba(250, 252, 255, 0.9) 100%);
    }}
    body.zen-mode .preview-body {{
      background: linear-gradient(180deg, rgba(18, 35, 57, 0.88) 0%, rgba(16, 30, 48, 0.86) 100%);
    }}
    .preview-body::after {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      box-shadow: inset 0 0 0 1px rgba(130, 149, 176, 0.18), inset 0 24px 40px -34px rgba(12, 27, 48, 0.36);
      z-index: 2;
    }}
    body.zen-mode .preview-body::after {{
      box-shadow: inset 0 0 0 1px rgba(124, 152, 195, 0.18), inset 0 24px 40px -34px rgba(5, 12, 24, 0.7);
    }}
    iframe {{
      width: 100%;
      height: 100%;
      border: none;
      background: transparent;
    }}

    @keyframes slideUpFade {{
      0% {{ opacity: 0; transform: translateY(10px) scale(0.99); }}
      100% {{ opacity: 1; transform: translateY(0) scale(1); }}
    }}

    .slim-header {{
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      min-height: 58px;
      padding: 0.6rem 1rem;
      background: linear-gradient(160deg, rgba(255, 255, 255, 0.84) 0%, rgba(247, 250, 255, 0.76) 100%);
      backdrop-filter: blur(18px) saturate(1.2);
      -webkit-backdrop-filter: blur(18px) saturate(1.2);
      border-bottom: 1px solid var(--border-soft);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      z-index: 100;
      transition: transform var(--timing), background var(--timing);
    }}
    body.zen-mode .slim-header {{ transform: translateY(-100%); }}
    .header-left, .header-right {{ display: flex; align-items: center; gap: 0.7rem; min-width: 0; }}
    .header-right {{ margin-left: auto; flex-wrap: wrap; justify-content: flex-end; }}
    .run-title {{
      font-size: 0.88rem;
      font-weight: 500;
      color: var(--ink-soft);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: min(56vw, 640px);
    }}
    .run-title strong {{
      font-family: var(--font-display);
      color: var(--ink);
      font-size: 0.97rem;
      font-weight: 600;
      letter-spacing: 0.01em;
    }}
    .run-slash {{ color: #8a98ae; margin-inline: 0.22rem; }}
    .run-id {{ color: #6c7a90; }}
    .actions-group {{ display: flex; align-items: center; gap: 0.45rem; }}
    .icon-btn {{
      width: 36px;
      height: 36px;
      border-radius: 10px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--ink-soft);
      cursor: pointer;
      transition: background var(--timing), border-color var(--timing), color var(--timing), transform 140ms ease;
    }}
    .icon-btn:hover {{ background: rgba(23, 48, 84, 0.08); border-color: rgba(50, 84, 134, 0.18); color: var(--ink); }}
    .icon-btn:active {{ transform: translateY(1px); }}
    .icon-btn:focus-visible {{
      outline: none;
      border-color: rgba(34, 83, 144, 0.35);
      box-shadow: 0 0 0 3px var(--focus-ring);
    }}
    .icon-btn-sm {{ width: 26px; height: 26px; border-radius: 8px; font-size: 0.76rem; }}
    .pill-btn,
    .primary-btn,
    .dl-pill {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      font-size: 0.8rem;
      line-height: 1;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
      transition: background var(--timing), border-color var(--timing), color var(--timing), box-shadow var(--timing), transform 140ms ease;
    }}
    .pill-btn {{
      padding: 0.5rem 0.88rem;
      border: 1px solid var(--border-soft);
      background: var(--surface-deep);
      color: var(--ink-soft);
      box-shadow: var(--shadow-soft);
    }}
    .pill-btn:hover {{ background: #fff; border-color: var(--border-strong); color: var(--ink); }}
    .primary-btn {{
      padding: 0.5rem 0.95rem;
      border: 1px solid transparent;
      background: linear-gradient(138deg, var(--primary) 0%, #285da0 100%);
      color: #f7fbff;
      box-shadow: 0 10px 18px -12px rgba(26, 76, 137, 0.78);
    }}
    .primary-btn:hover {{ background: linear-gradient(138deg, var(--primary-hover) 0%, #204f8c 100%); box-shadow: 0 14px 22px -14px rgba(26, 76, 137, 0.72); }}
    .primary-btn:active {{ transform: translateY(1px); }}
    .export-btn {{ background: linear-gradient(140deg, #12786a 0%, #1c907f 100%); box-shadow: 0 10px 18px -12px rgba(18, 120, 106, 0.76); }}
    .export-btn:hover {{ background: linear-gradient(140deg, #0f695d 0%, #1a7b6d 100%); box-shadow: 0 14px 22px -14px rgba(18, 120, 106, 0.68); }}
    .dl-pill {{
      padding: 0.5rem 0.82rem;
      border: 1px solid rgba(30, 74, 135, 0.26);
      background: rgba(255, 255, 255, 0.94);
      color: var(--primary);
      box-shadow: var(--shadow-soft);
    }}
    .dl-pill:hover {{ background: #fff; border-color: rgba(30, 74, 135, 0.45); color: #143763; }}
    .sec-pill {{
      margin-left: 0.45rem;
      padding: 0.2rem 0.56rem;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 600;
      color: #5f6f87;
      border: 1px solid rgba(120, 136, 162, 0.28);
      background: rgba(255, 255, 255, 0.68);
      white-space: nowrap;
    }}
    .nav-progress {{
      margin-left: 0.4rem;
      padding: 0.2rem 0.56rem;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 600;
      color: #1f4d8f;
      border: 1px solid rgba(41, 91, 156, 0.26);
      background: rgba(31, 93, 163, 0.09);
      white-space: nowrap;
    }}

    .floating-navigator {{
      position: absolute;
      top: 0.92rem;
      left: 0.95rem;
      bottom: 0.95rem;
      width: 350px;
      background: linear-gradient(164deg, rgba(255, 255, 255, 0.9) 0%, rgba(249, 252, 255, 0.83) 100%);
      backdrop-filter: blur(18px) saturate(1.12);
      -webkit-backdrop-filter: blur(18px) saturate(1.12);
      border: 1px solid var(--border-soft);
      border-radius: 22px;
      box-shadow: var(--shadow-float);
      display: flex;
      flex-direction: column;
      transition: transform var(--timing), opacity var(--timing);
      z-index: 50;
      overflow: hidden;
    }}
    body.zen-mode .floating-navigator,
    body.nav-hidden .floating-navigator {{
      transform: translateX(calc(-100% - 2rem));
      opacity: 0;
    }}

    .nav-search {{
      padding: 0.9rem 0.95rem 0.86rem;
      border-bottom: 1px solid var(--border-soft);
      display: flex;
      flex-direction: column;
      gap: 0.62rem;
      background: linear-gradient(180deg, rgba(249, 252, 255, 0.7) 0%, rgba(245, 250, 255, 0.5) 100%);
    }}
    .nav-section {{
      border: 1px solid rgba(136, 153, 179, 0.24);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.76);
      padding: 0.52rem 0.56rem 0.58rem;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.65);
    }}
    .nav-section-title {{
      margin-bottom: 0.38rem;
      font-size: 0.62rem;
      letter-spacing: 0.08em;
      font-weight: 700;
      text-transform: uppercase;
      color: #7e8ca3;
    }}
    .nav-section-distribution .quick-row:first-of-type {{ margin-top: 0.52rem; }}
    .nav-search input {{
      width: 100%;
      border-radius: 10px;
      border: 1px solid var(--border-soft);
      background: rgba(255, 255, 255, 0.9);
      color: var(--ink);
      padding: 0.62rem 0.72rem;
      font-family: inherit;
      font-size: 0.8rem;
      transition: border-color var(--timing), box-shadow var(--timing), background var(--timing);
    }}
    .nav-search input:focus-visible {{
      outline: none;
      border-color: rgba(34, 83, 144, 0.42);
      box-shadow: 0 0 0 3px var(--focus-ring);
      background: #fff;
    }}
    .jump-row {{ margin-top: 0.48rem; display: flex; gap: 0.45rem; align-items: center; }}
    .jump-row input {{
      width: 100%;
      border-radius: 9px;
      border: 1px solid var(--border-soft);
      background: rgba(255, 255, 255, 0.9);
      padding: 0.52rem 0.64rem;
      font-family: inherit;
      font-size: 0.79rem;
    }}
    .jump-row button {{
      border-radius: 9px;
      border: 1px solid var(--border-soft);
      background: rgba(255, 255, 255, 0.92);
      color: var(--ink-soft);
      padding: 0.5rem 0.72rem;
      cursor: pointer;
      font-size: 0.75rem;
      font-weight: 700;
      white-space: nowrap;
      transition: border-color var(--timing), color var(--timing), background var(--timing);
    }}
    .jump-row button:hover {{ border-color: rgba(72, 97, 130, 0.4); color: var(--ink); background: #fff; }}

    .dist-bar {{ display: flex; height: 7px; border-radius: 4px; overflow: hidden; background: rgba(220, 228, 239, 0.55); }}
    .dist-segment {{ height: 100%; }}
    .dist-ins {{ background: var(--ins); }}
    .dist-del {{ background: var(--del); }}
    .dist-rep {{ background: var(--rep); }}
    .dist-mov {{ background: var(--mov); }}
    .dist-unc {{ background: #dbe5f2; }}
    .quick-row {{ margin-top: 0.52rem; display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; }}
    .quick-btn {{
      border-radius: 999px;
      border: 1px solid var(--border-soft);
      background: rgba(255, 255, 255, 0.88);
      color: var(--ink-soft);
      padding: 0.3rem 0.64rem;
      font-size: 0.7rem;
      font-weight: 700;
      cursor: pointer;
      white-space: nowrap;
      transition: border-color var(--timing), color var(--timing), background var(--timing), box-shadow var(--timing);
    }}
    .quick-btn:hover {{ border-color: rgba(72, 97, 130, 0.42); color: var(--ink); background: #fff; }}
    .quick-btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    .quick-btn.active {{ background: #176d8b; border-color: #176d8b; color: #fff; box-shadow: 0 8px 18px -14px rgba(23, 109, 139, 0.86); }}
    .quick-btn.subtle {{ font-weight: 600; }}
    .quick-count {{ font-size: 0.7rem; color: var(--muted); }}

    .filter-group {{
      border-bottom: 1px solid var(--border-soft);
      padding: 0.24rem 0 0.22rem;
      background: linear-gradient(180deg, rgba(251, 253, 255, 0.58) 0%, rgba(247, 250, 255, 0.5) 100%);
    }}
    .filter-group:last-of-type {{ border-bottom: 1px solid var(--border-soft); }}
    .filter-label {{
      padding: 0.34rem 1rem 0.2rem;
      font-size: 0.64rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      color: #8190a8;
      text-transform: uppercase;
    }}
    .filters-scroll {{
      padding: 0.12rem 1rem 0.48rem;
      display: flex;
      gap: 0.32rem;
      overflow-x: auto;
      scrollbar-width: none;
      transition: opacity 150ms ease, transform 170ms ease;
    }}
    .filters-scroll.scope-shift {{ opacity: 0.72; transform: translateY(-1px); }}
    .filter-btn,
    .facet-filter-btn,
    .decision-filter-btn {{
      border-radius: 999px;
      border: 1px solid var(--border-soft);
      background: rgba(255, 255, 255, 0.88);
      color: var(--ink-soft);
      cursor: pointer;
      white-space: nowrap;
      transition: background var(--timing), border-color var(--timing), color var(--timing), box-shadow var(--timing);
    }}
    .filter-btn {{ padding: 0.3rem 0.6rem; font-size: 0.74rem; }}
    .facet-filter-btn,
    .decision-filter-btn {{ padding: 0.28rem 0.55rem; font-size: 0.7rem; }}
    .filter-btn:hover,
    .facet-filter-btn:hover,
    .decision-filter-btn:hover {{
      border-color: rgba(75, 101, 135, 0.4);
      background: #fff;
    }}
    .filter-btn:focus-visible,
    .facet-filter-btn:focus-visible,
    .decision-filter-btn:focus-visible {{
      outline: none;
      border-color: rgba(34, 83, 144, 0.35);
      box-shadow: 0 0 0 3px var(--focus-ring);
    }}
    .filter-btn.active {{ background: var(--ink); color: #f3f8ff; border-color: var(--ink); box-shadow: 0 9px 15px -12px rgba(15, 30, 54, 0.84); }}
    .facet-filter-btn.active {{ background: #0f607e; color: #fff; border-color: #0f607e; box-shadow: 0 8px 14px -10px rgba(15, 96, 126, 0.8); }}
    .decision-filter-btn.active {{ background: #1d4f8d; color: #fff; border-color: #1d4f8d; box-shadow: 0 8px 14px -10px rgba(29, 79, 141, 0.75); }}

    .bulk-row {{
      padding: 0.62rem 1rem;
      border-bottom: 1px solid var(--border-soft);
      display: flex;
      gap: 0.42rem;
      align-items: center;
      flex-wrap: wrap;
      background: linear-gradient(180deg, rgba(248, 251, 255, 0.72) 0%, rgba(244, 249, 255, 0.58) 100%);
    }}
    .bulk-btn {{
      border-radius: 9px;
      border: 1px solid var(--border-soft);
      background: rgba(255, 255, 255, 0.9);
      color: var(--ink-soft);
      padding: 0.38rem 0.6rem;
      font-size: 0.71rem;
      font-weight: 600;
      cursor: pointer;
      transition: border-color var(--timing), background var(--timing), color var(--timing);
    }}
    .bulk-btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    .bulk-btn:hover {{ background: #fff; border-color: rgba(75, 101, 135, 0.42); color: var(--ink); }}
    .bulk-status {{ margin-left: auto; font-size: 0.71rem; color: var(--muted); min-height: 1rem; }}
    .bulk-status.error {{ color: #b13e35; }}
    .decision-guide {{
      padding: 0.5rem 1rem 0.62rem;
      border-bottom: 1px solid var(--border-soft);
      display: flex;
      gap: 0.45rem;
      align-items: center;
      flex-wrap: wrap;
      background: linear-gradient(180deg, rgba(251, 253, 255, 0.72) 0%, rgba(246, 250, 255, 0.58) 100%);
    }}
    .decision-guide-note {{ font-size: 0.7rem; color: var(--muted); line-height: 1.2; }}
    .decision-summary {{
      margin-left: 0.42rem;
      padding: 0.2rem 0.56rem;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 600;
      color: #136657;
      border: 1px solid rgba(19, 102, 87, 0.26);
      background: rgba(20, 122, 101, 0.1);
      white-space: nowrap;
    }}

    .change-list-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      padding: 0.46rem 1rem 0.4rem;
      border-bottom: 1px solid rgba(136, 154, 178, 0.22);
      background: rgba(250, 253, 255, 0.7);
    }}
    .change-list-title {{
      font-size: 0.68rem;
      font-weight: 700;
      letter-spacing: 0.07em;
      text-transform: uppercase;
      color: #73839c;
    }}
    .change-list-note {{
      font-size: 0.65rem;
      color: #8b99af;
      white-space: nowrap;
    }}
    .change-list {{
      flex: 1;
      overflow-y: auto;
      padding: 0.48rem 0.52rem 0.8rem;
      scroll-behavior: smooth;
      transition: opacity 180ms ease, transform 180ms ease, filter 220ms ease;
    }}
    .change-list.scope-shift {{ opacity: 0.68; transform: translateY(2px); filter: saturate(0.92); }}
    .empty-state {{
      padding: 1.7rem 1rem;
      text-align: center;
      color: #6e7f98;
      font-size: 0.8rem;
      border-radius: 12px;
      border: 1px dashed rgba(133, 152, 178, 0.32);
      background: rgba(253, 254, 255, 0.82);
    }}
    .detail-card {{
      padding: 0.74rem 0.78rem;
      border-radius: 14px;
      margin-bottom: 0.34rem;
      cursor: pointer;
      transition: transform var(--timing), border-color var(--timing), box-shadow var(--timing), background var(--timing);
      border: 1px solid rgba(146, 166, 194, 0.18);
      border-left: 4px solid transparent;
      background: linear-gradient(160deg, rgba(255, 255, 255, 0.78) 0%, rgba(246, 250, 255, 0.74) 100%);
      animation: slideUpFade 420ms cubic-bezier(0.2, 0.74, 0.24, 1) backwards;
      box-shadow: 0 7px 14px -16px rgba(16, 32, 58, 0.5);
    }}
    .detail-card:hover {{
      background: linear-gradient(160deg, rgba(255, 255, 255, 0.94) 0%, rgba(249, 252, 255, 0.9) 100%);
      border-color: rgba(96, 123, 163, 0.3);
      transform: translateX(2px);
    }}
    .detail-card.active {{
      background: linear-gradient(148deg, #ffffff 0%, #f4f8ff 100%);
      border-left-color: var(--primary);
      border-color: rgba(33, 74, 131, 0.34);
      box-shadow: 0 14px 26px -20px rgba(21, 56, 104, 0.76), 0 0 0 1px rgba(32, 75, 134, 0.3);
      transform: translateX(4px);
      z-index: 10;
    }}
    .detail-card.active.is-changed {{
      border-left-color: #2a5e9f;
      box-shadow: 0 15px 28px -20px rgba(24, 60, 109, 0.8), 0 0 0 1px rgba(35, 83, 148, 0.32);
    }}
    .detail-card.is-changed {{ border-left-color: rgba(95, 115, 141, 0.5); }}
    .detail-card.decision-pending {{
      border-right: 4px solid #8ea3c2;
      background: linear-gradient(150deg, rgba(231, 238, 249, 0.58) 0%, rgba(255, 255, 255, 0.96) 62%);
    }}
    .detail-card.decision-accept {{
      border-right: 4px solid var(--ins);
      background: linear-gradient(150deg, var(--accept-soft) 0%, rgba(255, 255, 255, 0.95) 58%);
    }}
    .detail-card.decision-reject {{
      border-right: 4px solid var(--del);
      background: linear-gradient(150deg, var(--reject-soft) 0%, rgba(255, 255, 255, 0.95) 58%);
    }}
    .detail-card.active.decision-pending {{
      border-left-color: #5b7aa6;
      border-right-color: #6f8db8;
      background: linear-gradient(145deg, rgba(224, 234, 248, 0.9) 0%, #fdfefe 66%);
      box-shadow: 0 16px 28px -20px rgba(52, 77, 116, 0.62), 0 0 0 1px rgba(102, 130, 170, 0.38);
    }}
    .detail-card.active.decision-accept {{
      border-left-color: #0f6f62;
      border-right-color: #0f6f62;
      background: linear-gradient(145deg, rgba(201, 238, 230, 0.92) 0%, rgba(255, 255, 255, 0.96) 64%);
      box-shadow: 0 16px 28px -20px rgba(14, 94, 82, 0.56), 0 0 0 1px rgba(16, 109, 94, 0.32);
    }}
    .detail-card.active.decision-reject {{
      border-left-color: #ab4035;
      border-right-color: #ab4035;
      background: linear-gradient(145deg, rgba(245, 218, 214, 0.9) 0%, rgba(255, 255, 255, 0.95) 64%);
      box-shadow: 0 16px 28px -20px rgba(143, 51, 42, 0.58), 0 0 0 1px rgba(171, 63, 52, 0.3);
    }}
    .detail-title {{
      font-size: 0.83rem;
      font-weight: 600;
      color: var(--ink);
      line-height: 1.3;
      margin-bottom: 0.34rem;
    }}
    .detail-meta {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 0.45rem;
      font-size: 0.68rem;
      color: var(--muted-light);
      margin-bottom: 0.42rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .detail-meta-left {{
      display: flex;
      align-items: center;
      gap: 0.35rem;
      flex-wrap: wrap;
      min-width: 0;
    }}
    .detail-meta-right {{
      display: inline-flex;
      align-items: center;
      gap: 0.24rem;
      margin-left: auto;
      flex-shrink: 0;
    }}
    .detail-kind {{ color: #5e6e86; font-weight: 700; }}
    .detail-dot {{ color: #b4c2d7; }}
    .detail-sec {{
      display: inline-block;
      padding: 0.08rem 0.35rem;
      border-radius: 999px;
      border: 1px solid rgba(115, 138, 171, 0.32);
      background: rgba(255, 255, 255, 0.92);
      color: #4a5f80;
      font-weight: 700;
    }}
    .detail-excerpt {{
      font-size: 0.78rem;
      color: #49566d;
      line-height: 1.32;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      min-height: 2.1em;
    }}
    .facet-badges {{ margin-top: 0.38rem; display: flex; flex-wrap: wrap; gap: 0.28rem; }}
    .facet-badge {{
      display: inline-block;
      padding: 0.09rem 0.36rem;
      border-radius: 999px;
      font-size: 0.61rem;
      font-weight: 700;
      letter-spacing: 0.03em;
      border: 1px solid rgba(129, 147, 172, 0.31);
      color: #3d506e;
      background: rgba(244, 248, 253, 0.94);
      text-transform: uppercase;
    }}
    .facet-badge.format-only {{
      color: #0c6a74;
      border-color: rgba(12, 106, 116, 0.3);
      background: rgba(18, 122, 133, 0.13);
    }}
    .decision-tag {{
      display: inline-flex;
      align-items: center;
      gap: 0.26rem;
      padding: 0.11rem 0.42rem;
      border-radius: 999px;
      font-size: 0.58rem;
      font-weight: 700;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      border: 1px solid transparent;
    }}
    .decision-tag::before {{
      content: "";
      width: 0.36rem;
      height: 0.36rem;
      border-radius: 50%;
      background: currentColor;
      opacity: 0.85;
      flex-shrink: 0;
    }}
    .decision-tag.pending {{ color: var(--muted); border-color: rgba(128, 145, 171, 0.32); background: rgba(245, 248, 253, 0.92); }}
    .decision-tag.accept {{ color: #0d6658; border-color: rgba(13, 102, 88, 0.28); background: rgba(19, 127, 111, 0.1); }}
    .decision-tag.reject {{ color: #ab3f34; border-color: rgba(171, 63, 52, 0.26); background: rgba(198, 75, 64, 0.1); }}
    .decision-state {{
      display: inline-block;
      padding: 0.11rem 0.46rem;
      border-radius: 999px;
      font-size: 0.6rem;
      font-weight: 700;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      border: 1px solid transparent;
    }}
    .decision-state.saving {{ color: #8a4c08; border-color: rgba(138, 76, 8, 0.26); background: rgba(184, 123, 22, 0.16); }}
    .decision-state.saved {{ color: #0d6658; border-color: rgba(13, 102, 88, 0.26); background: rgba(19, 127, 111, 0.14); }}
    .decision-state.error {{ color: #ab3f34; border-color: rgba(171, 63, 52, 0.24); background: rgba(198, 75, 64, 0.14); }}

    .floating-inspector {{
      position: absolute;
      bottom: 1.58rem;
      right: 1.7rem;
      width: 468px;
      max-height: 56vh;
      background: linear-gradient(170deg, rgba(255, 255, 255, 0.94) 0%, rgba(248, 252, 255, 0.9) 100%);
      backdrop-filter: blur(18px) saturate(1.1);
      -webkit-backdrop-filter: blur(18px) saturate(1.1);
      border: 1px solid var(--border-soft);
      border-radius: 20px;
      box-shadow: var(--shadow-float);
      display: flex;
      flex-direction: column;
      z-index: 60;
      transform: translateY(18px);
      opacity: 0;
      pointer-events: none;
      transition: transform var(--timing), opacity var(--timing);
    }}
    .floating-inspector.visible {{ transform: translateY(0); opacity: 1; pointer-events: auto; }}
    body.zen-mode .floating-inspector {{
      transform: translateY(20px) !important;
      opacity: 0 !important;
      pointer-events: none !important;
    }}

    .insp-head {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 0.75rem;
      padding: 0.92rem 1rem;
      border-bottom: 1px solid var(--border-soft);
      background: rgba(246, 250, 255, 0.64);
    }}
    .insp-head h3 {{
      margin: 0;
      font-family: var(--font-display);
      font-size: 0.96rem;
      letter-spacing: 0.01em;
      color: var(--ink);
      font-weight: 600;
    }}
    .insp-subtitle {{
      margin-top: 0.22rem;
      font-size: 0.67rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--muted-light);
      font-weight: 700;
    }}
    .insp-body {{ padding: 0.95rem 1rem 1.05rem; overflow-y: auto; }}
    .insp-label {{ font-size: 0.73rem; color: var(--muted); margin-bottom: 0.68rem; }}
    .diff-block {{
      background: rgba(255, 255, 255, 0.95);
      border: 1px solid rgba(128, 146, 173, 0.24);
      border-radius: 13px;
      margin-bottom: 0.96rem;
      overflow: hidden;
    }}
    .diff-hdr {{
      padding: 0.52rem 0.75rem;
      background: rgba(239, 245, 252, 0.74);
      font-size: 0.69rem;
      font-weight: 700;
      color: #5d6c83;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      border-bottom: 1px solid rgba(134, 153, 180, 0.24);
    }}
    .diff-content {{ padding: 0.75rem; font-size: 0.84rem; line-height: 1.44; white-space: pre-wrap; color: #344157; }}
    .insp-facets {{ margin-bottom: 0.82rem; display: flex; flex-wrap: wrap; gap: 0.35rem; }}

    .zen-exit {{
      position: absolute;
      top: 1rem;
      left: 50%;
      transform: translateX(-50%);
      padding: 0.5rem 1rem;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.14);
      backdrop-filter: blur(8px);
      border: 1px solid rgba(255, 255, 255, 0.28);
      color: #f4f8ff;
      cursor: pointer;
      z-index: 200;
      display: none;
      opacity: 0;
      transition: opacity var(--timing), background var(--timing);
      font-size: 0.8rem;
      font-weight: 600;
    }}
    body.zen-mode .zen-exit {{ display: block; opacity: 1; }}
    .zen-exit:hover {{ background: rgba(255, 255, 255, 0.22); }}

    .kbd-hints {{
      position: absolute;
      bottom: 1rem;
      left: 50%;
      transform: translateX(-50%);
      display: flex;
      gap: 0.9rem;
      background: rgba(255, 255, 255, 0.78);
      backdrop-filter: blur(18px) saturate(1.08);
      border: 1px solid var(--border-soft);
      padding: 0.46rem 0.9rem;
      border-radius: 999px;
      z-index: 100;
      box-shadow: var(--shadow-float);
      transition: opacity var(--timing);
    }}
    body.zen-mode .kbd-hints {{ opacity: 0; pointer-events: none; }}
    .kbd-hint {{ font-size: 0.72rem; color: var(--muted); display: flex; align-items: center; gap: 0.38rem; white-space: nowrap; }}
    kbd {{
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid rgba(120, 140, 168, 0.3);
      border-radius: 5px;
      padding: 0.1rem 0.4rem;
      font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
      font-weight: 600;
      color: var(--ink-soft);
    }}

    .minimap {{
      position: absolute;
      right: 0;
      top: 0;
      bottom: 0;
      width: 30px;
      background: linear-gradient(180deg, rgba(247, 251, 255, 0.9) 0%, rgba(238, 245, 253, 0.82) 100%);
      backdrop-filter: blur(10px) saturate(1.06);
      -webkit-backdrop-filter: blur(10px) saturate(1.06);
      border-left: 1px solid rgba(127, 148, 176, 0.34);
      box-shadow: inset 1px 0 0 rgba(255, 255, 255, 0.58);
      z-index: 3;
      cursor: crosshair;
      transform-origin: right center;
      transition: transform var(--timing), opacity var(--timing), filter var(--timing);
    }}
    body.zen-mode .minimap {{ transform: translateX(100%); }}
    body.nav-hidden .minimap {{ transform: translateX(100%); }}
    body.zen-mode .minimap {{
      background: linear-gradient(180deg, rgba(28, 52, 82, 0.78) 0%, rgba(20, 39, 63, 0.74) 100%);
      border-left-color: rgba(132, 159, 201, 0.24);
      box-shadow: inset 1px 0 0 rgba(192, 216, 251, 0.12);
    }}
    .minimap.scope-shift {{ opacity: 0.52; filter: saturate(0.84); transform: scaleX(0.92); }}
    .minimap-tick {{
      position: absolute;
      width: 100%;
      height: 3px;
      left: 0;
      opacity: 0.68;
      transition: transform 120ms ease, opacity 160ms ease;
    }}
    .minimap-tick:hover {{ opacity: 1; transform: scaleY(2.8); }}
    .minimap-tick.ins {{ background: var(--ins); }}
    .minimap-tick.del {{ background: var(--del); }}
    .minimap-tick.replace {{ background: var(--rep); }}
    .minimap-tick.move {{ background: var(--mov); }}

    @media (max-width: 1180px) {{
      .floating-navigator {{ width: 318px; }}
      .floating-inspector {{ width: min(430px, calc(100vw - 2.4rem)); }}
      .run-title {{ max-width: min(48vw, 520px); }}
    }}

    @media (max-width: 920px) {{
      .slim-header {{
        min-height: 88px;
        align-items: flex-start;
        padding: 0.52rem 0.78rem 0.6rem;
      }}
      .stage {{ top: 88px; padding: 0.72rem; }}
      .preview-shell {{ border-radius: 18px; }}
      .preview-body {{ border-radius: 0 0 18px 18px; }}
      .header-left {{ width: 100%; }}
      .header-right {{ width: 100%; justify-content: flex-start; gap: 0.42rem; }}
      .run-title {{ max-width: calc(100vw - 88px); }}
      .floating-navigator {{ width: min(86vw, 340px); top: 0.72rem; bottom: 0.72rem; left: 0.72rem; }}
      .floating-inspector {{ right: 1rem; left: 1rem; width: auto; max-height: 48vh; bottom: 1rem; }}
      .kbd-hints {{ display: none; }}
    }}

    @media (max-width: 640px) {{
      .slim-header {{ min-height: 96px; }}
      .stage {{ top: 96px; padding: 0.58rem; }}
      .preview-shell {{ border-radius: 14px; }}
      .preview-chrome {{ height: 36px; padding-inline: 0.58rem; }}
      .preview-mode {{ font-size: 0.58rem; padding-inline: 0.38rem; }}
      .preview-mode strong {{ font-size: 0.62rem; }}
      .preview-body {{ height: calc(100% - 36px); border-radius: 0 0 14px 14px; }}
      .nav-progress {{ display: none; }}
      .decision-summary {{ margin-left: 0.2rem; }}
      .pill-btn,
      .primary-btn,
      .dl-pill {{ font-size: 0.75rem; padding: 0.48rem 0.74rem; }}
      .icon-btn {{ width: 34px; height: 34px; }}
      .floating-navigator {{ width: calc(100vw - 1.4rem); left: 0.7rem; }}
    }}

    @media (prefers-reduced-motion: reduce) {{
      *,
      *::before,
      *::after {{
        animation-duration: 1ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 1ms !important;
        scroll-behavior: auto !important;
      }}
    }}
  </style>
</head>
<body>
  <header class="slim-header" id="header">
    <div class="header-left">
      <button id="btn-nav" class="icon-btn">☰</button>
      <div class="run-title"><strong>Review Run</strong><span class="run-slash">/</span><span id="r-title" class="run-id">...</span><span id="sec-pill" class="sec-pill">sec -</span><span id="nav-progress" class="nav-progress">0/0 visible</span><span id="decision-summary" class="decision-summary">0/0 decided</span></div>
    </div>
    <div class="header-right">
      <button id="btn-export" class="primary-btn export-btn">Export Final Doc</button>
      <div id="dl-group" class="actions-group"></div>
      <button id="btn-split" class="pill-btn">View: Inline</button>
      <button id="btn-zen" class="primary-btn">Zen Mode</button>
    </div>
  </header>

  <main class="stage">
    <section class="preview-shell" id="preview-shell">
      <div class="preview-chrome">
        <div class="preview-title"><span class="preview-dot"></span>Document Preview</div>
        <div class="preview-mode">View <strong id="preview-mode-label">Inline</strong></div>
      </div>
      <div class="preview-body">
        <iframe id="frame"></iframe>
        <div class="minimap" id="minimap"></div>
      </div>
    </section>
    <aside class="floating-navigator">
      <div class="nav-search">
        <div class="nav-section nav-section-find">
          <div class="nav-section-title">Find</div>
          <input id="search" type="search" placeholder="Search changes... (/)" />
          <div class="jump-row">
            <input id="jump-index" type="number" min="1" step="1" placeholder="Go to section #"/>
            <button id="jump-btn">Go</button>
          </div>
        </div>
        <div class="nav-section nav-section-distribution">
          <div class="nav-section-title">Scope</div>
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
      <div class="change-list-head">
        <span class="change-list-title">Section List</span>
        <span class="change-list-note">Select to inspect</span>
      </div>
      <div id="detail-list" class="change-list"></div>
    </aside>
    <div class="floating-inspector" id="inspector">
      <div class="insp-head"><div><h3 id="insp-title">Change</h3><div id="insp-subtitle" class="insp-subtitle">Section Details</div></div><button id="close-insp" class="icon-btn icon-btn-sm">✕</button></div>
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
    const previewModeLabel = D.getElementById("preview-mode-label");
    const minimap = D.getElementById("minimap");
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
      pulseScopeShift();
      renderSections();
    }}

    let scopeShiftTimer = 0;
    function pulseScopeShift() {{
      nList.classList.add("scope-shift");
      if (minimap) minimap.classList.add("scope-shift");
      [filterRow, facetRow, decisionRow].forEach(row => {{
        if (row) row.classList.add("scope-shift");
      }});
      if (scopeShiftTimer) clearTimeout(scopeShiftTimer);
      scopeShiftTimer = setTimeout(() => {{
        nList.classList.remove("scope-shift");
        if (minimap) minimap.classList.remove("scope-shift");
        [filterRow, facetRow, decisionRow].forEach(row => {{
          if (row) row.classList.remove("scope-shift");
        }});
        scopeShiftTimer = 0;
      }}, 170);
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
      if (previewModeLabel) {{
        previewModeLabel.textContent = VIEW_LABELS[mode] || mode;
      }}
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
       if (!minimap) return;
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
       minimap.innerHTML = html;
    }}
    
    function renderSections() {{
      const secs = fSec();
      refreshDecisionUi();
      updateFormatOnlyUi();
      updateNavProgress();
      if(!secs.length) {{
        nList.innerHTML = '<div class="empty-state">No sections match the current scope.</div>';
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
      nList.innerHTML = secs.map((x, i) => {{
        const decision = decisionForSection(x);
        const cardClasses = [
          "detail-card",
          x.index === s.sel ? "active" : "",
          x.is_changed ? "is-changed" : "",
          x.is_changed ? "decision-" + decision : "",
        ].filter(Boolean).join(" ");
        const decisionMeta = x.is_changed
          ? `<div class="detail-meta-right"><span class="decision-tag ${{decision}}">${{decision}}</span>${{decisionStatusBadgeMarkup(x.index)}}</div>`
          : "";
        return `
        <div class="${{cardClasses}}" data-index="${{x.index}}" style="animation-delay: ${{Math.min(i*0.03, 0.4)}}s">
          <div class="detail-title">${{enc(x.label||"Section "+x.index)}}</div>
          <div class="detail-meta">
            <div class="detail-meta-left"><span class="detail-kind">${{enc(x.kind_label || x.kind || "Section")}}</span><span class="detail-dot">•</span><span class="detail-sec">sec ${{x.index}}</span></div>
            ${{decisionMeta}}
          </div>
          <div class="detail-excerpt">${{enc(ex(x))}}</div>
          <div class="facet-badges">${{sectionBadgeMarkup(x)}}</div>
        </div>
      `;
      }}).join("");
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
        pulseScopeShift();
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
        pulseScopeShift();
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
        pulseScopeShift();
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
    
    search.oninput = () => {{
      s.q = search.value;
      pulseScopeShift();
      renderSections();
    }};
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
