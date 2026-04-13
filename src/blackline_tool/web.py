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
from .runner import VALID_FORMATS, generate_outputs

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
        _send_error(handler, HTTPStatus.NOT_FOUND, "Not found")

    def _handle_decision(self, handler: BaseHTTPRequestHandler, run_id: str) -> None:
        try:
            payload = _read_json(handler)
            run_dir = self._resolve_run_dir(run_id)
            if not run_dir:
                _send_error(handler, HTTPStatus.NOT_FOUND, "Run not found")
                return
            
            decisions_path = run_dir / "decisions.json"
            decisions = {}
            if decisions_path.exists():
                decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
            
            idx = str(payload.get("section_index"))
            decision = payload.get("decision")
            if decision in ("accept", "reject", "pending"):
                if decision == "pending":
                    decisions.pop(idx, None)
                else:
                    decisions[idx] = decision
                decisions_path.write_text(json.dumps(decisions, indent=2) + "\n", encoding="utf-8")
            
            _send_json(handler, {"status": "ok", "decisions": decisions})
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
        
        decisions_path = self._resolve_run_dir(run_id) / "decisions.json"
        if decisions_path.exists():
            payload["decisions"] = json.loads(decisions_path.read_text(encoding="utf-8"))
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
    iframe {{ width: 100%; height: 100%; border: none; }}
    
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
    
    .floating-navigator {{
      position: absolute; top: 1rem; left: 1rem; bottom: 1rem; width: 340px;
      background: var(--surface); backdrop-filter: blur(24px); border: 1px solid var(--border-soft);
      border-radius: 20px; box-shadow: var(--shadow-float); display: flex; flex-direction: column;
      transition: 0.4s; z-index: 50;
    }}
    body.zen-mode .floating-navigator, body.nav-hidden .floating-navigator {{ transform: translateX(calc(-100% - 2rem)); opacity: 0; }}
    
    .nav-search {{ padding: 1rem; border-bottom: 1px solid var(--border-soft); }}
    .nav-search input {{ width: 100%; border-radius: 8px; border: 1px solid var(--border-soft); padding: 0.6rem; font-family: inherit; }}
    
    /* Distribution Bar */
    .dist-bar {{ display: flex; height: 6px; border-radius: 3px; overflow: hidden; margin-top: 0.5rem; }}
    .dist-segment {{ height: 100%; }}
    .dist-ins {{ background: var(--ins); }} .dist-del {{ background: var(--del); }}
    .dist-rep {{ background: var(--rep); }} .dist-mov {{ background: var(--mov); }} .dist-unc {{ background: #e5e7eb; }}
    
    .filters-scroll {{ padding: 0.75rem 1rem; display: flex; gap: 0.4rem; overflow-x: auto; border-bottom: 1px solid var(--border-soft); scrollbar-width: none; }}
    .filter-btn {{ padding: 0.3rem 0.6rem; border-radius: 999px; font-size: 0.75rem; border: 1px solid var(--border-soft); background: var(--surface-solid); cursor: pointer; white-space: nowrap; }}
    .filter-btn.active {{ background: var(--ink); color: white; }}
    
    .change-list {{ flex: 1; overflow-y: auto; padding: 0.5rem; scroll-behavior: smooth; }}
    .detail-card {{ padding: 0.75rem; border-radius: 12px; margin-bottom: 0.25rem; cursor: pointer; transition: 0.2s; border-left: 3px solid transparent; }}
    .detail-card:hover {{ background: rgba(0,0,0,0.03); }}
    .detail-card.active {{ background: var(--surface-solid); border-left-color: var(--primary); box-shadow: 0 1px 2px rgba(0,0,0,0.05); }}
    .detail-card.decision-accept {{ border-right: 4px solid var(--ins); opacity: 0.7; }}
    .detail-card.decision-reject {{ border-right: 4px solid var(--del); opacity: 0.7; text-decoration: line-through; }}
    .detail-title {{ font-size: 0.85rem; font-weight: 600; }}
    .detail-meta {{ font-size: 0.7rem; color: var(--muted-light); margin-bottom: 0.4rem; text-transform: uppercase; }}
    .detail-excerpt {{ font-size: 0.8rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    
    .floating-inspector {{
      position: absolute; bottom: 2rem; right: 2rem; width: 450px; max-height: 50vh;
      background: var(--surface); backdrop-filter: blur(24px); border: 1px solid var(--border-soft);
      border-radius: 20px; box-shadow: var(--shadow-float); display: flex; flex-direction: column;
      z-index: 60; transform: translateY(20px); opacity: 0; pointer-events: none; transition: 0.3s;
    }}
    .floating-inspector.visible {{ transform: translateY(0); opacity: 1; pointer-events: auto; }}
    body.zen-mode .floating-inspector {{ transform: translateY(20px)!important; opacity: 0!important; pointer-events: none!important; }}
    
    .insp-head {{ display: flex; justify-content: space-between; padding: 1rem; border-bottom: 1px solid var(--border-soft); }}
    .insp-head h3 {{ margin: 0; font-size: 0.875rem; }}
    .insp-body {{ padding: 1rem; overflow-y: auto; }}
    .diff-block {{ background: var(--surface-solid); border: 1px solid var(--border-soft); border-radius: 12px; margin-bottom: 1rem; }}
    .diff-hdr {{ padding: 0.5rem 0.75rem; background: rgba(0,0,0,0.02); font-size: 0.7rem; font-weight: 600; color: var(--muted); text-transform: uppercase; border-bottom: 1px solid var(--border-soft); }}
    .diff-content {{ padding: 0.75rem; font-size: 0.85rem; white-space: pre-wrap; }}
    
    .zen-exit {{ position: absolute; top: 1rem; left: 50%; transform: translateX(-50%); padding: 0.5rem 1rem; border-radius: 999px; background: rgba(255,255,255,0.1); backdrop-filter: blur(8px); border: 1px solid rgba(255,255,255,0.2); color: white; cursor: pointer; z-index: 200; display: none; opacity: 0; transition: 0.3s; font-size: 0.8rem; }}
    body.zen-mode .zen-exit {{ display: block; opacity: 1; }}
    .zen-exit:hover {{ background: rgba(255,255,255,0.2); }}

    /* Keyboard Shortcuts Overlay */
    .kbd-hints {{ position: absolute; bottom: 1rem; left: 50%; transform: translateX(-50%); display: flex; gap: 1rem; background: var(--surface); backdrop-filter: blur(24px); border: 1px solid var(--border-soft); padding: 0.5rem 1rem; border-radius: 999px; z-index: 100; box-shadow: var(--shadow-float); transition: 0.4s; }}
    body.zen-mode .kbd-hints {{ opacity: 0; pointer-events: none; }}
    .kbd-hint {{ font-size: 0.75rem; color: var(--muted); display: flex; align-items: center; gap: 0.4rem; }}
    kbd {{ background: var(--surface-solid); border: 1px solid var(--border-soft); border-radius: 4px; padding: 0.1rem 0.4rem; font-family: monospace; font-weight: 600; color: var(--ink); }}
  </style>
</head>
<body>
  <header class="slim-header" id="header">
    <div class="header-left">
      <button id="btn-nav" class="icon-btn">☰</button>
      <div style="font-size:0.9rem; font-weight:500;">Review Run <span style="color:#6b7280; margin-left:0.2rem">/ <span id="r-title">...</span></span></div>
    </div>
    <div class="header-right">
      <div id="dl-group" style="display:flex; gap:0.5rem; margin-right:0.5rem;"></div>
      <button id="btn-split" class="pill-btn">Split View</button>
      <button id="btn-zen" class="primary-btn">Zen Mode</button>
    </div>
  </header>

  <main class="stage">
    <iframe id="frame"></iframe>
    <aside class="floating-navigator">
      <div class="nav-search">
        <input id="search" type="search" placeholder="Search changes... (/)" />
        <div class="dist-bar" id="dist-bar"></div>
      </div>
      <div id="filter-row" class="filters-scroll"></div>
      <div id="detail-list" class="change-list"></div>
    </aside>
    <div class="floating-inspector" id="inspector">
      <div class="insp-head"><h3 id="insp-title">Change</h3><button id="close-insp" class="icon-btn" style="width:24px;height:24px;">✕</button></div>
      <div id="insp-body" class="insp-body"></div>
    </div>
    <button id="btn-exit-zen" class="zen-exit">Exit Zen Mode (Esc)</button>
    <div class="kbd-hints">
      <div class="kbd-hint"><kbd>J</kbd> / <kbd>K</kbd> Prev/Next</div>
      <div class="kbd-hint"><kbd>A</kbd> Accept</div>
      <div class="kbd-hint"><kbd>R</kbd> Reject</div>
      <div class="kbd-hint"><kbd>U</kbd> Undo</div>
      <div class="kbd-hint"><kbd>S</kbd> Split View</div>
      <div class="kbd-hint"><kbd>/</kbd> Search</div>
      <div class="kbd-hint"><kbd>Z</kbd> Zen</div>
      <div class="kbd-hint"><kbd>B</kbd> Nav</div>
    </div>
  </main>
  
  <script>
    const runId = {json.dumps(run_id)};
    const s = {{ meta: null, filter: "changed", q: "", sel: null, navOff: false, zen: false, insp: false, split: false, iframe: null }};
    const D = document;
    const body = D.body, frame = D.getElementById("frame"), nList = D.getElementById("detail-list");
    const insp = D.getElementById("inspector"), filterRow = D.getElementById("filter-row"), search = D.getElementById("search");
    
    // Commands
    function z() {{ s.zen = !s.zen; body.className = s.zen ? "zen-mode" : (s.navOff ? "nav-hidden" : ""); if(s.zen) insp.classList.remove("visible"); else if(s.insp) insp.classList.add("visible"); }}
    function n() {{ if(s.zen) z(); s.navOff = !s.navOff; body.classList.toggle("nav-hidden", s.navOff); }}
    function sToggle() {{
       s.split = !s.split;
       if (s.iframe) {{ s.iframe.body.className = s.split ? "view-split" : "view-inline"; }}
       D.getElementById("btn-split").textContent = s.split ? "Inline View" : "Split View";
       if (s.sel) syncFrame(s.sel);
    }}
    
    D.getElementById("btn-zen").onclick = z; D.getElementById("btn-exit-zen").onclick = z; D.getElementById("btn-nav").onclick = n;
    D.getElementById("btn-split").onclick = sToggle;
    D.getElementById("close-insp").onclick = () => {{ s.insp = false; insp.classList.remove("visible"); }};
    
    // Iframe Scroll Sync
    function syncFrame(idx) {{
      if(!s.iframe) return;
      const el = s.iframe.getElementById("section-" + idx);
      if(el) {{
        el.scrollIntoView({{behavior: "smooth", block: "center"}});
        // Add a visual flash
        const origBg = el.style.backgroundColor;
        el.style.backgroundColor = "rgba(255, 230, 0, 0.4)";
        setTimeout(() => el.style.backgroundColor = origBg, 1500);
      }}
    }}
    
    frame.onload = () => {{
      s.iframe = frame.contentDocument;
      if (s.meta && s.meta.decisions) {{
          for (let idx of Object.keys(s.meta.decisions)) {{
              let el = s.iframe.getElementById("section-" + idx);
              if (el) el.classList.add("decided-" + s.meta.decisions[idx]);
          }}
      }}
      // Observe iframe scrolling back to parent
      const obs = new IntersectionObserver((ents) => {{
        // Find the majority visible element
        let best = null, maxR = 0;
        ents.forEach(e => {{ if(e.isIntersecting && e.intersectionRatio > maxR) {{ maxR = e.intersectionRatio; best = e.target; }} }});
        if(best && best.dataset.sectionIndex) {{
           // s.sel = Number(best.dataset.sectionIndex);
           // We could auto-select here, but better to just gently indicate visually avoiding scroll loops
        }}
      }}, {{root: s.iframe, threshold: 0.5}});
      const docs = s.iframe.querySelectorAll("[data-section-index]");
      docs.forEach(d => obs.observe(d));
    }};
    
    function slug(v) {{ return String(v||"").toLowerCase(); }}
    function ex(sec) {{ const v = sec.revised_text || sec.original_text || ""; return v.length > 80 ? v.slice(0, 80)+"…" : v; }}
    function enc(v) {{ return String(v).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;"); }}
    
    function fSec() {{
      if(!s.meta) return [];
      const trm = slug(s.q).trim();
      return s.meta.sections.filter(x => {{
        const bp = s.filter==="all" ? true : s.filter==="changed" ? x.is_changed : x.kind===s.filter;
        if(!bp) return false;
        if(!trm) return true;
        return slug([x.label, x.kind, x.original_text, x.revised_text].join(" ")).includes(trm);
      }});
    }}
    
    function renderInsp() {{
      const secs = fSec(); const a = secs.find(x => x.index === s.sel);
      if(!a) {{ s.insp = false; insp.classList.remove("visible"); return; }}
      s.insp = true; if(!s.zen) insp.classList.add("visible");
      D.getElementById("insp-title").textContent = a.kind_label || a.kind;
      D.getElementById("insp-body").innerHTML = `
        <div style="font-size:0.75rem; color:var(--muted); margin-bottom:1rem">${{enc(a.label)}}</div>
        <div class="diff-block"><div class="diff-hdr">Original</div><div class="diff-content">${{enc(a.original_text||"—")}}</div></div>
        <div class="diff-block"><div class="diff-hdr">Revised</div><div class="diff-content">${{enc(a.revised_text||"—")}}</div></div>
      `;
      syncFrame(a.index);
    }}
    
    function setSel(idx) {{
      s.sel = idx; renderSections(); renderInsp();
      const card = D.querySelector(`.detail-card[data-index="${{idx}}"]`);
      if(card) card.scrollIntoView({{behavior: "smooth", block: "nearest"}});
    }}
    
    function renderSections() {{
      const secs = fSec();
      if(!secs.length) {{ nList.innerHTML = '<div style="padding: 2rem 1rem; text-align:center; color:gray;">Empty</div>'; return; }}
      nList.innerHTML = secs.map(x => `
        <div class="detail-card ${{x.index === s.sel ? 'active':''}} ${{s.meta.decisions && s.meta.decisions[x.index] ? 'decision-'+s.meta.decisions[x.index] : ''}}" data-index="${{x.index}}">
          <div class="detail-title">${{enc(x.label||"Section "+x.index)}}</div>
          <div class="detail-meta">${{x.kind_label}} · sec ${{x.index}}</div>
          <div class="detail-excerpt">${{enc(ex(x))}}</div>
        </div>
      `).join("");
      nList.querySelectorAll(".detail-card").forEach(c => c.onclick = () => setSel(Number(c.dataset.index)));
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
    
    function init(m) {{
      s.meta = m; D.getElementById("r-title").textContent = m.original_name + " → " + m.revised_name;
      frame.src = m.preview_url;
      
      const dlGroup = D.getElementById("dl-group");
      if (m.downloads) {{
         dlGroup.innerHTML = Object.entries(m.downloads).map(([fmt, url]) => 
            `<a href="${{url}}" class="dl-pill" target="_blank" download>Download ${{fmt.toUpperCase()}}</a>`
         ).join("");
      }}

      const c = {{all:m.sections.length, changed:0, move:0, replace:0, insert:0, delete:0}};
      m.sections.forEach(x => {{ if(x.is_changed) c.changed++; if(c[x.kind]!==undefined) c[x.kind]++; }});
      buildDistBar(c);
      
      const flts = [["changed", "Changes", c.changed], ["move", "Moves", c.move], ["replace", "Replaced", c.replace], ["insert", "Inserts", c.insert], ["delete", "Deletes", c.delete], ["all", "All", c.all]];
      filterRow.innerHTML = flts.map(x => `<button class="filter-btn ${{s.filter===x[0]?'active':''}}" data-f="${{x[0]}}">${{x[1]}} (${{x[2]}})</button>`).join("");
      filterRow.querySelectorAll(".filter-btn").forEach(btn => btn.onclick = () => {{ s.filter = btn.dataset.f; init(m); renderSections(); }});
      
      renderSections();
    }}
    
    D.addEventListener('keydown', e => {{
      if(e.target.tagName==="INPUT") {{ if(e.key==="Escape") e.target.blur(); return; }}
      if(e.key === "z" || e.key === "Z") z();
      if(e.key === "Escape" && s.zen) z();
      if(e.key === "b" || e.key === "B") n();
      if(e.key === "s" || e.key === "S") sToggle();
      if(e.key === "/") {{ e.preventDefault(); search.focus(); }}
      if(e.key === "a" || e.key === "A") {{ if (s.sel) makeDecision(s.sel, "accept"); }}
      if(e.key === "r" || e.key === "R") {{ if (s.sel) makeDecision(s.sel, "reject"); }}
      if(e.key === "u" || e.key === "U") {{ if (s.sel) makeDecision(s.sel, "pending"); }}
      if(e.key === "j" || e.key === "J" || e.key === "ArrowDown") {{
        const sc = fSec(); if(!sc.length) return;
        let c = sc.findIndex(x => x.index === s.sel);
        if(c < 0 || c >= sc.length-1) setSel(sc[0].index);
        else setSel(sc[c+1].index);
      }}
      if(e.key === "k" || e.key === "K" || e.key === "ArrowUp") {{
        const sc = fSec(); if(!sc.length) return;
        let c = sc.findIndex(x => x.index === s.sel);
        if(c <= 0) setSel(sc[sc.length-1].index);
        else setSel(sc[c-1].index);
      }}
    }});

    function makeDecision(idx, decision) {{
      if(!s.meta) return;
      s.meta.decisions = s.meta.decisions || {{}};
      if (decision === 'pending') delete s.meta.decisions[idx];
      else s.meta.decisions[idx] = decision;
      
      if (s.iframe) {{
        const el = s.iframe.getElementById("section-" + idx);
        if (el) {{
          el.classList.remove("decided-accept", "decided-reject");
          if (decision !== "pending") el.classList.add("decided-" + decision);
        }}
      }}
      
      const card = D.querySelector(`.detail-card[data-index="${{idx}}"]`);
      if (card) {{
        card.classList.remove("decision-accept", "decision-reject");
        if (decision !== "pending") card.classList.add("decision-" + decision);
      }}
      
      fetch(`/api/runs/${{encodeURIComponent(runId)}}/decisions`, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{section_index: idx, decision: decision}})
      }});
      
      if (decision !== 'pending') {{
         setTimeout(() => {{
            const sc = fSec();
            const c = sc.findIndex(x => x.index === s.sel);
            if (c >= 0 && c < sc.length - 1) setSel(sc[c+1].index);
         }}, 150);
      }}
    }}
    
    search.oninput = () => {{ s.q = search.value; renderSections(); }};
    
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
