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
from urllib.parse import parse_qs, quote, unquote, urlparse

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
            # v2 UI is now the default. Opt out (legacy shell) with ?v=1.
            query = parse_qs(parsed.query or "")
            if query.get("v", [""])[0] == "1":
                _send_html(handler, build_review_shell(parts[1]))
            else:
                _send_html(handler, build_review_shell_v2(parts[1]))
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
                # v2 review shell needs word-level diff tokens to render inline
                # ins/del spans without re-opening the DOCX client-side.
                "combined_tokens": [
                    {"text": tok.text, "kind": tok.kind}
                    for tok in section.combined_tokens
                ],
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
    :root {{
      --bg: #ffffff;
      --canvas: #f7f7f8;
      --surface: #ffffff;
      --border: #e4e4e7;
      --border-strong: #d4d4d8;
      --text: #18181b;
      --text-muted: #71717a;
      --text-subtle: #a1a1aa;
      --accent: #18181b;
      --accent-hover: #000000;
      --ok: #15803d;
      --warn: #b45309;
      --bad: #b91c1c;
      --focus: #2563eb;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas, monospace;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      font-family: var(--font-sans);
      font-size: 14px;
      line-height: 1.5;
      color: var(--text);
      background: var(--canvas);
      min-height: 100vh;
      display: flex;
      justify-content: center;
      padding: 40px 24px 80px;
      -webkit-font-smoothing: antialiased;
    }}
    .shell {{
      width: 100%;
      max-width: 720px;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }}
    header {{ margin-bottom: 4px; }}
    h1 {{
      font-size: 20px;
      font-weight: 600;
      letter-spacing: -0.01em;
      margin: 0 0 4px;
      color: var(--text);
    }}
    p.subtitle {{
      font-size: 13px;
      color: var(--text-muted);
      margin: 0;
    }}
    /* Decorative elements (hero-strip, workflow-rail, live-deck) kept hidden */
    .hero-strip, .workflow-rail, .live-deck {{ display: none; }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 24px;
    }}
    form {{ display: flex; flex-direction: column; gap: 24px; }}
    .form-step {{ display: flex; flex-direction: column; gap: 10px; }}
    .section-title {{
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-muted);
      margin-bottom: 2px;
    }}
    .mode-row {{ display: flex; gap: 0; border: 1px solid var(--border); border-radius: 4px; overflow: hidden; width: fit-content; }}
    .mode-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 14px;
      font-size: 13px;
      font-weight: 500;
      color: var(--text-muted);
      cursor: pointer;
      border-right: 1px solid var(--border);
      transition: background 100ms, color 100ms;
    }}
    .mode-pill:last-child {{ border-right: none; }}
    .mode-pill input {{ display: none; }}
    .mode-pill:has(input:checked) {{ background: var(--text); color: #fff; }}
    .mode-pill:hover:not(:has(input:checked)) {{ background: var(--canvas); color: var(--text); }}
    .mode-summary {{ font-size: 12px; color: var(--text-muted); }}
    .upload-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .upload-zone {{
      position: relative;
      border: 1px dashed var(--border-strong);
      border-radius: 4px;
      padding: 20px 16px;
      background: var(--canvas);
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 4px;
      transition: border-color 100ms, background 100ms;
    }}
    .upload-zone:hover {{ border-color: var(--text-muted); }}
    .upload-zone.dragover {{ border-color: var(--focus); background: #eff6ff; }}
    .upload-zone.has-file {{ border-style: solid; border-color: var(--ok); background: #f0fdf4; }}
    .upload-input {{ position: absolute; inset: 0; opacity: 0; cursor: pointer; }}
    .upload-zone .icon {{
      font-family: var(--font-mono);
      font-size: 11px;
      font-weight: 600;
      color: var(--text-muted);
      letter-spacing: 0.1em;
    }}
    .upload-zone .lbl {{ font-size: 13px; font-weight: 600; color: var(--text); }}
    .upload-zone .sub {{ font-size: 12px; color: var(--text-muted); }}
    .upload-zone .fname {{
      font-size: 12px;
      color: var(--ok);
      font-family: var(--font-mono);
      display: none;
      word-break: break-all;
    }}
    .upload-zone.has-file .fname {{ display: block; }}
    .file-list {{
      margin: 6px 0 0;
      padding: 0;
      list-style: none;
      font-size: 11px;
      font-family: var(--font-mono);
      color: var(--text-muted);
      display: none;
      max-height: 120px;
      overflow-y: auto;
    }}
    .file-list.show {{ display: block; }}
    .file-list li {{ padding: 2px 0; }}
    .field-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .field {{ display: flex; flex-direction: column; gap: 4px; }}
    .field label {{ font-size: 12px; font-weight: 500; color: var(--text-muted); }}
    input[type="text"], select {{
      font-family: var(--font-sans);
      font-size: 13px;
      padding: 6px 10px;
      border: 1px solid var(--border-strong);
      border-radius: 4px;
      background: var(--surface);
      color: var(--text);
      transition: border-color 100ms;
    }}
    input[type="text"]:focus, select:focus {{ outline: none; border-color: var(--focus); box-shadow: 0 0 0 2px rgba(37,99,235,0.15); }}
    .pill-group {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .check-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 10px;
      border: 1px solid var(--border);
      border-radius: 4px;
      background: var(--surface);
      font-size: 12px;
      color: var(--text-muted);
      cursor: pointer;
      user-select: none;
      transition: background 100ms, border-color 100ms, color 100ms;
    }}
    .check-pill input {{ margin: 0; accent-color: var(--text); }}
    .check-pill:has(input:checked) {{ background: var(--text); border-color: var(--text); color: #fff; }}
    .check-pill:hover:not(:has(input:checked)) {{ border-color: var(--text-muted); color: var(--text); }}
    details {{ font-size: 12px; }}
    details summary {{
      cursor: pointer;
      color: var(--text-muted);
      padding: 4px 0;
      font-weight: 500;
      list-style: none;
    }}
    details summary:hover {{ color: var(--text); }}
    details summary::marker, details summary::-webkit-details-marker {{ display: none; }}
    .btn {{
      font-family: var(--font-sans);
      font-size: 13px;
      font-weight: 500;
      padding: 8px 16px;
      background: var(--accent);
      color: #fff;
      border: 1px solid var(--accent);
      border-radius: 4px;
      cursor: pointer;
      transition: background 100ms, border-color 100ms;
      align-self: flex-start;
    }}
    .btn:hover:not(:disabled) {{ background: var(--accent-hover); border-color: var(--accent-hover); }}
    .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    #status {{
      display: none;
      padding: 8px 12px;
      border-radius: 4px;
      font-size: 13px;
      border: 1px solid transparent;
    }}
    #status.show {{ display: block; }}
    #status.tone-info, #status.tone-working {{ background: #eff6ff; color: #1e40af; border-color: #bfdbfe; }}
    #status.tone-success {{ background: #f0fdf4; color: var(--ok); border-color: #bbf7d0; }}
    #status.tone-warning {{ background: #fffbeb; color: var(--warn); border-color: #fde68a; }}
    #status.tone-error {{ background: #fef2f2; color: var(--bad); border-color: #fecaca; }}
    /* Batch */
    .batch-panel {{
      display: none;
      margin-top: 8px;
      padding: 16px;
      border: 1px solid var(--border);
      border-radius: 4px;
      background: var(--canvas);
      flex-direction: column;
      gap: 10px;
    }}
    .batch-panel.show {{ display: flex; }}
    .batch-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }}
    .batch-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; color: var(--text-muted); }}
    .batch-summary {{ font-size: 12px; color: var(--text); }}
    .batch-progress {{ height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }}
    .batch-progress-fill {{ height: 100%; background: var(--text); width: 0%; transition: width 200ms; }}
    .batch-progress-label {{ font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }}
    .batch-results {{ margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 2px; max-height: 300px; overflow-y: auto; }}
    .batch-row {{
      display: grid;
      grid-template-columns: 24px 1fr auto auto;
      gap: 10px;
      align-items: center;
      padding: 6px 8px;
      font-size: 12px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 3px;
    }}
    .batch-row.running {{ border-color: var(--focus); }}
    .batch-row.done {{ border-color: var(--ok); }}
    .batch-row.failed {{ border-color: var(--bad); }}
    .batch-index {{ font-family: var(--font-mono); color: var(--text-muted); font-size: 11px; }}
    .batch-name {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .batch-state {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-muted); }}
    .batch-row.done .batch-state {{ color: var(--ok); }}
    .batch-row.failed .batch-state {{ color: var(--bad); }}
    .batch-row.running .batch-state {{ color: var(--focus); }}
    .batch-link a {{ color: var(--focus); text-decoration: none; font-size: 11px; }}
    .batch-link a:hover {{ text-decoration: underline; }}
    .batch-empty {{ color: var(--text-muted); font-size: 12px; padding: 8px 0; }}
    .batch-actions {{ display: flex; gap: 8px; }}
    .batch-btn {{
      font-family: var(--font-sans);
      font-size: 12px;
      padding: 5px 10px;
      border: 1px solid var(--border-strong);
      background: var(--surface);
      color: var(--text);
      border-radius: 4px;
      cursor: pointer;
    }}
    .batch-btn:hover {{ background: var(--canvas); }}
    .batch-open-row {{ display: none; align-items: center; gap: 8px; padding-top: 4px; border-top: 1px solid var(--border); }}
    .batch-open-row.show {{ display: flex; }}
    .batch-open-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); font-weight: 600; }}
    .batch-open-select {{ flex: 1; min-width: 0; font-size: 12px; padding: 4px 8px; border: 1px solid var(--border-strong); border-radius: 4px; }}
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <h1>Blackline Studio</h1>
      <p class="subtitle">Compare one-to-one or run a queued batch against one baseline draft.</p>
    </header>
    <div class="hero-strip" aria-hidden="true">
      <span class="hero-chip">Local-first processing</span>
      <span class="hero-chip">Tracked-change exports</span>
      <span class="hero-chip">Version-switch batch review</span>
    </div>
    <div class="workflow-rail" aria-hidden="true">
      <div class="workflow-step"><strong>1</strong><span>Upload drafts</span></div>
      <div class="workflow-step"><strong>2</strong><span>Configure settings</span></div>
      <div class="workflow-step"><strong>3</strong><span>Review and switch versions</span></div>
    </div>
    <section class="live-deck" id="live-deck" aria-label="Live Workflow Metrics">
      <article class="live-metric">
        <span class="metric-label">Mode</span>
        <strong id="metric-mode" class="metric-value">Single</strong>
        <span id="metric-mode-meta" class="metric-meta">One revised draft</span>
      </article>
      <article class="live-metric">
        <span class="metric-label">Queue Size</span>
        <strong id="metric-queue" class="metric-value">0</strong>
        <span id="metric-queue-meta" class="metric-meta">No revised draft selected</span>
      </article>
      <article class="live-metric">
        <span class="metric-label">Outputs</span>
        <strong id="metric-formats" class="metric-value">0</strong>
        <span id="metric-formats-meta" class="metric-meta">Select at least one format</span>
      </article>
      <article class="live-metric">
        <span class="metric-label">Readiness</span>
        <strong id="metric-ready" class="metric-value">Not Ready</strong>
        <span id="metric-ready-meta" class="metric-meta">Missing input documents</span>
        <div class="metric-track"><span id="metric-ready-fill" class="metric-fill"></span></div>
      </article>
    </section>
    <div class="card">
      <form id="compare-form">
        <section class="form-step">
          <div class="section-title">Step 1: Upload Documents</div>
          <div class="mode-row" id="compare-mode-row">
            <label class="mode-pill"><input type="radio" name="compare_mode" id="mode-single" value="single" checked /> <span>Single Review</span></label>
            <label class="mode-pill"><input type="radio" name="compare_mode" id="mode-batch" value="batch" /> <span>Batch Queue</span></label>
          </div>
          <div id="mode-summary" class="mode-summary">Single review compares one original and one revised draft.</div>
          <div class="upload-grid">
            <label class="upload-zone" id="z-original">
              <input class="upload-input" type="file" id="original" name="original" required />
              <div class="icon">O</div>
              <div class="lbl">Original Draft</div>
              <div class="sub">Baseline (.docx, .txt)</div>
              <div class="fname" id="n-original">Selected</div>
            </label>
            <label class="upload-zone" id="z-revised">
              <input class="upload-input" type="file" id="revised" name="revised" required />
              <div class="icon">R</div>
              <div class="lbl">Revised Draft</div>
              <div class="sub" id="revised-hint">Latest edits (.docx, .txt)</div>
              <div class="fname" id="n-revised">Selected</div>
              <ul class="file-list" id="revised-list"></ul>
            </label>
          </div>
        </section>

        <section class="form-step">
          <div class="section-title">Step 2: Settings</div>
          <div class="field-grid">
            <div class="field"><label>Comparison Profile</label><select name="profile">{profile_options}</select></div>
            <div class="field"><label>Output Name</label><input type="text" name="base_name" value="blackline_report" /></div>
          </div>
        </section>

        <section class="form-step">
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
        </section>

        <button class="btn" type="submit" id="submit-btn">Generate Review Run</button>
        <div id="status"></div>
        <section class="batch-panel" id="batch-panel">
          <div class="batch-head">
            <div class="batch-label">Batch Queue Tracker</div>
            <div class="batch-summary" id="batch-summary">No batch started.</div>
          </div>
          <div class="batch-progress"><div class="batch-progress-fill" id="batch-progress-fill"></div></div>
          <div class="batch-progress-label" id="batch-progress-label">Waiting for queue run.</div>
          <ol class="batch-results" id="batch-results"></ol>
          <div class="batch-actions">
            <button type="button" class="batch-btn" id="batch-retry-failed" hidden>Retry Failed Items</button>
          </div>
          <div class="batch-open-row" id="batch-open-row">
            <span class="batch-open-label">Switch Version</span>
            <select id="batch-open-select" class="batch-open-select" aria-label="Switch revised version"></select>
            <button type="button" class="batch-btn" id="batch-open-btn">Open</button>
          </div>
        </section>
      </form>
    </div>
  </div>
  <script>
    const form = document.getElementById("compare-form");
    const statusNode = document.getElementById("status");
    const submitBtn = document.getElementById("submit-btn");
    const originalInput = document.getElementById("original");
    const revisedInput = document.getElementById("revised");
    const revisedHint = document.getElementById("revised-hint");
    const revisedList = document.getElementById("revised-list");
    const modeInputs = Array.from(document.querySelectorAll("input[name='compare_mode']"));
    const modeSummary = document.getElementById("mode-summary");
    const profileSelect = form.querySelector("select[name='profile']");
    const formatCheckboxes = Array.from(form.querySelectorAll("input[name='formats']"));
    const metricMode = document.getElementById("metric-mode");
    const metricModeMeta = document.getElementById("metric-mode-meta");
    const metricQueue = document.getElementById("metric-queue");
    const metricQueueMeta = document.getElementById("metric-queue-meta");
    const metricFormats = document.getElementById("metric-formats");
    const metricFormatsMeta = document.getElementById("metric-formats-meta");
    const metricReady = document.getElementById("metric-ready");
    const metricReadyMeta = document.getElementById("metric-ready-meta");
    const metricReadyFill = document.getElementById("metric-ready-fill");
    const batchPanel = document.getElementById("batch-panel");
    const batchSummary = document.getElementById("batch-summary");
    const batchProgressFill = document.getElementById("batch-progress-fill");
    const batchProgressLabel = document.getElementById("batch-progress-label");
    const batchResults = document.getElementById("batch-results");
    const retryFailedBtn = document.getElementById("batch-retry-failed");
    const batchOpenRow = document.getElementById("batch-open-row");
    const batchOpenSelect = document.getElementById("batch-open-select");
    const batchOpenBtn = document.getElementById("batch-open-btn");
    const BATCH_HISTORY_KEY = "blackline_batch_history_v1";
    let lastBatchContext = null;
    let isBusy = false;

    function encodeHtml(value) {{
      return String(value).replace(/[&<>"']/g, (char) => ({{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }}[char]));
    }}

    function setMetricValue(node, value) {{
      if (!node) return;
      const normalized = String(value);
      if (node.dataset.metricValue === normalized) return;
      node.dataset.metricValue = normalized;
      node.textContent = normalized;
      node.classList.remove("pulse");
      void node.offsetWidth;
      node.classList.add("pulse");
    }}

    function summarizeFormats(formats) {{
      if (!formats.length) return "Select at least one format";
      if (formats.length <= 2) return formats.map((fmt) => fmt.toUpperCase()).join(" + ");
      return `${{formats.slice(0, 2).map((fmt) => fmt.toUpperCase()).join(", ")}} +${{formats.length - 2}}`;
    }}

    function updateLiveMetrics() {{
      const mode = getMode();
      const revisedCount = Array.from(revisedInput.files || []).length;
      const formatValues = formatCheckboxes.filter((item) => item.checked).map((item) => item.value);
      const hasOriginal = !!(originalInput.files && originalInput.files[0]);
      const hasRevised = revisedCount > 0;
      const readinessScore = Number(hasOriginal) + Number(hasRevised) + Number(formatValues.length > 0);
      const readinessPct = Math.round((readinessScore / 3) * 100);
      const queueText = revisedCount ? String(revisedCount) : "0";
      const queueMeta = mode === "batch"
        ? (revisedCount ? `${{revisedCount}} revised draft${{revisedCount === 1 ? "" : "s"}} queued` : "Add revised drafts to start queue")
        : (revisedCount ? "Single revised draft selected" : "Select one revised draft");
      const selectedProfile = profileSelect ? String(profileSelect.value || "default") : "default";

      setMetricValue(metricMode, mode === "batch" ? "Batch" : "Single");
      if (metricModeMeta) {{
        metricModeMeta.textContent = mode === "batch"
          ? "One original against many revised drafts"
          : "One original versus one revised draft";
      }}
      setMetricValue(metricQueue, queueText);
      if (metricQueueMeta) metricQueueMeta.textContent = queueMeta;
      setMetricValue(metricFormats, String(formatValues.length));
      if (metricFormatsMeta) metricFormatsMeta.textContent = summarizeFormats(formatValues);

      const readyText = isBusy ? "Processing" : (readinessScore === 3 ? "Ready" : readinessScore === 2 ? "Almost Ready" : "Not Ready");
      setMetricValue(metricReady, readyText);
      if (metricReadyMeta) {{
        if (isBusy) {{
          metricReadyMeta.textContent = "Generating comparison runs";
        }} else if (readinessScore === 3) {{
          metricReadyMeta.textContent = `Profile: ${{selectedProfile.replace(/[_-]+/g, " ")}}`;
        }} else if (!hasOriginal && !hasRevised) {{
          metricReadyMeta.textContent = "Upload original and revised drafts";
        }} else if (!hasOriginal) {{
          metricReadyMeta.textContent = "Original draft is missing";
        }} else if (!hasRevised) {{
          metricReadyMeta.textContent = "Revised draft is missing";
        }} else {{
          metricReadyMeta.textContent = "Choose at least one output format";
        }}
      }}
      if (metricReadyFill) {{
        metricReadyFill.style.width = `${{Math.max(readinessScore ? 12 : 0, readinessPct)}}%`;
      }}
    }}

    function getMode() {{
      const current = modeInputs.find((item) => item.checked);
      return current ? current.value : "single";
    }}

    function extractRunIdFromUrl(runUrl) {{
      const match = String(runUrl || "").match(/\\/runs\\/([A-Za-z0-9-]+)/);
      return match ? match[1] : "";
    }}

    function loadBatchHistory() {{
      try {{
        const raw = window.localStorage.getItem(BATCH_HISTORY_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed : [];
      }} catch (_error) {{
        return [];
      }}
    }}

    function saveBatchHistory(history) {{
      try {{
        const trimmed = Array.isArray(history) ? history.slice(0, 10) : [];
        window.localStorage.setItem(BATCH_HISTORY_KEY, JSON.stringify(trimmed));
      }} catch (_error) {{
        // Ignore local storage issues so compare flow is never blocked.
      }}
    }}

    function persistBatchSession(common, originalFile, rows) {{
      const items = rows
        .filter((row) => row.status === "done" && row.run_url)
        .map((row, idx) => ({{
          index: idx + 1,
          run_id: extractRunIdFromUrl(row.run_url),
          run_url: row.run_url,
          revised_name: row.file.name
        }}))
        .filter((item) => item.run_id);
      if (!items.length) return null;
      const session = {{
        session_id: `${{Date.now()}}-${{Math.random().toString(36).slice(2, 8)}}`,
        created_at: new Date().toISOString(),
        original_name: originalFile.name || "",
        base_name: common.baseName || "",
        profile: common.profile || "",
        items
      }};
      const history = loadBatchHistory().filter((entry) => entry && Array.isArray(entry.items));
      history.unshift(session);
      saveBatchHistory(history);
      return session;
    }}

    function renderBatchOpenPicker(session) {{
      if (!session || !Array.isArray(session.items) || session.items.length < 2) {{
        batchOpenRow.classList.remove("show");
        batchOpenSelect.innerHTML = "";
        return;
      }}
      const options = session.items.map((item) => `
        <option value="${{encodeHtml(item.run_id)}}">${{item.index}}. ${{encodeHtml(item.revised_name)}}</option>
      `).join("");
      batchOpenSelect.innerHTML = options;
      batchOpenRow.classList.add("show");
    }}

    function setStatus(message, tone = "info") {{
      if (!message) {{
        statusNode.textContent = "";
        statusNode.className = "";
        return;
      }}
      if (typeof tone === "boolean") {{
        tone = tone ? "error" : "info";
      }}
      const normalizedTone = new Set(["info", "working", "warning", "error", "success"]).has(tone) ? tone : "info";
      statusNode.textContent = message;
      statusNode.className = `show tone-${{normalizedTone}}`;
    }}

    function sanitizeStem(name) {{
      return String(name || "file")
        .replace(/\\.[^.]*$/, "")
        .replace(/[^A-Za-z0-9_-]+/g, "_")
        .replace(/^_+|_+$/g, "")
        .slice(0, 38) || "file";
    }}

    function deriveBaseName(baseName, revisedName, index, total) {{
      if (total <= 1) return baseName;
      const seq = String(index + 1).padStart(2, "0");
      return `${{baseName}}_${{seq}}_${{sanitizeStem(revisedName)}}`;
    }}

    function updateUploadBadge(input, zoneId, nameId) {{
      const zone = document.getElementById(zoneId);
      const name = document.getElementById(nameId);
      const files = Array.from(input.files || []);
      if (!files.length) {{
        zone.classList.remove("has-file");
        name.textContent = "Selected";
        return;
      }}
      zone.classList.add("has-file");
      name.textContent = files.length === 1 ? files[0].name : `${{files.length}} files selected`;
    }}

    function updateRevisedList() {{
      const files = Array.from(revisedInput.files || []);
      const isBatch = getMode() === "batch";
      if (!isBatch || files.length <= 1) {{
        revisedList.classList.remove("show");
        revisedList.innerHTML = "";
        return;
      }}
      revisedList.innerHTML = files.map((file, idx) => `<li>${{idx + 1}}. ${{encodeHtml(file.name)}}</li>`).join("");
      revisedList.classList.add("show");
    }}

    function updateModeUi() {{
      const isBatch = getMode() === "batch";
      revisedInput.multiple = isBatch;
      revisedHint.textContent = isBatch ? "Queue one or more revised drafts (.docx, .txt)" : "Latest edits (.docx, .txt)";
      submitBtn.textContent = isBatch ? "Run Batch Queue" : "Generate Review Run";
      if (modeSummary) {{
        modeSummary.textContent = isBatch
          ? "Batch queue runs one original against multiple revised drafts, then lets you switch versions instantly."
          : "Single review compares one original and one revised draft.";
      }}
      if (!isBatch) {{
        batchPanel.classList.remove("show");
        retryFailedBtn.hidden = true;
        batchOpenRow.classList.remove("show");
      }}
      updateUploadBadge(revisedInput, "z-revised", "n-revised");
      updateRevisedList();
      updateLiveMetrics();
    }}

    function attachDrop(zoneId, input) {{
      const zone = document.getElementById(zoneId);
      zone.addEventListener("dragover", (event) => {{
        event.preventDefault();
        zone.classList.add("dragover");
      }});
      zone.addEventListener("dragleave", (event) => {{
        event.preventDefault();
        zone.classList.remove("dragover");
      }});
      zone.addEventListener("drop", (event) => {{
        event.preventDefault();
        zone.classList.remove("dragover");
        if (!event.dataTransfer.files.length) return;
        input.files = event.dataTransfer.files;
        if (input === revisedInput) {{
          updateUploadBadge(revisedInput, "z-revised", "n-revised");
          updateRevisedList();
        }} else {{
          updateUploadBadge(originalInput, "z-original", "n-original");
        }}
        updateLiveMetrics();
      }});
    }}

    async function fileToBase64(file) {{
      const buffer = await file.arrayBuffer();
      let binary = "";
      const bytes = new Uint8Array(buffer);
      for (let i = 0; i < bytes.length; i += 0x8000) {{
        binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
      }}
      return btoa(binary);
    }}

    function readSettings(formData) {{
      const formats = formData.getAll("formats");
      if (!formats.length) throw new Error("Select at least one output format.");
      return {{
        baseName: String(formData.get("base_name") || "blackline_report"),
        profile: String(formData.get("profile") || "default"),
        formats,
        strict_legal: formData.get("strict_legal") === "on",
        ignore_case: formData.get("ignore_case") === "on",
        ignore_whitespace: formData.get("ignore_whitespace") === "on",
        ignore_smart_punctuation: formData.get("ignore_smart_punctuation") === "on",
        ignore_punctuation: formData.get("ignore_punctuation") === "on",
        ignore_numbering: formData.get("ignore_numbering") === "on",
        detect_moves: formData.get("detect_moves") === "on"
      }};
    }}

    async function buildPayload(common, originalFile, revisedFile, index, total) {{
      return {{
        original_name: originalFile.name,
        original_content: await fileToBase64(originalFile),
        revised_name: revisedFile.name,
        revised_content: await fileToBase64(revisedFile),
        base_name: deriveBaseName(common.baseName, revisedFile.name, index, total),
        profile: common.profile,
        formats: common.formats,
        strict_legal: common.strict_legal,
        ignore_case: common.ignore_case,
        ignore_whitespace: common.ignore_whitespace,
        ignore_smart_punctuation: common.ignore_smart_punctuation,
        ignore_punctuation: common.ignore_punctuation,
        ignore_numbering: common.ignore_numbering,
        detect_moves: common.detect_moves
      }};
    }}

    async function requestCompare(payload) {{
      const response = await fetch("/api/compare", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload)
      }});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Comparison failed.");
      return result;
    }}

    function renderBatchRows(rows) {{
      if (!rows.length) {{
        batchResults.innerHTML = '<li class="batch-empty">Queue items will appear here once processing starts.</li>';
        return;
      }}
      batchResults.innerHTML = rows.map((row, idx) => `
        <li class="batch-row ${{row.status}}">
          <span class="batch-index">${{idx + 1}}</span>
          <span class="batch-name" title="${{encodeHtml(row.file.name)}}">${{encodeHtml(row.file.name)}}</span>
          <span class="batch-state">${{encodeHtml(row.status_label)}}</span>
          <span class="batch-link">${{row.run_url ? `<a href="${{encodeHtml(row.run_url)}}" target="_blank" rel="noopener">Open</a>` : ""}}</span>
        </li>
      `).join("");
    }}

    function updateBatchProgress(done, total) {{
      const pct = total ? Math.round((done / total) * 100) : 0;
      batchProgressFill.style.width = `${{pct}}%`;
      batchProgressLabel.textContent = `${{done}} / ${{total}} processed`;
    }}

    async function runBatch(common, originalFile, revisedFiles) {{
      batchPanel.classList.add("show");
      const rows = revisedFiles.map((file) => ({{
        file,
        status: "pending",
        status_label: "Queued",
        run_url: "",
        error: ""
      }}));
      renderBatchRows(rows);
      updateBatchProgress(0, rows.length);
      retryFailedBtn.hidden = true;
      batchSummary.textContent = `Queued ${{rows.length}} comparisons`;
      let completed = 0;
      for (let i = 0; i < rows.length; i += 1) {{
        rows[i].status = "running";
        rows[i].status_label = "Running";
        renderBatchRows(rows);
        setStatus(`Processing ${{i + 1}} of ${{rows.length}}: ${{rows[i].file.name}}`, "working");
        try {{
          const payload = await buildPayload(common, originalFile, rows[i].file, i, rows.length);
          const result = await requestCompare(payload);
          rows[i].status = "done";
          rows[i].status_label = "Done";
          rows[i].run_url = result.run_url || "";
        }} catch (error) {{
          rows[i].status = "failed";
          rows[i].status_label = "Failed";
          rows[i].error = error && error.message ? error.message : String(error);
        }}
        completed += 1;
        renderBatchRows(rows);
        updateBatchProgress(completed, rows.length);
      }}
      const successCount = rows.filter((row) => row.status === "done").length;
      const failedRows = rows.filter((row) => row.status === "failed");
      const hasFailures = failedRows.length > 0;
      batchSummary.textContent = hasFailures
        ? `${{successCount}} done, ${{failedRows.length}} failed`
        : `All ${{successCount}} comparisons complete`;
      const session = persistBatchSession(common, originalFile, rows);
      renderBatchOpenPicker(session);
      lastBatchContext = {{
        common,
        originalFile,
        failedRows: failedRows.map((row) => row.file)
      }};
      retryFailedBtn.hidden = !hasFailures;
      setStatus(
        hasFailures
          ? `Batch finished with ${{failedRows.length}} failures. Review queue details below.`
          : `Batch complete. ${{successCount}} review runs are ready.`,
        hasFailures ? "warning" : "success"
      );
    }}

    function setFormBusy(busy) {{
      isBusy = busy;
      submitBtn.disabled = busy;
      document.body.classList.toggle("is-processing", busy);
      modeInputs.forEach((input) => {{
        input.disabled = busy;
      }});
      originalInput.disabled = busy;
      revisedInput.disabled = busy;
      Array.from(form.querySelectorAll("input[type='checkbox'], select, input[type='text']")).forEach((input) => {{
        input.disabled = busy;
      }});
      retryFailedBtn.disabled = busy;
      updateLiveMetrics();
    }}

    originalInput.addEventListener("change", () => {{
      updateUploadBadge(originalInput, "z-original", "n-original");
      updateLiveMetrics();
    }});
    revisedInput.addEventListener("change", () => {{
      updateUploadBadge(revisedInput, "z-revised", "n-revised");
      updateRevisedList();
      updateLiveMetrics();
    }});
    modeInputs.forEach((mode) => mode.addEventListener("change", updateModeUi));
    if (profileSelect) profileSelect.addEventListener("change", updateLiveMetrics);
    formatCheckboxes.forEach((checkbox) => checkbox.addEventListener("change", updateLiveMetrics));
    attachDrop("z-original", originalInput);
    attachDrop("z-revised", revisedInput);
    updateModeUi();
    updateLiveMetrics();

    retryFailedBtn.addEventListener("click", async () => {{
      if (isBusy || !lastBatchContext || !lastBatchContext.failedRows.length) return;
      setFormBusy(true);
      try {{
        setStatus("Retrying failed batch items...", "working");
        await runBatch(lastBatchContext.common, lastBatchContext.originalFile, lastBatchContext.failedRows);
      }} finally {{
        setFormBusy(false);
      }}
    }});

    batchOpenBtn.addEventListener("click", () => {{
      const runId = batchOpenSelect.value;
      if (!runId) return;
      window.location.assign(`/runs/${{encodeURIComponent(runId)}}`);
    }});

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      if (isBusy) return;
      setStatus("Processing documents...", "working");
      try {{
        const formData = new FormData(form);
        const originalFile = originalInput.files && originalInput.files[0];
        const revisedFiles = Array.from(revisedInput.files || []);
        if (!originalFile) throw new Error("Select an original file.");
        if (!revisedFiles.length) throw new Error("Select at least one revised file.");
        const common = readSettings(formData);
        setFormBusy(true);
        const isBatch = getMode() === "batch";
        if (!isBatch) {{
          if (revisedFiles.length > 1) throw new Error("Single mode supports one revised file. Switch to Batch Queue.");
          const payload = await buildPayload(common, originalFile, revisedFiles[0], 0, 1);
          const result = await requestCompare(payload);
          window.location.assign(result.run_url);
          return;
        }}
        await runBatch(common, originalFile, revisedFiles);
      }} catch (error) {{
        setStatus(error && error.message ? error.message : String(error), "error");
      }} finally {{
        setFormBusy(false);
      }}
    }});
  </script>
</body>
</html>
"""


# ============================================================================
# Review Shell v2 — dense-pro redesign port (see CHANGES.md)
#
# Parallel-running scaffold. Opt in via /runs/<id>?v=2. Old shell remains the
# default until the design port lands end-to-end. The constants below will
# grow to hold the full v2 stylesheet + client script; for now they carry the
# design tokens + the skeleton markup so routing + tokens can be validated.
# ============================================================================

_REVIEW_V2_STYLES = """
:root{
  --paper:#FAFAF7; --paper-2:#F3F2EC; --paper-3:#E9E7DD;
  --line:#DCDAD0;  --line-2:#C9C6B8;
  --ink:#1C1B17;   --ink-2:#3B3A33; --ink-3:#6B695E; --ink-4:#9B9886;
  --accent:oklch(0.48 0.13 250);
  --accent-2:oklch(0.42 0.14 250);
  --accent-soft:oklch(0.93 0.04 250);
  --ins:oklch(0.58 0.12 148); --ins-bg:oklch(0.94 0.05 148);
  --del:oklch(0.55 0.16 25);  --del-bg:oklch(0.94 0.05 25);
  --mod:oklch(0.58 0.12 70);  --mod-bg:oklch(0.94 0.05 70);
  --font-sans:"Inter Tight",ui-sans-serif,system-ui,-apple-system,"Helvetica Neue",Helvetica,Arial,sans-serif;
  --font-mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --top-h:40px; --status-h:26px; --rail-w:272px; --insp-w:340px;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;height:100%}
body{
  font-family:var(--font-sans);
  font-size:12.5px;
  line-height:1.35;
  letter-spacing:-0.005em;
  color:var(--ink);
  background:var(--paper);
  -webkit-font-smoothing:antialiased;
}
.app{
  display:grid;
  grid-template-rows:var(--top-h) 1fr var(--status-h);
  height:100vh;
  overflow:hidden;
}
button{ font-family:inherit; color:inherit; }

/* =========================================================
   TOP BAR
   ========================================================= */
.top{
  display:flex; align-items:stretch; gap:0;
  background:var(--paper); border-bottom:1px solid var(--line);
  padding:0; font-size:11.5px;
}
.top-sec{
  display:flex; align-items:center; gap:8px;
  padding:0 10px; height:100%;
  border-right:1px solid var(--line);
}
.top-sec:last-child{ border-right:none; }
.top-sec.right{ margin-left:auto; border-right:none; padding-right:10px; }

.brand{ gap:8px; }
.brand-mark{ width:12px; height:12px; background:var(--ink); border-radius:2px; }
.brand-name{ font-weight:600; letter-spacing:-0.01em; }
.brand-tag{ font-family:var(--font-mono); font-size:9.5px; color:var(--ink-4); text-transform:uppercase; letter-spacing:0.12em; }

.top-sec .lbl{
  font-family:var(--font-mono); font-size:9.5px; color:var(--ink-4);
  text-transform:uppercase; letter-spacing:0.1em;
}
.top-sec .metric{
  display:inline-flex; align-items:baseline; gap:4px;
  font-family:var(--font-mono); font-size:11px;
}
.top-sec .metric .k{ color:var(--ink-4); }
.top-sec .metric .v{ color:var(--ink-2); font-weight:500; }
.top-sec .metric .v.pending{ color:var(--mod); font-weight:600; }
.top-sec .hsep{ width:1px; height:10px; background:var(--line); display:inline-block; }

/* Buttons */
.btn{
  display:inline-flex; align-items:center; gap:6px;
  height:26px; padding:0 10px;
  background:var(--paper); color:var(--ink-2);
  border:1px solid var(--line); border-radius:4px;
  font-size:11.5px; font-weight:500; cursor:pointer;
  transition:background .1s, border-color .1s, color .1s;
}
.btn:hover{ background:var(--paper-2); border-color:var(--line-2); color:var(--ink); }
.btn.primary{
  background:var(--ink); color:var(--paper);
  border-color:var(--ink);
}
.btn.primary:hover{ background:var(--ink-2); border-color:var(--ink-2); color:var(--paper); }
.btn.ghost{ background:transparent; border-color:transparent; color:var(--ink-3); }
.btn.ghost:hover{ background:var(--paper-2); color:var(--ink); border-color:var(--line); }
.btn .caret{ opacity:.7; transition:transform .15s; }
.menu-wrap.open .btn .caret{ transform:rotate(180deg); }

/* Kbd keycap: 16×16 mono 10px, paper-2 bg, 1px border + 2px bottom, radius 3 */
.kb, .kb-small{
  display:inline-flex; align-items:center; justify-content:center;
  min-width:16px; height:16px; padding:0 3px;
  font-family:var(--font-mono); font-size:10px; font-weight:500;
  color:var(--ink-3);
  background:var(--paper-2);
  border:1px solid var(--line); border-bottom-width:2px;
  border-radius:3px;
  letter-spacing:0;
}
.btn .kb{ background:var(--paper); }
.btn.primary .kb{ background:rgba(255,255,255,0.14); color:var(--paper); border-color:rgba(255,255,255,0.2); }

/* Export dropdown */
.menu-wrap{ position:relative; }
.menu{
  position:absolute; top:calc(100% + 6px); left:0; z-index:60;
  min-width:220px; padding:6px;
  background:var(--paper); border:1px solid var(--line); border-radius:6px;
  box-shadow:0 10px 30px rgba(0,0,0,0.08);
  display:none; flex-direction:column; gap:2px;
}
.menu-wrap.open .menu{ display:flex; }
.menu .mini{
  font-family:var(--font-mono); font-size:9.5px; color:var(--ink-4);
  text-transform:uppercase; letter-spacing:0.1em;
  padding:6px 8px 3px;
}
.menu button, .menu a{
  display:flex; align-items:center; justify-content:space-between; gap:8px;
  text-decoration:none;
  padding:6px 8px; border:none; background:transparent; cursor:pointer;
  font-size:12px; color:var(--ink-2); border-radius:3px;
  text-align:left;
}
.menu button:hover, .menu a:hover{ background:var(--paper-2); color:var(--ink); }
.menu button:disabled{ color:var(--ink-4); cursor:not-allowed; background:transparent; }
.menu .sep{ height:1px; background:var(--line); margin:4px 2px; }

/* Jump input */
.jump{
  width:64px; height:24px; padding:0 6px;
  font-family:var(--font-mono); font-size:11px;
  background:var(--paper); color:var(--ink);
  border:1px solid var(--line); border-radius:3px;
  text-align:right;
}
.jump:focus{ outline:none; border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-soft); }
.jump::-webkit-outer-spin-button, .jump::-webkit-inner-spin-button{ -webkit-appearance:none; margin:0; }
.jump{ -moz-appearance:textfield; }

/* Segmented control */
.segmented{
  display:inline-flex; align-items:center;
  background:var(--paper-2); border:1px solid var(--line); border-radius:5px;
  padding:2px; gap:2px;
}
.segmented button{
  height:20px; padding:0 10px;
  background:transparent; border:none;
  font-size:11px; font-weight:500; color:var(--ink-3);
  border-radius:3px; cursor:pointer;
  transition:background .1s, color .1s;
}
.segmented button:hover{ color:var(--ink); }
.segmented button[aria-pressed="true"]{
  background:var(--ink); color:var(--paper);
}

/* =========================================================
   BODY GRID  (fills in steps 3–5)
   ========================================================= */
.body{
  display:grid;
  grid-template-columns:var(--rail-w) 1fr;
  min-height:0;
  overflow:hidden;
}
/* =========================================================
   LEFT RAIL
   ========================================================= */
.rail{
  background:var(--paper);
  border-right:1px solid var(--line);
  min-height:0; overflow:hidden;
  display:flex; flex-direction:column;
  padding:0;
}
.rail-search{
  padding:8px 10px; border-bottom:1px solid var(--line);
  display:flex; flex-direction:column; gap:6px;
  position:sticky; top:0; background:var(--paper); z-index:2;
}
.rail-scroll{ flex:1; overflow:auto; scrollbar-width:thin; }
.rail-scroll::-webkit-scrollbar{ width:8px; }
.rail-scroll::-webkit-scrollbar-thumb{ background:var(--paper-3); border-radius:4px; }

.search{ position:relative; }
.search input{
  width:100%; height:26px; padding:0 8px 0 24px;
  background:var(--paper-2); border:1px solid var(--line); border-radius:4px;
  font-size:12px; font-family:inherit; color:var(--ink); outline:none;
}
.search input:focus{ border-color:var(--accent); background:var(--paper); box-shadow:0 0 0 2px var(--accent-soft); }
.search::before{
  content:""; position:absolute; left:8px; top:50%; transform:translateY(-50%);
  width:10px; height:10px; border:1.5px solid var(--ink-4); border-radius:50%;
  pointer-events:none;
}
.search::after{
  content:""; position:absolute; left:15px; top:16px;
  width:5px; height:1.5px; background:var(--ink-4); transform:rotate(45deg);
  pointer-events:none;
}
.search-hint{
  display:flex; gap:4px; align-items:center;
  font-family:var(--font-mono); font-size:10px; color:var(--ink-4);
  text-transform:uppercase; letter-spacing:0.06em;
}
.search-hint .kb{ margin-left:2px; }

.group{ border-bottom:1px solid var(--line); }
.group-h{
  display:flex; align-items:center; gap:6px;
  padding:8px 10px 6px;
  font-family:var(--font-mono); font-size:10px; color:var(--ink-3);
  text-transform:uppercase; letter-spacing:0.08em;
  cursor:pointer; user-select:none;
  background:transparent; border:none; width:100%;
}
.group-h:hover{ color:var(--ink); }
.group-h .caret{ width:8px; height:8px; transition:transform .15s; color:var(--ink-4); }
.group-h .caret svg{ display:block; }
.group.collapsed .caret{ transform:rotate(-90deg); }
.group-h .count{ margin-left:auto; color:var(--ink-4); font-weight:500; }
.group-body{ padding:2px 8px 10px; display:flex; flex-wrap:wrap; gap:4px; }
.group.collapsed .group-body,
.group.collapsed .group-extras{ display:none; }

.chip{
  display:inline-flex; align-items:center; gap:5px;
  height:22px; padding:0 8px;
  border:1px solid var(--line); border-radius:11px;
  background:var(--paper); color:var(--ink-2);
  font-size:11px; white-space:nowrap;
  font-family:inherit; cursor:pointer;
  transition:background .1s, border-color .1s, color .1s;
}
.chip:hover{ border-color:var(--line-2); background:var(--paper-2); }
.chip[aria-pressed="true"]{ background:var(--ink); color:var(--paper); border-color:var(--ink); }
.chip .n{ font-family:var(--font-mono); font-size:10px; color:var(--ink-4); margin-left:1px; }
.chip[aria-pressed="true"] .n{ color:var(--paper-3); }
.chip.swatch::before{ content:""; width:6px; height:6px; border-radius:50%; background:var(--dot, var(--ink-3)); }

.scope-chip{
  justify-content:space-between;
  width:100%; border-radius:4px; height:24px;
}
.scope-progress{ margin:0 10px 8px; height:4px; background:var(--paper-3); border-radius:2px; overflow:hidden; }
.scope-progress > div{ height:100%; background:var(--accent); width:0%; transition:width .25s; }

.row-btns{ display:grid; grid-template-columns:1fr 1fr; gap:4px; padding:0 8px 10px; }
.row-btns.col3{ grid-template-columns:repeat(3,1fr); }
.row-btns .btn{ justify-content:space-between; width:100%; height:24px; padding:0 8px; font-size:11px; }
.row-btns .btn.accent{ background:var(--accent); color:var(--paper); border-color:var(--accent); }
.row-btns .btn.accent:hover{ background:var(--accent-2); border-color:var(--accent-2); }

.group-extras{ }
.group-hint{
  padding:0 10px 10px; font-family:var(--font-mono); font-size:10px; color:var(--ink-4);
}

.section-list-h{
  margin:0; padding:10px 10px 6px;
  font-family:var(--font-mono); font-size:10px; color:var(--ink-3);
  text-transform:uppercase; letter-spacing:0.08em; font-weight:500;
  border-top:1px solid var(--line); background:var(--paper);
}
.section-list{ padding:2px 0 10px; }
.section-item{
  display:grid; grid-template-columns:24px 1fr auto; gap:6px; align-items:center;
  padding:4px 10px; font-size:11.5px; cursor:pointer; color:var(--ink-2);
  border:none; background:transparent; width:100%; text-align:left;
  font-family:inherit;
}
.section-item:hover{ background:var(--paper-2); }
.section-item.active{ background:var(--accent-soft); color:var(--ink); }
.section-item .idx{ font-family:var(--font-mono); font-size:10px; color:var(--ink-4); }
.section-item .bar{ display:inline-flex; gap:1px; align-items:center; }
.section-item .bar span{ width:3px; height:10px; background:var(--paper-3); border-radius:1px; }
.section-item .bar span.m{ background:var(--mod); }
.section-item .bar span.i{ background:var(--ins); }
.section-item .bar span.d{ background:var(--del); }
/* =========================================================
   STAGE / DOCUMENT CARD
   ========================================================= */
.stage{
  background:var(--paper-2);
  position:relative; overflow:auto; min-width:0;
}
.doc{
  width:860px; max-width:calc(100% - 80px);
  margin:28px auto 60px;
  background:#fff;
  border:1px solid var(--line);
  box-shadow:0 1px 0 rgba(0,0,0,0.02), 0 20px 40px -20px rgba(28,27,23,0.12);
  padding:64px 84px;
  min-height:1100px;
  font-family:var(--font-sans);
  color:var(--ink);
  font-size:13.5px; line-height:1.65;
}
.doc h1{ font-size:22px; letter-spacing:-0.02em; margin:0 0 4px; text-align:center; color:var(--accent); font-weight:600; }
.doc .subtitle{ text-align:center; color:var(--ink-3); font-family:var(--font-mono); font-size:10px; text-transform:uppercase; letter-spacing:0.14em; margin-bottom:28px; }
.doc h2{ font-size:14px; text-transform:uppercase; letter-spacing:0.08em; margin:26px 0 10px; color:var(--ink-2); font-weight:600; }
.doc h3{ font-size:13px; margin:20px 0 6px; color:var(--ink-2); font-weight:600; }
.doc p{ margin:0 0 12px; }
.doc .pnum{ float:left; width:36px; margin-left:-52px; font-family:var(--font-mono); font-size:10px; color:var(--ink-4); padding-top:4px; }
.doc .p-interactive{ transition:background .1s; cursor:pointer; padding-left:8px; margin-left:-8px; border-radius:3px; position:relative; }
.doc .p-interactive:hover{ background:var(--paper-2); }
.doc .p-interactive.active{ background:var(--accent-soft); outline:1px solid var(--accent); outline-offset:-1px; }

/* Inline diff marks */
.ins{ background:var(--ins-bg); color:oklch(0.35 0.14 148); border-bottom:1px solid var(--ins); padding:0 1px; }
.del{ background:var(--del-bg); color:oklch(0.38 0.17 25); text-decoration:line-through; padding:0 1px; }
.modbar{ border-left:2px solid var(--mod); padding-left:10px; margin-left:-12px; cursor:pointer; }
.modbar:hover{ background:var(--mod-bg); }
.modbar.selected, .modbar.active{ background:var(--mod-bg); outline:1px solid var(--mod); outline-offset:-1px; }
.doc .p-interactive.kind-insert{ border-left:2px solid var(--ins); padding-left:10px; margin-left:-12px; }
.doc .p-interactive.kind-delete{ border-left:2px solid var(--del); padding-left:10px; margin-left:-12px; }

::selection{ background:var(--accent-soft); color:var(--ink); }

.stage-loading{
  font-family:var(--font-mono); font-size:10.5px;
  color:var(--ink-3); text-transform:uppercase; letter-spacing:0.06em;
  padding:28px; text-align:center;
}

/* =========================================================
   SLIDE-IN INSPECTOR
   ========================================================= */
.inspector{
  position:absolute; top:0; right:0; bottom:0;
  width:var(--insp-w);
  background:var(--paper);
  border-left:1px solid var(--line);
  box-shadow:-20px 0 40px -20px rgba(28,27,23,0.12);
  transform:translateX(100%);
  transition:transform .22s cubic-bezier(.2,.7,.2,1);
  display:flex; flex-direction:column;
  z-index:20;
}
.inspector.open{ transform:translateX(0); }
.insp-h{
  display:flex; align-items:flex-start; gap:8px;
  padding:10px 12px; border-bottom:1px solid var(--line);
}
.insp-h .insp-h-main{ flex:1; min-width:0; }
.insp-h .status-pill{
  display:inline-flex; align-items:center; gap:5px;
  padding:2px 7px; border-radius:10px;
  font-family:var(--font-mono); font-size:10px; text-transform:uppercase; letter-spacing:0.06em;
  background:var(--mod-bg); color:oklch(0.38 0.14 70);
  margin-bottom:4px;
}
.insp-h .status-pill::before{ content:""; width:5px; height:5px; border-radius:50%; background:var(--mod); }
.insp-h .status-pill.kind-insert{ background:var(--ins-bg); color:oklch(0.35 0.14 148); }
.insp-h .status-pill.kind-insert::before{ background:var(--ins); }
.insp-h .status-pill.kind-delete{ background:var(--del-bg); color:oklch(0.38 0.17 25); }
.insp-h .status-pill.kind-delete::before{ background:var(--del); }
.insp-h h2{
  margin:0; font-size:13px; font-weight:600; letter-spacing:-0.01em;
  display:flex; align-items:baseline; gap:8px; flex-wrap:wrap;
}
.insp-h h2 .ctx{ color:var(--ink-4); font-weight:400; font-family:var(--font-mono); font-size:11px; }
.insp-h .close{
  margin-left:auto; color:var(--ink-4); font-size:16px; line-height:1;
  width:22px; height:22px; border-radius:4px;
  background:transparent; border:none; cursor:pointer;
}
.insp-h .close:hover{ background:var(--paper-2); color:var(--ink); }

.insp-body{ flex:1; overflow:auto; padding:12px 12px 0; }
.insp-section{ margin-bottom:14px; }
.insp-section .label{
  font-family:var(--font-mono); font-size:9.5px; color:var(--ink-4);
  text-transform:uppercase; letter-spacing:0.1em;
  margin-bottom:6px; display:flex; align-items:center; gap:6px;
}
.insp-section .label::after{ content:""; flex:1; height:1px; background:var(--line); }

.meta-row{
  display:flex; align-items:baseline; justify-content:space-between;
  padding:3px 0; font-size:12px;
}
.meta-row .k{ color:var(--ink-4); font-family:var(--font-mono); font-size:10.5px; }
.meta-row .v{ color:var(--ink); font-weight:500; text-align:right; }

.tags{ display:flex; flex-wrap:wrap; gap:4px; }
.tag{
  font-family:var(--font-mono); font-size:9.5px;
  text-transform:uppercase; letter-spacing:0.06em;
  padding:2px 6px; border-radius:3px;
  background:var(--paper-2); color:var(--ink-3); border:1px solid var(--line);
}
.tag.active{ background:var(--accent-soft); color:var(--accent-2); border-color:transparent; }

.delta-box{
  border:1px solid var(--line); border-radius:4px; padding:8px 10px;
  background:var(--paper-2);
  font-family:var(--font-mono); font-size:11px; color:var(--ink-2);
  line-height:1.55;
}
.delta-box code{ color:var(--accent-2); }
.delta-box .muted{ color:var(--ink-4); }

.diff-pair{ display:flex; flex-direction:column; gap:8px; }
.diff-card{ border:1px solid var(--line); border-radius:4px; overflow:hidden; background:#fff; }
.diff-card .dh{
  padding:5px 9px; border-bottom:1px solid var(--line);
  font-family:var(--font-mono); font-size:9.5px;
  text-transform:uppercase; letter-spacing:0.1em;
  display:flex; align-items:center; gap:6px;
  background:var(--paper-2); color:var(--ink-3);
}
.diff-card .dh::before{ content:""; width:6px; height:6px; border-radius:50%; }
.diff-card.orig .dh::before{ background:var(--del); }
.diff-card.rev  .dh::before{ background:var(--ins); }
.diff-card .db{ padding:10px 12px; font-size:12.5px; line-height:1.5; }
.diff-card.orig{ border-left:2px solid var(--del); }
.diff-card.rev { border-left:2px solid var(--ins); }

.insp-foot{
  border-top:1px solid var(--line);
  padding:10px 12px;
  display:flex; flex-direction:column; gap:8px;
  background:var(--paper);
}
.insp-foot .label{
  font-family:var(--font-mono); font-size:9.5px; color:var(--ink-4);
  text-transform:uppercase; letter-spacing:0.1em;
}
.insp-foot .row{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:6px; }
.insp-foot .btn{ justify-content:center; height:28px; font-size:12px; gap:6px; }
.insp-foot .btn[aria-pressed="true"]{
  background:var(--ink); color:var(--paper); border-color:var(--ink);
}
.insp-foot .btn.accept[aria-pressed="true"]{ background:var(--accent); border-color:var(--accent); }
.insp-foot .btn .kb{ background:var(--paper-2); }
.insp-foot .btn.accept[aria-pressed="true"] .kb{
  background:rgba(255,255,255,0.16); color:var(--paper); border-color:rgba(255,255,255,0.2);
}

/* =========================================================
   STATUS BAR
   ========================================================= */
.status{
  display:flex; align-items:center; gap:10px;
  font-family:var(--font-mono); font-size:10.5px; letter-spacing:0.06em;
  color:var(--ink-3); text-transform:uppercase;
  background:var(--paper); border-top:1px solid var(--line);
  padding:0 12px;
}
.status .dot{
  width:6px; height:6px; border-radius:50%;
  background:var(--ink-4); display:inline-block;
}
.status .dot.ok{ background:var(--ins); }
.status .vsep{ width:1px; height:12px; background:var(--line); display:inline-block; }
.status .pending{ color:var(--mod); font-weight:600; }
.status .spacer{ flex:1; }
.status .minibar{
  display:inline-block; position:relative;
  width:70px; height:4px; background:var(--paper-3);
  border-radius:2px; overflow:hidden;
}
.status .minibar .fill{
  position:absolute; top:0; left:0; bottom:0;
  background:var(--accent);
  width:0%; transition:width .3s;
}
.status .hint{ color:var(--ink-4); }
"""

_REVIEW_V2_SCRIPT = """
/* Review shell v2 — top bar + status bar dispatcher.
   Shared runtime state lives on window.__BL__ so later steps (rail, stage,
   inspector) can bind to the same meta + selection + decisions store. */
(function v2Boot(){
  const RUN_ID = window.__BL_RUN_ID__;
  const BL = window.__BL__ = {
    runId: RUN_ID,
    meta: null,
    decisions: {},
    selection: null,
    viewMode: 'inline',
    filters: { kind:'changed', facets:new Set(), decision:'any', formatOnly:false, q:'' },
    listeners: new Set(),
    emit(kind, payload){ for(const l of this.listeners) try{ l(kind, payload); }catch(_){} },
    on(fn){ this.listeners.add(fn); return () => this.listeners.delete(fn); },
  };

  /* ---------------- Data layer ---------------- */
  function loadMeta(){
    return fetch('/api/runs/' + encodeURIComponent(RUN_ID))
      .then(r => r.ok ? r.json() : Promise.reject('HTTP ' + r.status))
      .then(meta => { BL.meta = meta; return meta; });
  }

  function sectionsChanged(){
    if (!BL.meta || !Array.isArray(BL.meta.sections)) return [];
    return BL.meta.sections.filter(s => s && s.kind && s.kind !== 'equal');
  }

  function countDecisions(){
    const d = BL.decisions || {};
    let accepted = 0, rejected = 0;
    for (const k in d){ if (d[k] === 'accept') accepted++; else if (d[k] === 'reject') rejected++; }
    const changed = sectionsChanged().length;
    const decided = accepted + rejected;
    const pending = Math.max(0, changed - decided);
    return { accepted, rejected, decided, pending, changed };
  }

  /* ---------------- Top bar metrics + status bar ---------------- */
  function pad2(n){ return n < 10 ? '0' + n : '' + n; }

  function fmtDuration(iso){
    if (!iso) return '—';
    try{
      const then = new Date(iso).getTime();
      const sec = Math.max(1, Math.round((Date.now() - then) / 1000));
      if (sec < 60) return sec + 's ago';
      if (sec < 3600) return Math.round(sec/60) + 'm ago';
      if (sec < 86400) return Math.round(sec/3600) + 'h ago';
      return Math.round(sec/86400) + 'd ago';
    } catch(_){ return '—'; }
  }

  function refreshMetricsAndStatus(){
    if (!BL.meta) return;
    const c = countDecisions();
    const visible = c.changed; // step 3 (filters) will reduce this

    // Top bar metrics
    setText('m-t', fmtDuration(BL.meta.created_at));
    setText('m-vis', visible + '/' + c.changed);
    setText('m-pend', String(c.pending));

    // Status bar
    const orig = BL.meta.original_name || '—';
    const rev = BL.meta.revised_name || '—';
    setText('s-files', orig + ' ↔ ' + rev);
    setText('s-changes', c.changed + ' change' + (c.changed === 1 ? '' : 's'));
    setText('s-decided', String(c.decided));
    setText('s-pending', String(c.pending));
    const pct = c.changed ? Math.round((c.decided / c.changed) * 100) : 0;
    setText('s-progress-pct', pct + '%');
    const fill = document.getElementById('s-progress');
    if (fill) fill.style.width = pct + '%';
  }

  function setText(id, value){
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  /* ---------------- Export dropdown ---------------- */
  function wireExportMenu(){
    const wrap = document.getElementById('exportWrap');
    const btn = document.getElementById('exportBtn');
    if (!wrap || !btn) return;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      wrap.classList.toggle('open');
    });
    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) wrap.classList.remove('open');
    });
    // Populate links from metadata.files
    wrap.addEventListener('click', (e) => {
      const b = e.target.closest('[data-export]');
      if (!b) return;
      const fmt = b.getAttribute('data-export');
      if (fmt === 'final-docx'){
        window.location.href = '/api/runs/' + encodeURIComponent(RUN_ID) + '/export-clean';
      } else if (BL.meta && BL.meta.files && BL.meta.files[fmt]){
        const fname = BL.meta.files[fmt];
        window.location.href = '/runs/' + encodeURIComponent(RUN_ID) + '/downloads/' + encodeURIComponent(fname);
      }
      wrap.classList.remove('open');
    });
  }

  function refreshExportMenuState(){
    if (!BL.meta || !BL.meta.files) return;
    const files = BL.meta.files;
    document.querySelectorAll('#exportMenu [data-export]').forEach(b => {
      const fmt = b.getAttribute('data-export');
      if (fmt === 'final-docx') return; // always enabled
      if (!files[fmt]) b.disabled = true;
    });
  }

  /* ---------------- Segmented view-mode control ---------------- */
  function wireSegmented(){
    const buttons = Array.from(document.querySelectorAll('.segmented [data-view]'));
    buttons.forEach(b => b.addEventListener('click', () => {
      buttons.forEach(o => o.setAttribute('aria-pressed', o === b ? 'true' : 'false'));
      BL.viewMode = b.getAttribute('data-view');
      BL.emit('viewMode', BL.viewMode);
    }));
  }

  /* ---------------- Nav controls (Prev/Next/Jump/Zen/Shortcuts) ---------------- */
  function wireNav(){
    const prev = document.getElementById('btn-prev-section');
    const next = document.getElementById('btn-next-section');
    const jump = document.getElementById('jump-index');
    const zen = document.getElementById('btn-zen');
    const shortcuts = document.getElementById('btn-shortcuts');
    if (prev) prev.addEventListener('click', () => BL.emit('nav','prev'));
    if (next) next.addEventListener('click', () => BL.emit('nav','next'));
    if (jump) jump.addEventListener('keydown', (e) => {
      if (e.key === 'Enter'){
        const n = parseInt(jump.value, 10);
        if (!Number.isNaN(n)) BL.emit('jump', n);
      }
    });
    if (zen) zen.addEventListener('click', () => {
      document.body.classList.toggle('zen-mode');
      BL.emit('zen', document.body.classList.contains('zen-mode'));
    });
    if (shortcuts) shortcuts.addEventListener('click', () => BL.emit('shortcuts','toggle'));
  }

  /* ---------------- Keyboard shortcuts (minimal, step-2 scope) ---------------- */
  function wireKeys(){
    document.addEventListener('keydown', (e) => {
      if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'e'){
        e.preventDefault();
        window.location.href = '/api/runs/' + encodeURIComponent(RUN_ID) + '/export-clean';
      }
    });
  }

  /* ---------------- Filters + rail ---------------- */
  const FACET_ORDER = ['content','formatting','style','alignment','layout','indentation','spacing','pagination','numbering','capitalization','punctuation','whitespace','header','footer','table','textbox','footnote','endnote'];
  const FACET_LABELS = {
    content:'Content', formatting:'Formatting', style:'Style', alignment:'Alignment',
    layout:'Layout', indentation:'Indent', spacing:'Spacing', pagination:'Pagination',
    numbering:'Numbering', capitalization:'Capitalization', punctuation:'Punctuation',
    whitespace:'Whitespace', header:'Header', footer:'Footer', table:'Table',
    textbox:'Textbox', footnote:'Footnote', endnote:'Endnote',
  };

  function decisionFor(section){
    const saved = BL.decisions[String(section.index)];
    if (saved === 'accept') return 'accept';
    if (saved === 'reject') return 'reject';
    return 'pending';
  }

  function sectionMatchesFilters(sec){
    const f = BL.filters;
    if (!sec) return false;
    // Kind
    if (f.kind === 'changed' && (!sec.kind || sec.kind === 'equal')) return false;
    else if (f.kind !== 'changed' && f.kind !== 'all' && sec.kind !== f.kind) return false;
    // Facets (union: match if ANY selected facet appears on the section)
    if (f.facets.size){
      const secFacets = new Set([...(sec.change_facets||[]), ...(sec.format_change_facets||[])]);
      let hit = false;
      for (const x of f.facets){ if (secFacets.has(x)){ hit = true; break; } }
      if (!hit) return false;
    }
    // Decisions
    if (f.decision !== 'any'){
      const d = decisionFor(sec);
      if (f.decision !== d) return false;
    }
    // Formatting-only
    if (f.formatOnly){
      const hasContent = (sec.change_facets||[]).some(x => x !== 'formatting' && !['style','alignment','layout','indentation','spacing','pagination'].includes(x));
      if (hasContent) return false;
    }
    // Search
    const q = (f.q||'').trim().toLowerCase();
    if (q){
      const hay = [sec.label, sec.kind, sec.original_text, sec.revised_text].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function visibleSections(){
    if (!BL.meta) return [];
    return (BL.meta.sections || []).filter(sectionMatchesFilters);
  }

  function buildCounts(){
    if (!BL.meta) return null;
    const secs = BL.meta.sections || [];
    const c = {
      all: secs.length, changed:0, insert:0, delete:0, replace:0, move:0,
      facets:{}, decision:{any:0, pending:0, accept:0, reject:0},
      fmtOnly:0, fmtOnlyTotal:0,
    };
    for (const s of secs){
      if (s.kind && s.kind !== 'equal') c.changed += 1;
      if (s.kind && c[s.kind] !== undefined) c[s.kind] += 1;
      const allFacets = new Set([...(s.change_facets||[]), ...(s.format_change_facets||[])]);
      for (const f of allFacets){ c.facets[f] = (c.facets[f]||0) + 1; }
      const d = decisionFor(s);
      c.decision.any += 1;
      c.decision[d] = (c.decision[d]||0) + 1;
      // Formatting-only: sections whose only facets are layout/spacing/style/etc.
      if (s.kind && s.kind !== 'equal'){
        const f = s.change_facets || [];
        const fmtOnly = f.length && f.every(x => ['formatting','style','alignment','layout','indentation','spacing','pagination'].includes(x));
        if (fmtOnly) c.fmtOnly += 1;
        c.fmtOnlyTotal += 1;
      }
    }
    return c;
  }

  function makeChip(label, opts){
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip' + (opts.dot ? ' swatch' : '');
    if (opts.dot) btn.style.setProperty('--dot', opts.dot);
    btn.setAttribute('aria-pressed', opts.active ? 'true' : 'false');
    btn.dataset.value = opts.value;
    btn.innerHTML = '<span>' + escapeHtml(label) + '</span>' + (opts.count != null ? '<span class="n">' + opts.count + '</span>' : '');
    if (opts.onToggle) btn.addEventListener('click', () => opts.onToggle(btn));
    return btn;
  }

  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function renderTypeChips(counts){
    const row = document.getElementById('filter-row');
    if (!row) return;
    row.innerHTML = '';
    const items = [
      {label:'Changes',  value:'changed', dot:'var(--mod)',    count:counts.changed},
      {label:'Moves',    value:'move',    dot:'var(--ink-3)',  count:counts.move},
      {label:'Replaced', value:'replace', dot:'var(--accent)', count:counts.replace},
      {label:'Inserts',  value:'insert',  dot:'var(--ins)',    count:counts.insert},
      {label:'Deletes',  value:'delete',  dot:'var(--del)',    count:counts.delete},
      {label:'All',      value:'all',     dot:null,            count:counts.all},
    ];
    for (const it of items){
      row.appendChild(makeChip(it.label, {
        value: it.value, dot: it.dot, count: it.count,
        active: BL.filters.kind === it.value,
        onToggle: () => { BL.filters.kind = it.value; BL.emit('filters','kind'); renderRail(); },
      }));
    }
  }

  function renderFacetChips(counts){
    const row = document.getElementById('facet-row');
    if (!row) return;
    row.innerHTML = '';
    // "Any facet" clears all facet filters.
    row.appendChild(makeChip('Any facet', {
      value: '__any__', count: counts.changed,
      active: BL.filters.facets.size === 0,
      onToggle: () => { BL.filters.facets.clear(); BL.emit('filters','facets'); renderRail(); },
    }));
    for (const key of FACET_ORDER){
      const n = counts.facets[key] || 0;
      if (!n) continue;
      row.appendChild(makeChip(FACET_LABELS[key] || key, {
        value: key, count: n,
        active: BL.filters.facets.has(key),
        onToggle: () => {
          if (BL.filters.facets.has(key)) BL.filters.facets.delete(key);
          else BL.filters.facets.add(key);
          BL.emit('filters','facets');
          renderRail();
        },
      }));
    }
  }

  function renderDecisionChips(counts){
    const row = document.getElementById('decision-row');
    if (!row) return;
    row.innerHTML = '';
    const items = [
      {label:'Any',      value:'any',     dot:null,         count:counts.decision.any},
      {label:'Pending',  value:'pending', dot:null,         count:counts.decision.pending},
      {label:'Accepted', value:'accept',  dot:'var(--ins)', count:counts.decision.accept},
      {label:'Rejected', value:'reject',  dot:'var(--del)', count:counts.decision.reject},
    ];
    for (const it of items){
      row.appendChild(makeChip(it.label, {
        value: it.value, dot: it.dot, count: it.count,
        active: BL.filters.decision === it.value,
        onToggle: () => { BL.filters.decision = it.value; BL.emit('filters','decision'); renderRail(); },
      }));
    }
  }

  function renderSectionList(){
    const list = document.getElementById('detail-list');
    if (!list) return;
    const vis = visibleSections();
    if (!vis.length){
      list.innerHTML = '<div class="group-hint">No sections in current scope.</div>';
      return;
    }
    list.innerHTML = '';
    for (const sec of vis){
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'section-item' + (BL.selection === sec.index ? ' active' : '');
      btn.dataset.index = String(sec.index);
      const bar = barFor(sec);
      btn.innerHTML =
        '<span class="idx">' + String(sec.index).padStart(2,'0') + '</span>' +
        '<span>' + escapeHtml(sec.label || 'Section ' + sec.index) + '</span>' +
        '<span class="bar">' + bar + '</span>';
      btn.addEventListener('click', () => {
        BL.selection = sec.index;
        BL.emit('select', sec.index);
        renderSectionList();
      });
      list.appendChild(btn);
    }
  }

  function barFor(sec){
    // 4-cell mini density bar. Class per cell: m=mod, i=ins, d=del, empty=no change.
    const kinds = [];
    if (sec.kind === 'insert') kinds.push('i');
    else if (sec.kind === 'delete') kinds.push('d');
    else if (sec.kind === 'replace' || sec.kind === 'move') kinds.push('m');
    // Pad with subtle indicators based on facet count.
    const facetCount = (sec.change_facets||[]).length + (sec.format_change_facets||[]).length;
    while (kinds.length < 4){
      kinds.push(facetCount >= kinds.length ? 'm' : '');
    }
    return kinds.slice(0,4).map(k => '<span' + (k ? ' class="' + k + '"' : '') + '></span>').join('');
  }

  function renderRail(){
    const counts = buildCounts();
    if (!counts) return;
    renderTypeChips(counts);
    renderFacetChips(counts);
    renderDecisionChips(counts);
    renderSectionList();

    // Scope counts + progress
    const visCount = visibleSections().length;
    setText('scope-count', visCount + ' / ' + counts.changed);
    setText('type-count', counts.changed + ' changed');
    const activeFacets = BL.filters.facets.size || (counts.changed ? Object.keys(counts.facets).length : 0);
    setText('facet-count', activeFacets + ' filter' + (activeFacets === 1 ? '' : 's'));
    setText('decisions-count', counts.decision.decided || (counts.decision.accept + counts.decision.reject) + ' / ' + counts.changed);
    setText('format-only-count', counts.fmtOnly + '/' + counts.fmtOnlyTotal);

    const fmtBtn = document.getElementById('format-only-toggle');
    if (fmtBtn) fmtBtn.setAttribute('aria-pressed', BL.filters.formatOnly ? 'true' : 'false');

    const pct = counts.changed ? (visCount / counts.changed) * 100 : 0;
    const pf = document.getElementById('scope-progress-fill');
    if (pf) pf.style.width = Math.max(0, Math.min(100, pct)) + '%';

    // Pending guidance
    const note = document.getElementById('next-undecided-note');
    if (note){
      if (counts.decision.pending){
        note.textContent = counts.decision.pending + ' pending · ' + counts.decision.decided + ' decided';
      } else if (counts.changed){
        note.textContent = 'All changes decided.';
      } else {
        note.textContent = 'No changes in this run.';
      }
    }
  }

  function wireGroups(){
    document.querySelectorAll('.rail .group .group-h').forEach(h => {
      h.addEventListener('click', () => {
        const group = h.closest('.group');
        if (!group) return;
        group.classList.toggle('collapsed');
        h.setAttribute('aria-expanded', group.classList.contains('collapsed') ? 'false' : 'true');
      });
    });
  }

  function wireSearch(){
    const input = document.getElementById('search');
    if (!input) return;
    input.addEventListener('input', () => {
      BL.filters.q = input.value || '';
      BL.emit('filters','search');
      renderRail();
    });
    document.addEventListener('keydown', (e) => {
      if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if (e.key === '/'){ e.preventDefault(); input.focus(); }
    });
  }

  function wireRailButtons(){
    const on = (id, fn) => { const b = document.getElementById(id); if (b) b.addEventListener('click', fn); };
    on('format-only-toggle', () => {
      BL.filters.formatOnly = !BL.filters.formatOnly;
      BL.emit('filters','formatOnly');
      renderRail();
    });
    on('next-pending-btn', () => BL.emit('nav','next-pending'));
    on('next-format-btn',  () => BL.emit('nav','next-format'));
    on('next-changed-btn', () => BL.emit('nav','next-changed'));
    on('next-undecided-btn', () => BL.emit('nav','next-undecided'));
    on('bulk-accept', () => applyBulk('accept'));
    on('bulk-reject', () => applyBulk('reject'));
    on('bulk-clear',  () => applyBulk('pending'));
    on('bulk-undo',   () => BL.emit('undo'));
  }

  function applyBulk(decision){
    const indexes = visibleSections().filter(s => s.kind !== 'equal').map(s => s.index);
    if (!indexes.length) return;
    fetch('/api/runs/' + encodeURIComponent(RUN_ID) + '/decisions/batch', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({decision, section_indexes: indexes}),
    }).then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(payload => {
        if (payload && payload.decisions) BL.decisions = payload.decisions;
        refreshMetricsAndStatus();
        renderRail();
      }).catch(() => {});
  }

  function loadDecisions(){
    if (BL.meta && BL.meta.decisions && typeof BL.meta.decisions === 'object'){
      BL.decisions = Object.assign({}, BL.meta.decisions);
    }
  }

  /* ---------------- Document renderer ---------------- */
  function renderTokens(tokens){
    if (!Array.isArray(tokens) || !tokens.length) return '';
    const parts = [];
    for (const t of tokens){
      const txt = escapeHtml(t.text || '').replace(/\\n/g, '<br>');
      if (t.kind === 'insert') parts.push('<span class="ins">' + txt + '</span>');
      else if (t.kind === 'delete') parts.push('<span class="del">' + txt + '</span>');
      else parts.push(txt);
    }
    return parts.join('');
  }

  function renderDoc(){
    const doc = document.getElementById('doc');
    if (!doc || !BL.meta) return;
    const secs = BL.meta.sections || [];
    const title = BL.meta.revised_name || BL.meta.original_name || 'Document';
    const subtitle = (BL.meta.original_name && BL.meta.revised_name)
      ? (BL.meta.original_name + ' → ' + BL.meta.revised_name)
      : (BL.meta.profile_summary || '');

    let html = '';
    html += '<h1>' + escapeHtml(title.replace(/\\.docx?$/i, '').replace(/_/g,' ')) + '</h1>';
    html += '<div class="subtitle">' + escapeHtml(subtitle) + '</div>';

    for (const sec of secs){
      const idx = sec.index;
      const changed = sec.kind && sec.kind !== 'equal';
      // Decide inline body: if tokens are present, render them; otherwise use
      // the revised text (unchanged paragraph) or the original text (deleted).
      const tokens = sec.combined_tokens;
      let body;
      if (tokens && tokens.length){
        body = renderTokens(tokens);
      } else if (sec.kind === 'delete'){
        body = escapeHtml(sec.original_text || '');
      } else {
        body = escapeHtml(sec.revised_text || sec.original_text || '');
      }
      if (!body) body = '&nbsp;';

      const labelText = sec.label || ('Paragraph ' + idx);
      const pnum = escapeHtml(labelText.replace(/^Paragraph\\s+/i, '¶'));
      const classes = ['p-interactive'];
      if (changed) classes.push('modbar');
      classes.push('kind-' + (sec.kind || 'equal'));
      if (BL.selection === idx) classes.push('active', 'selected');

      // Use <h2>/<h3>/<h4> heuristics: block_kind hints don't include heading,
      // but the label often is "Heading …" — fall back to <p> which is fine.
      const tag = 'p';
      html += '<' + tag + ' class="' + classes.join(' ') + '" id="sec-' + idx + '" data-idx="' + idx + '">' +
        '<span class="pnum">' + pnum + '</span>' +
        body + '</' + tag + '>';
    }
    doc.innerHTML = html;

    // Wire click-to-select. Changed paragraphs open the inspector; unchanged
    // ones close it (per brief).
    doc.querySelectorAll('.p-interactive').forEach(el => {
      el.addEventListener('click', (e) => {
        if (e.target && e.target.tagName === 'A') return;
        const idx = parseInt(el.dataset.idx, 10);
        if (Number.isNaN(idx)) return;
        const sec = findSection(idx);
        if (sec && sec.kind && sec.kind !== 'equal'){
          selectSection(idx);
          openInspector(idx);
        } else {
          closeInspector();
        }
      });
    });

    // Banner cleanup
    const banner = document.getElementById('v2-stage-banner');
    if (banner && banner.parentNode === doc) banner.remove();
  }

  function findSection(idx){
    if (!BL.meta) return null;
    return (BL.meta.sections || []).find(s => s.index === idx) || null;
  }

  function markSectionActive(idx){
    document.querySelectorAll('.doc .p-interactive.active').forEach(el => el.classList.remove('active','selected'));
    if (idx == null) return;
    const el = document.getElementById('sec-' + idx);
    if (el) el.classList.add('active', 'selected');
  }

  function selectSection(idx){
    BL.selection = idx;
    markSectionActive(idx);
    renderSectionList();
  }

  function scrollSectionIntoView(idx){
    const el = document.getElementById('sec-' + idx);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  /* ---------------- Inspector ---------------- */
  function openInspector(idx){
    const insp = document.getElementById('inspector');
    if (!insp) return;
    const sec = findSection(idx);
    if (!sec) return;
    renderInspector(sec);
    insp.classList.add('open');
    insp.setAttribute('aria-hidden', 'false');
  }

  function closeInspector(){
    const insp = document.getElementById('inspector');
    if (!insp) return;
    insp.classList.remove('open');
    insp.setAttribute('aria-hidden', 'true');
  }

  function renderInspector(sec){
    const kind = sec.kind || 'equal';
    const label = sec.kind_label || kind;
    const decision = decisionFor(sec);

    // Header pill + title + subtitle
    const pill = document.getElementById('insp-pill');
    if (pill){
      pill.className = 'status-pill kind-' + kind;
      const decLabel = decision.charAt(0).toUpperCase() + decision.slice(1);
      pill.textContent = label + ' · ' + decLabel;
    }
    setText('insp-title', sec.label || ('Paragraph ' + sec.index));
    const container = sec.container === 'body' ? 'Body' : (sec.container || 'Body');
    setText('insp-subtitle', '· ' + container + ' · sec ' + sec.index);

    // Classification tags
    const tags = document.getElementById('insp-tags');
    if (tags){
      tags.innerHTML = '';
      const all = new Set([...(sec.change_facets||[]), ...(sec.format_change_facets||[])]);
      if (!all.size){ tags.innerHTML = '<span class="tag">No facets</span>'; }
      else for (const t of all){
        const span = document.createElement('span');
        span.className = 'tag' + ((sec.format_change_facets||[]).includes(t) ? ' active' : '');
        span.textContent = FACET_LABELS[t] || t;
        tags.appendChild(span);
      }
    }

    // Formatting deltas — placeholder summary from format_change_facets.
    // TODO(ui): when core.py emits per-key deltas (layout.align, spacing.before, …),
    //   render them verbatim in the delta-box.
    const deltas = document.getElementById('insp-deltas');
    const fmt = sec.format_change_facets || [];
    if (deltas){
      if (!fmt.length){
        deltas.innerHTML = '<span class="muted">No formatting changes.</span>';
      } else {
        deltas.innerHTML = fmt.map(f => '<code>' + escapeHtml(f) + '</code>: <span class="muted">changed</span>').join('<br>');
      }
    }

    // Metadata rows — only what core.py actually exposes today.
    const meta = document.getElementById('insp-meta');
    if (meta){
      const rows = [
        ['location', sec.location_kind || '—'],
        ['container', sec.container || '—'],
        ['kind', sec.kind || '—'],
        ['original', sec.original_label || '—'],
        ['revised', sec.revised_label || '—'],
      ];
      meta.innerHTML = rows.map(([k,v]) =>
        '<div class="meta-row"><span class="k">' + escapeHtml(k) + '</span><span class="v">' + escapeHtml(String(v)) + '</span></div>'
      ).join('');
    }

    // Compare blocks
    setText('insp-original', sec.original_text || '—');
    setText('insp-revised',  sec.revised_text  || '—');

    // Decision buttons reflect current state.
    document.querySelectorAll('.insp-foot [data-action]').forEach(b => {
      const act = b.getAttribute('data-action');
      b.setAttribute('aria-pressed', act === decision ? 'true' : 'false');
    });
  }

  function wireInspector(){
    const insp = document.getElementById('inspector');
    const close = document.getElementById('inspClose');
    if (close) close.addEventListener('click', () => closeInspector());
    if (insp){
      insp.querySelectorAll('[data-action]').forEach(b => {
        b.addEventListener('click', () => {
          if (BL.selection == null) return;
          const action = b.getAttribute('data-action');
          sendDecision(BL.selection, action);
        });
      });
    }
    document.addEventListener('keydown', (e) => {
      if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if (e.key === 'Escape'){ closeInspector(); return; }
      if (BL.selection == null) return;
      const k = e.key.toLowerCase();
      if (k === 'a'){ e.preventDefault(); sendDecision(BL.selection, 'accept'); }
      else if (k === 'r'){ e.preventDefault(); sendDecision(BL.selection, 'reject'); }
      else if (k === 'u'){ e.preventDefault(); sendDecision(BL.selection, 'pending'); }
    });
  }

  function sendDecision(index, decision){
    fetch('/api/runs/' + encodeURIComponent(RUN_ID) + '/decisions', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({section_index: index, decision}),
    }).then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(payload => {
        if (payload && payload.decisions) BL.decisions = payload.decisions;
        const sec = findSection(index);
        if (sec) renderInspector(sec);
        refreshMetricsAndStatus();
        renderRail();
      }).catch(() => {});
  }

  /* ---------------- Navigation bridge ---------------- */
  BL.on((kind, payload) => {
    if (kind === 'nav'){
      const vis = visibleSections().filter(s => s.kind !== 'equal');
      if (!vis.length) return;
      const curIdx = BL.selection == null ? -1 : vis.findIndex(s => s.index === BL.selection);
      let next = null;
      if (payload === 'prev') next = vis[Math.max(0, curIdx - 1)];
      else if (payload === 'next') next = vis[Math.min(vis.length - 1, curIdx + 1)];
      else if (payload === 'next-pending') next = vis.find((s, i) => i > curIdx && decisionFor(s) === 'pending') || vis.find(s => decisionFor(s) === 'pending');
      else if (payload === 'next-changed') next = vis[Math.min(vis.length - 1, curIdx + 1)];
      else if (payload === 'next-undecided') next = vis.find((s, i) => i > curIdx && decisionFor(s) === 'pending') || vis.find(s => decisionFor(s) === 'pending');
      else if (payload === 'next-format'){
        const fmtOnly = vis.filter(s => (s.change_facets||[]).every(x => ['formatting','style','alignment','layout','indentation','spacing','pagination'].includes(x)));
        next = fmtOnly.find((s, i) => vis.indexOf(s) > curIdx) || fmtOnly[0];
      }
      if (next){ selectSection(next.index); openInspector(next.index); scrollSectionIntoView(next.index); }
    } else if (kind === 'jump'){
      const target = findSection(payload);
      if (target){
        selectSection(payload);
        if (target.kind !== 'equal') openInspector(payload);
        scrollSectionIntoView(payload);
      }
    } else if (kind === 'select'){
      const sec = findSection(payload);
      if (sec){
        selectSection(payload);
        if (sec.kind !== 'equal') openInspector(payload);
        scrollSectionIntoView(payload);
      }
    }
  });

  /* ---------------- Keyboard: J/K navigation ---------------- */
  function wireNavKeys(){
    document.addEventListener('keydown', (e) => {
      if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === 'j' || e.key === 'ArrowDown'){ e.preventDefault(); BL.emit('nav','next'); }
      else if (k === 'k' || e.key === 'ArrowUp'){ e.preventDefault(); BL.emit('nav','prev'); }
      else if (k === 'n'){ BL.emit('nav','next-pending'); }
      else if (k === 'm'){ BL.emit('nav','next-format'); }
      else if (k === 'c'){ BL.emit('nav','next-changed'); }
    });
  }

  /* ---------------- Boot ---------------- */
  wireExportMenu();
  wireSegmented();
  wireNav();
  wireKeys();
  wireGroups();
  wireSearch();
  wireRailButtons();
  wireInspector();
  wireNavKeys();
  loadMeta().then(() => {
    loadDecisions();
    refreshMetricsAndStatus();
    refreshExportMenuState();
    renderDoc();
    renderRail();
  }).catch(err => {
    const banner = document.getElementById('v2-stage-banner');
    if (banner) banner.textContent = 'Metadata unavailable (' + err + ')';
  });
})();
"""


def build_review_shell_v2(run_id: str) -> str:
    """Render the v2 review shell skeleton.

    Scaffolding stage: proves the ?v=2 route + design tokens + /api/runs/<id>
    wiring. Subsequent commits fill in the top bar, left rail, document stage,
    and slide-in inspector per the dense-pro design spec.
    """
    escaped_run_id = html.escape(run_id)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Review Run {escaped_run_id} · v2</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter+Tight:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>{_REVIEW_V2_STYLES}</style>
</head>
<body>
  <div class="app" id="app">
    <header class="top" role="banner">
      <div class="top-sec brand">
        <div class="brand-mark"></div>
        <div class="brand-name">Blackline</div>
        <div class="brand-tag">Compare</div>
      </div>

      <div class="top-sec metrics" aria-label="Run metrics">
        <span class="lbl">Run</span>
        <span class="metric"><span class="k">t</span><span class="v" id="m-t">—</span></span>
        <span class="hsep"></span>
        <span class="metric"><span class="k">vis</span><span class="v" id="m-vis">0/0</span></span>
        <span class="hsep"></span>
        <span class="metric"><span class="k">pend</span><span class="v pending" id="m-pend">0</span></span>
      </div>

      <div class="top-sec export-sec">
        <div class="menu-wrap" id="exportWrap">
          <button class="btn primary" id="exportBtn" aria-haspopup="menu" aria-expanded="false">
            Export
            <svg class="caret" width="8" height="8" viewBox="0 0 8 8" aria-hidden="true"><path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round"></path></svg>
          </button>
          <div class="menu" id="exportMenu" role="menu">
            <div class="mini">Final document</div>
            <button type="button" data-export="final-docx">Final .docx <span class="kb">⌘E</span></button>
            <div class="sep"></div>
            <div class="mini">Raw compare</div>
            <button type="button" data-export="docx">Raw .docx</button>
            <button type="button" data-export="html">Raw .html</button>
            <button type="button" data-export="json">Raw .json</button>
            <button type="button" data-export="pdf">Raw .pdf</button>
          </div>
        </div>
        <button class="btn" id="btn-zen" type="button">Zen</button>
      </div>

      <div class="top-sec nav" aria-label="Section navigation">
        <span class="lbl">Nav</span>
        <button class="btn" id="btn-prev-section" type="button" aria-label="Previous change">Prev <span class="kb">K</span></button>
        <button class="btn" id="btn-next-section" type="button" aria-label="Next change">Next <span class="kb">J</span></button>
        <input class="jump" id="jump-index" type="number" min="1" step="1" inputmode="numeric" placeholder="¶" aria-label="Jump to paragraph" />
        <span class="kb" aria-hidden="true">G</span>
      </div>

      <div class="top-sec right">
        <button class="btn ghost" id="btn-shortcuts" type="button">Shortcuts</button>
        <div class="segmented" role="group" aria-label="View mode">
          <button type="button" data-view="inline" id="btn-inline" aria-pressed="true">Inline</button>
          <button type="button" data-view="split" id="btn-split" aria-pressed="false">Split</button>
          <button type="button" data-view="tri" id="btn-tri" aria-pressed="false">Tri</button>
        </div>
      </div>
    </header>

    <div class="body">
      <aside class="rail" id="rail">
        <div class="rail-search">
          <div class="search">
            <input id="search" type="search" placeholder="Search changes… (/)" aria-label="Search changes" />
          </div>
          <div class="search-hint">
            <span>search</span><span class="kb">/</span>
            <span style="margin-left:6px">browse</span><span class="kb">B</span>
            <span style="margin-left:6px">jump</span><span class="kb">G</span>
          </div>
        </div>

        <div class="rail-scroll" id="rail-scroll">

          <!-- SCOPE -->
          <div class="group" data-group="scope">
            <button class="group-h" type="button" aria-expanded="true">
              <span class="caret" aria-hidden="true"><svg width="8" height="8" viewBox="0 0 8 8"><path d="M2 3l2 2 2-2" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg></span>
              Scope
              <span class="count" id="scope-count">0 / 0</span>
            </button>
            <div class="group-body" style="flex-direction:column;align-items:stretch;gap:4px">
              <button class="chip scope-chip" type="button" id="format-only-toggle" aria-pressed="false">
                <span>Formatting-only</span><span class="n" id="format-only-count">0/0</span>
              </button>
              <div class="row-btns" style="padding:0;">
                <button class="btn" id="next-pending-btn" type="button">Next pend <span class="kb">N</span></button>
                <button class="btn" id="next-format-btn" type="button">Next fmt <span class="kb">M</span></button>
              </div>
              <button class="btn" id="next-changed-btn" type="button" style="justify-content:space-between;height:24px;font-size:11px;">Next changed <span class="kb">C</span></button>
            </div>
            <div class="scope-progress"><div id="scope-progress-fill"></div></div>
          </div>

          <!-- TYPE -->
          <div class="group" data-group="type">
            <button class="group-h" type="button" aria-expanded="true">
              <span class="caret" aria-hidden="true"><svg width="8" height="8" viewBox="0 0 8 8"><path d="M2 3l2 2 2-2" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg></span>
              Type
              <span class="count" id="type-count">0 types</span>
            </button>
            <div class="group-body" id="filter-row"></div>
          </div>

          <!-- FACETS -->
          <div class="group" data-group="facet">
            <button class="group-h" type="button" aria-expanded="true">
              <span class="caret" aria-hidden="true"><svg width="8" height="8" viewBox="0 0 8 8"><path d="M2 3l2 2 2-2" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg></span>
              Facets
              <span class="count" id="facet-count">0 filters</span>
            </button>
            <div class="group-body" id="facet-row"></div>
          </div>

          <!-- DECISIONS -->
          <div class="group" data-group="decisions">
            <button class="group-h" type="button" aria-expanded="true">
              <span class="caret" aria-hidden="true"><svg width="8" height="8" viewBox="0 0 8 8"><path d="M2 3l2 2 2-2" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg></span>
              Decisions
              <span class="count" id="decisions-count">0 / 0</span>
            </button>
            <div class="group-body" id="decision-row"></div>
            <div class="group-extras">
              <div class="row-btns col3">
                <button class="btn" id="bulk-accept" type="button">Accept vis</button>
                <button class="btn" id="bulk-reject" type="button">Reject vis</button>
                <button class="btn" id="bulk-clear" type="button">Clear vis</button>
              </div>
              <div class="row-btns">
                <button class="btn" id="bulk-undo" type="button">Undo last</button>
                <button class="btn accent" id="next-undecided-btn" type="button">Next undecided</button>
              </div>
              <div class="group-hint" id="next-undecided-note">No decisions pending.</div>
            </div>
          </div>

          <h3 class="section-list-h">Sections</h3>
          <div class="section-list" id="detail-list"></div>

        </div>
      </aside>
      <section class="stage" id="stage">
        <article class="doc" id="doc">
          <div class="stage-loading" id="v2-stage-banner">Loading run…</div>
        </article>

        <aside class="inspector" id="inspector" aria-hidden="true">
          <div class="insp-h">
            <div class="insp-h-main">
              <span class="status-pill" id="insp-pill">Pending</span>
              <h2>
                <span id="insp-title">Paragraph</span>
                <span class="ctx" id="insp-subtitle">Section —</span>
              </h2>
            </div>
            <button class="close" id="inspClose" type="button" aria-label="Close inspector">×</button>
          </div>
          <div class="insp-body">

            <div class="insp-section">
              <div class="label">Classification</div>
              <div class="tags" id="insp-tags"></div>
            </div>

            <div class="insp-section" id="insp-deltas-wrap">
              <div class="label">Formatting deltas</div>
              <div class="delta-box" id="insp-deltas"><span class="muted">No formatting changes.</span></div>
            </div>

            <div class="insp-section">
              <div class="label">Metadata</div>
              <div id="insp-meta"></div>
            </div>

            <div class="insp-section">
              <div class="label">Compare</div>
              <div class="diff-pair">
                <div class="diff-card orig">
                  <div class="dh">Original</div>
                  <div class="db" id="insp-original">—</div>
                </div>
                <div class="diff-card rev">
                  <div class="dh">Revised</div>
                  <div class="db" id="insp-revised">—</div>
                </div>
              </div>
            </div>

          </div>
          <div class="insp-foot">
            <div class="label">Decision</div>
            <div class="row">
              <button class="btn accept" id="dec-accept" type="button" data-action="accept" aria-pressed="false">Accept <span class="kb">A</span></button>
              <button class="btn" id="dec-reject" type="button" data-action="reject" aria-pressed="false">Reject <span class="kb">R</span></button>
              <button class="btn ghost" id="dec-clear" type="button" data-action="pending">Clear</button>
            </div>
          </div>
        </aside>
      </section>
    </div>

    <footer class="status" role="contentinfo">
      <span class="dot ok" aria-hidden="true"></span>
      <span>Compare complete</span>
      <span class="vsep"></span>
      <span id="s-files">— ↔ —</span>
      <span class="vsep"></span>
      <span id="s-changes">0 changes</span>
      <span class="vsep"></span>
      <span><span id="s-decided">0</span> decided · <span class="pending"><span id="s-pending">0</span> pending</span></span>
      <span class="spacer"></span>
      <span class="hint">Progress</span>
      <span class="minibar" aria-hidden="true"><span class="fill" id="s-progress"></span></span>
      <span id="s-progress-pct">0%</span>
      <span class="vsep"></span>
      <span class="hint">⌘K for commands</span>
    </footer>
  </div>
  <script>window.__BL_RUN_ID__ = {json.dumps(run_id)};</script>
  <script>{_REVIEW_V2_SCRIPT}</script>
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
    :root {{
      --bg: #ffffff;
      --canvas: #f7f7f8;
      --surface: #ffffff;
      --surface-2: #fafafa;
      --border: #e4e4e7;
      --border-strong: #d4d4d8;
      --text: #18181b;
      --text-muted: #52525b;
      --text-subtle: #71717a;
      --accent: #18181b;
      --accent-hover: #000000;
      --focus: #2563eb;
      --focus-ring: rgba(37, 99, 235, 0.2);
      --ok: #15803d;
      --ok-bg: #f0fdf4;
      --warn: #b45309;
      --bad: #b91c1c;
      --bad-bg: #fef2f2;
      --insert: #15803d;
      --delete: #b91c1c;
      --replace: #b45309;
      --move: #1d4ed8;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas, monospace;
      --header-h: 88px;
      --nav-w: 320px;
      --insp-w: 340px;
      --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
      --shadow-md: 0 4px 12px rgba(0,0,0,0.08);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; height: 100%; }}
    body {{
      font-family: var(--font-sans);
      font-size: 13px;
      line-height: 1.5;
      color: var(--text);
      background: var(--canvas);
      -webkit-font-smoothing: antialiased;
      overflow: hidden;
    }}
    kbd {{
      display: inline-block;
      font-family: var(--font-mono);
      font-size: 10px;
      padding: 1px 5px;
      border: 1px solid var(--border-strong);
      border-bottom-width: 2px;
      border-radius: 3px;
      background: var(--surface);
      color: var(--text-muted);
      line-height: 1.4;
      min-width: 16px;
      text-align: center;
    }}

    /* ============ HEADER ============ */
    .slim-header {{
      height: var(--header-h);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 16px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      gap: 16px;
      flex-wrap: nowrap;
    }}
    .header-left {{ display: flex; align-items: center; gap: 16px; flex: 1; min-width: 0; overflow: hidden; }}
    .header-brand {{
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.02em;
      color: var(--text);
      padding-right: 12px;
      border-right: 1px solid var(--border);
      height: 40px;
      line-height: 40px;
    }}
    .brand-mark {{
      width: 14px; height: 14px;
      background: var(--text);
      display: inline-block;
      border-radius: 2px;
    }}
    .icon-btn {{
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text-muted);
      width: 28px; height: 28px;
      border-radius: 4px;
      font-size: 13px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: background 100ms, color 100ms;
    }}
    .icon-btn:hover {{ background: var(--canvas); color: var(--text); }}
    .icon-btn-sm {{ width: 22px; height: 22px; font-size: 11px; }}

    .run-title {{ display: flex; flex-direction: column; gap: 2px; min-width: 0; }}
    .run-title-main {{ font-size: 12px; color: var(--text-muted); display: flex; gap: 4px; align-items: baseline; }}
    .run-title-main strong {{ color: var(--text); font-weight: 600; }}
    .run-slash {{ color: var(--text-subtle); }}
    .run-id {{ font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 180px; }}
    .run-title-pills {{ display: flex; gap: 8px; font-size: 11px; color: var(--text-muted); }}
    .sec-pill {{ font-family: var(--font-mono); }}
    .nav-progress, .decision-summary {{}}
    .decision-summary.is-pending {{ color: var(--warn); }}
    .decision-summary.is-complete {{ color: var(--ok); }}

    .run-context {{
      display: flex;
      gap: 0;
      margin-left: auto;
      border-left: 1px solid var(--border);
      padding-left: 16px;
      overflow: hidden;
    }}
    .context-metric {{
      display: flex;
      flex-direction: column;
      padding: 0 14px;
      border-right: 1px solid var(--border);
      min-width: 0;
    }}
    .context-metric:last-child {{ border-right: none; }}
    .context-metric-label {{
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-subtle);
    }}
    .context-metric-value {{
      font-size: 13px;
      font-weight: 600;
      color: var(--text);
      line-height: 1.3;
      white-space: nowrap;
    }}
    .context-metric-meta {{
      font-size: 10px;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .context-progress {{ height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; margin-top: 3px; }}
    .context-progress-fill {{ height: 100%; background: var(--text); width: 0%; transition: width 200ms; }}

    .header-right {{ display: flex; align-items: center; }}
    .command-bar {{ display: flex; align-items: center; gap: 12px; }}
    .command-group {{ display: flex; align-items: center; gap: 6px; }}
    .command-group-label {{
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 600;
      color: var(--text-subtle);
      margin-right: 4px;
    }}
    .primary-btn {{
      font-family: var(--font-sans);
      font-size: 12px;
      font-weight: 500;
      padding: 6px 12px;
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      border-radius: 4px;
      cursor: pointer;
      transition: background 100ms;
    }}
    .primary-btn:hover {{ background: var(--accent-hover); }}
    .primary-btn.export-btn {{}}
    #btn-zen {{ background: var(--surface); color: var(--text-muted); border-color: var(--border-strong); }}
    #btn-zen:hover {{ background: var(--canvas); color: var(--text); }}

    .pill-btn, .nav-command, .batch-switch-go, .jump-row button {{
      font-family: var(--font-sans);
      font-size: 12px;
      padding: 5px 10px;
      border: 1px solid var(--border-strong);
      background: var(--surface);
      color: var(--text);
      border-radius: 4px;
      cursor: pointer;
      transition: background 100ms, border-color 100ms;
    }}
    .pill-btn:hover, .nav-command:hover, .batch-switch-go:hover, .jump-row button:hover {{ background: var(--canvas); }}
    .nav-command {{ display: inline-flex; align-items: center; gap: 6px; }}
    .nav-command-hint {{ display: inline-flex; gap: 2px; opacity: 0.7; }}
    .nav-inline-hint {{ font-size: 10px; color: var(--text-subtle); display: inline-flex; gap: 4px; align-items: center; }}

    .jump-inline {{ display: inline-flex; align-items: center; gap: 6px; }}
    .jump-inline-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-subtle); font-weight: 600; }}
    .jump-row {{ display: inline-flex; gap: 4px; }}
    .jump-row input {{
      width: 70px;
      font-family: var(--font-mono);
      font-size: 12px;
      padding: 5px 8px;
      border: 1px solid var(--border-strong);
      border-radius: 4px;
    }}
    .jump-row input:focus {{ outline: none; border-color: var(--focus); box-shadow: 0 0 0 2px var(--focus-ring); }}

    .batch-switcher {{ display: inline-flex; align-items: center; gap: 6px; }}
    .batch-switcher[hidden] {{ display: none !important; }}
    .batch-switch-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-subtle); font-weight: 600; }}
    .batch-switch-select {{ font-size: 12px; padding: 4px 6px; border: 1px solid var(--border-strong); border-radius: 4px; background: var(--surface); max-width: 160px; }}
    .batch-switch-meta {{ font-size: 10px; color: var(--text-muted); }}

    .view-mode-segmented {{ display: inline-flex; border: 1px solid var(--border-strong); border-radius: 4px; overflow: hidden; }}
    .view-mode-option {{
      font-family: var(--font-sans);
      font-size: 12px;
      padding: 5px 12px;
      background: var(--surface);
      color: var(--text-muted);
      border: none;
      border-right: 1px solid var(--border);
      cursor: pointer;
      transition: background 100ms, color 100ms;
    }}
    .view-mode-option:last-child {{ border-right: none; }}
    .view-mode-option:hover {{ background: var(--canvas); color: var(--text); }}
    .view-mode-option.active {{ background: var(--text); color: #fff; }}

    .actions-group {{ display: inline-flex; gap: 4px; }}
    .actions-group a {{
      font-size: 11px;
      padding: 4px 8px;
      border: 1px solid var(--border);
      border-radius: 3px;
      color: var(--text-muted);
      text-decoration: none;
      font-family: var(--font-mono);
    }}
    .actions-group a:hover {{ background: var(--canvas); color: var(--text); }}

    /* ============ STAGE ============ */
    .stage {{
      position: relative;
      display: grid;
      /* Navigator on the LEFT, document preview on the right. */
      grid-template-columns: var(--nav-w) 1fr;
      height: calc(100vh - var(--header-h));
      overflow: hidden;
    }}
    .floating-navigator {{ grid-column: 1; grid-row: 1; border-right: 1px solid var(--border); border-left: none; }}
    .preview-shell {{ grid-column: 2; grid-row: 1; border-right: none; }}
    body.nav-hidden .stage {{ grid-template-columns: 0 1fr; }}
    body.nav-hidden .floating-navigator {{ display: none; }}

    /* ============ PREVIEW ============ */
    .preview-shell {{
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border-right: 1px solid var(--border);
      background: var(--canvas);
    }}
    .preview-chrome {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 14px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      font-size: 11px;
      color: var(--text-muted);
    }}
    .preview-title {{ display: inline-flex; align-items: center; gap: 6px; font-weight: 500; }}
    .preview-dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--ok); display: inline-block; }}
    .preview-mode strong {{ color: var(--text); font-weight: 600; margin-left: 4px; }}
    .preview-body {{ flex: 1; position: relative; overflow: hidden; }}
    #frame {{ width: 100%; height: 100%; border: none; display: block; background: var(--canvas); }}
    .minimap {{
      position: absolute;
      top: 0; right: 0; bottom: 0;
      width: 8px;
      background: var(--surface);
      border-left: 1px solid var(--border);
    }}
    .minimap-tick {{
      position: absolute;
      left: 1px; right: 1px;
      height: 3px;
      border-radius: 1px;
      cursor: pointer;
      opacity: 0.6;
    }}
    .minimap-tick:hover, .minimap-tick.active {{ opacity: 1; }}
    .minimap-tick.insert {{ background: var(--insert); }}
    .minimap-tick.delete {{ background: var(--delete); }}
    .minimap-tick.replace {{ background: var(--replace); }}
    .minimap-tick.move {{ background: var(--move); }}

    /* ============ NAVIGATOR ============ */
    .floating-navigator {{
      display: flex;
      flex-direction: column;
      background: var(--surface);
      overflow: hidden;
      min-width: 0;
    }}
    .nav-search {{ padding: 10px 12px; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; gap: 10px; }}
    .nav-section {{ display: flex; flex-direction: column; gap: 6px; }}
    .nav-section-title {{
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-subtle);
    }}
    #search {{
      font-family: var(--font-sans);
      font-size: 12px;
      padding: 6px 10px;
      border: 1px solid var(--border-strong);
      border-radius: 4px;
      background: var(--surface);
      width: 100%;
    }}
    #search:focus {{ outline: none; border-color: var(--focus); box-shadow: 0 0 0 2px var(--focus-ring); }}
    .find-hint {{ font-size: 10px; color: var(--text-subtle); display: flex; gap: 3px; align-items: center; flex-wrap: wrap; }}
    .dist-bar {{ display: flex; height: 6px; border-radius: 3px; overflow: hidden; background: var(--border); }}
    .dist-segment {{ height: 100%; }}
    .dist-ins {{ background: var(--insert); }}
    .dist-del {{ background: var(--delete); }}
    .dist-rep {{ background: var(--replace); }}
    .dist-mov {{ background: var(--move); }}
    .dist-unc {{ background: var(--border-strong); }}
    .quick-row {{ display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }}
    .quick-btn {{
      font-family: var(--font-sans);
      font-size: 11px;
      padding: 4px 8px;
      border: 1px solid var(--border-strong);
      background: var(--surface);
      color: var(--text);
      border-radius: 3px;
      cursor: pointer;
    }}
    .quick-btn:hover {{ background: var(--canvas); }}
    .quick-btn.active {{ background: var(--text); color: #fff; border-color: var(--text); }}
    .quick-btn.subtle {{ color: var(--text-muted); }}
    .quick-count {{ font-size: 10px; color: var(--text-subtle); font-family: var(--font-mono); margin-left: auto; }}

    .filter-group {{ padding: 8px 12px; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; gap: 6px; }}
    .filter-label {{
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-subtle);
    }}
    .filters-scroll {{ display: flex; flex-wrap: wrap; gap: 4px; }}
    .filter-btn, .facet-btn, .decision-btn {{
      font-family: var(--font-sans);
      font-size: 11px;
      padding: 3px 8px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text-muted);
      border-radius: 3px;
      cursor: pointer;
      transition: all 100ms;
    }}
    .filter-btn:hover, .facet-btn:hover, .decision-btn:hover {{ background: var(--canvas); color: var(--text); }}
    .filter-btn.active, .facet-btn.active, .decision-btn.active {{ background: var(--text); color: #fff; border-color: var(--text); }}

    .bulk-row {{ padding: 8px 12px; border-bottom: 1px solid var(--border); display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }}
    .bulk-btn {{
      font-family: var(--font-sans);
      font-size: 11px;
      padding: 4px 8px;
      border: 1px solid var(--border-strong);
      background: var(--surface);
      color: var(--text);
      border-radius: 3px;
      cursor: pointer;
    }}
    .bulk-btn:hover {{ background: var(--canvas); }}
    .bulk-status {{ font-size: 10px; color: var(--text-muted); margin-left: auto; }}
    .bulk-status.error {{ color: var(--bad); }}

    .decision-guide {{ padding: 8px 12px; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; gap: 4px; }}
    .decision-guide-note {{ font-size: 10px; color: var(--text-subtle); }}

    .change-list-head {{
      padding: 10px 12px 6px;
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      border-bottom: 1px solid var(--border);
    }}
    .change-list-title {{ font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-subtle); }}
    .change-list-note {{ font-size: 10px; color: var(--text-subtle); }}
    .change-list {{ flex: 1; overflow-y: auto; padding: 4px 0; }}

    .detail-card {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 4px;
      border-left: 2px solid transparent;
      transition: background 100ms;
    }}
    .detail-card:hover {{ background: var(--canvas); }}
    .detail-card.active {{ background: #eff6ff; border-left-color: var(--focus); }}
    .detail-card.kind-insert {{ border-left-color: var(--insert); }}
    .detail-card.kind-delete {{ border-left-color: var(--delete); }}
    .detail-card.kind-replace {{ border-left-color: var(--replace); }}
    .detail-card.kind-move {{ border-left-color: var(--move); }}
    .detail-card.active.kind-insert,
    .detail-card.active.kind-delete,
    .detail-card.active.kind-replace,
    .detail-card.active.kind-move {{ border-left-width: 3px; }}

    .detail-title {{ display: flex; gap: 8px; align-items: baseline; }}
    .detail-index {{
      font-family: var(--font-mono);
      font-size: 10px;
      color: var(--text-subtle);
      min-width: 24px;
    }}
    .detail-title-text {{ font-size: 12px; font-weight: 500; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .detail-meta {{ display: flex; justify-content: space-between; gap: 8px; font-size: 10px; color: var(--text-muted); padding-left: 32px; }}
    .detail-meta-left {{ display: flex; gap: 4px; align-items: center; }}
    .detail-kind {{ text-transform: uppercase; letter-spacing: 0.04em; font-weight: 500; }}
    .detail-dot {{ color: var(--text-subtle); }}
    .detail-sec {{ font-family: var(--font-mono); }}
    .detail-excerpt {{ font-size: 11px; color: var(--text-muted); padding-left: 32px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .facet-badges {{ display: flex; flex-wrap: wrap; gap: 3px; padding-left: 32px; }}
    .facet-badge {{
      font-size: 9px;
      padding: 1px 5px;
      border: 1px solid var(--border);
      border-radius: 2px;
      background: var(--canvas);
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .status-chip, .status-chips {{ display: inline-flex; gap: 3px; align-items: center; font-size: 10px; }}
    .decision-tag {{
      text-transform: uppercase;
      letter-spacing: 0.04em;
      font-weight: 600;
      padding: 1px 5px;
      border-radius: 2px;
      font-size: 9px;
    }}
    .decision-tag.accept {{ background: var(--ok-bg); color: var(--ok); }}
    .decision-tag.reject {{ background: var(--bad-bg); color: var(--bad); }}
    .decision-tag.pending {{ background: var(--canvas); color: var(--text-muted); }}

    .empty-state {{ padding: 24px 16px; text-align: center; font-size: 12px; color: var(--text-subtle); }}

    /* ============ INSPECTOR ============ */
    .floating-inspector {{
      position: fixed;
      /* Navigator is on the left now — inspector floats at bottom-right. */
      right: 16px;
      bottom: 12px;
      width: var(--insp-w);
      max-height: calc(100vh - var(--header-h) - 24px);
      background: var(--surface);
      border: 1px solid var(--border-strong);
      border-radius: 6px;
      box-shadow: var(--shadow-md);
      display: none;
      flex-direction: column;
      z-index: 30;
      overflow: hidden;
    }}
    .floating-inspector.visible {{ display: flex; }}
    .insp-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 8px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
    }}
    .insp-head-copy h3 {{ margin: 0; font-size: 13px; font-weight: 600; }}
    .insp-subtitle {{ font-size: 11px; color: var(--text-muted); margin-top: 2px; }}
    .insp-body {{ padding: 12px 14px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; font-size: 12px; }}
    .insp-label-row {{ display: flex; flex-direction: column; gap: 2px; }}
    .insp-label {{ font-weight: 500; font-size: 12px; }}
    .insp-label-subtext {{ font-size: 10px; color: var(--text-subtle); font-family: var(--font-mono); }}
    .insp-facets-wrap {{ display: flex; flex-direction: column; gap: 8px; }}
    .insp-facets {{ display: flex; flex-wrap: wrap; gap: 3px; }}
    .insp-divider {{ height: 1px; background: var(--border); }}
    .diff-block {{ display: flex; flex-direction: column; gap: 4px; }}
    .diff-hdr {{ font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-subtle); }}
    .diff-content {{
      font-family: ui-serif, Georgia, "Times New Roman", serif;
      font-size: 12px;
      line-height: 1.5;
      padding: 8px 10px;
      border: 1px solid var(--border);
      border-radius: 4px;
      background: var(--surface-2);
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .diff-block-original .diff-content {{ border-left: 2px solid var(--delete); }}
    .diff-block-revised .diff-content {{ border-left: 2px solid var(--insert); }}
    .diff-block-meta .diff-content {{ font-family: var(--font-mono); font-size: 11px; }}
    .insp-action-wrap {{ display: flex; flex-direction: column; gap: 4px; padding-top: 6px; border-top: 1px solid var(--border); }}
    .insp-action-label {{ font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-subtle); }}
    .insp-actions {{ display: flex; gap: 4px; }}
    .insp-action-btn {{
      flex: 1;
      font-family: var(--font-sans);
      font-size: 12px;
      padding: 5px 8px;
      border: 1px solid var(--border-strong);
      background: var(--surface);
      color: var(--text);
      border-radius: 4px;
      cursor: pointer;
    }}
    .insp-action-btn:hover:not(:disabled) {{ background: var(--canvas); }}
    .insp-action-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .insp-action-btn.is-current {{ background: var(--text); color: #fff; border-color: var(--text); }}
    .insp-no-action {{ font-size: 11px; color: var(--text-subtle); font-style: italic; }}

    /* ============ ZEN ============ */
    body.zen-mode .slim-header,
    body.zen-mode .floating-navigator,
    body.zen-mode .kbd-hints {{ display: none !important; }}
    body.zen-mode .stage {{ grid-template-columns: 1fr; height: 100vh; }}
    .zen-exit {{
      position: fixed;
      top: 12px;
      right: 12px;
      display: none;
      font-family: var(--font-sans);
      font-size: 12px;
      padding: 6px 12px;
      background: var(--text);
      color: #fff;
      border: none;
      border-radius: 4px;
      cursor: pointer;
      z-index: 40;
    }}
    body.zen-mode .zen-exit {{ display: block; }}

    /* ============ SHORTCUTS OVERLAY ============ */
    .shortcut-overlay {{
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.4);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 100;
      padding: 24px;
    }}
    .shortcut-overlay.open {{ display: flex; }}
    .shortcut-panel {{
      background: var(--surface);
      border: 1px solid var(--border-strong);
      border-radius: 6px;
      max-width: 820px;
      width: 100%;
      max-height: 85vh;
      overflow-y: auto;
      box-shadow: var(--shadow-md);
    }}
    .shortcut-head {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
    }}
    .shortcut-kicker {{
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--text-subtle);
      margin-bottom: 4px;
    }}
    .shortcut-title {{ margin: 0; font-size: 15px; font-weight: 600; }}
    .shortcut-subtitle {{ font-size: 12px; color: var(--text-muted); margin-top: 3px; }}
    .shortcut-actions {{ display: flex; align-items: center; gap: 10px; }}
    .shortcut-legend {{ font-size: 10px; color: var(--text-subtle); }}
    .shortcut-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0; }}
    .shortcut-section {{ padding: 14px 20px; border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); }}
    .shortcut-section:nth-child(2n) {{ border-right: none; }}
    .shortcut-section-head {{ margin-bottom: 10px; }}
    .shortcut-section-eyebrow {{
      font-size: 10px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-subtle);
    }}
    .shortcut-section-title {{ margin: 2px 0 4px; font-size: 13px; font-weight: 600; }}
    .shortcut-section-copy {{ margin: 0; font-size: 11px; color: var(--text-muted); }}
    .shortcut-list {{ display: flex; flex-direction: column; gap: 10px; }}
    .shortcut-item {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }}
    .shortcut-copy {{ display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }}
    .shortcut-label {{ font-size: 12px; font-weight: 500; }}
    .shortcut-meta {{ font-size: 11px; color: var(--text-muted); line-height: 1.4; }}
    .shortcut-keyset {{ display: flex; gap: 6px; align-items: center; flex-shrink: 0; }}
    .shortcut-seq {{ display: inline-flex; gap: 2px; align-items: center; }}
    .shortcut-key {{
      font-family: var(--font-mono);
      font-size: 10px;
      padding: 2px 6px;
      border: 1px solid var(--border-strong);
      border-bottom-width: 2px;
      border-radius: 3px;
      background: var(--surface);
      color: var(--text);
      min-width: 18px;
      text-align: center;
    }}
    .shortcut-key.ghost {{ background: var(--canvas); color: var(--text-muted); }}
    .shortcut-key.modifier {{ padding: 2px 8px; }}
    .shortcut-join {{ color: var(--text-subtle); font-size: 10px; }}
    .shortcut-or {{ color: var(--text-subtle); font-size: 10px; }}

    /* ============ KBD HINTS ============ */
    .kbd-hints {{
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      display: flex;
      gap: 12px;
      padding: 6px 14px;
      background: var(--surface);
      border-top: 1px solid var(--border);
      font-size: 10px;
      color: var(--text-muted);
      overflow-x: auto;
      white-space: nowrap;
      z-index: 10;
    }}
    .kbd-hint {{ display: inline-flex; gap: 3px; align-items: center; flex-shrink: 0; }}
    body.zen-mode .kbd-hints,
    body.shortcuts-open .kbd-hints {{ display: none; }}

    /* Reduce content padding to make room for fixed kbd-hints */
    .stage {{ padding-bottom: 28px; }}
    body.zen-mode .stage {{ padding-bottom: 0; }}

    /* ============ PASS 2 SIMPLIFICATIONS ============ */
    /* Drop the bottom keyboard-hints strip — the Shortcuts dialog covers this */
    .kbd-hints {{ display: none !important; }}
    .stage {{ padding-bottom: 0; }}
    /* Drop the preview chrome bar — the document is self-evident */
    .preview-chrome {{ display: none; }}
    /* Keep only the two metrics that actually guide a review pass */
    .run-context .context-metric:nth-child(1),
    .run-context .context-metric:nth-child(2),
    .run-context .context-metric:nth-child(3) {{ display: none; }}

    .pulse, .scope-shift, .selection-glow {{}}

    /* ============ RESPONSIVE ============ */
    @media (max-width: 1200px) {{
      .run-context {{ display: none; }}
      :root {{ --header-h: 56px; }}
    }}
    @media (max-width: 900px) {{
      :root {{ --nav-w: 280px; }}
      .command-group-label, .nav-inline-hint, .command-group--nav .jump-inline-label {{ display: none; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      * {{ animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }}
    }}
  </style>
</head>
<body>
  <header class="slim-header" id="header">
    <div class="header-left">
      <span class="header-brand" aria-hidden="true"><span class="brand-mark"></span><span>Blackline</span></span>
      <button id="btn-nav" class="icon-btn">☰</button>
      <div class="run-title">
        <div class="run-title-main"><strong>Review Run</strong><span class="run-slash">/</span><span id="r-title" class="run-id">...</span></div>
        <div class="run-title-pills">
          <span id="sec-pill" class="sec-pill">sec -</span>
          <span id="nav-progress" class="nav-progress">Visible 0/0 · 0 changed</span>
          <span id="decision-summary" class="decision-summary">Decision 0/0 decided · 0 pending</span>
        </div>
      </div>
      <div class="run-context" aria-label="Run telemetry">
        <div class="context-metric">
          <span class="context-metric-label">Profile</span>
          <strong id="run-profile-pill" class="context-metric-value">Default</strong>
          <span class="context-metric-meta">Active compare profile</span>
        </div>
        <div class="context-metric">
          <span class="context-metric-label">Run Scope</span>
          <strong id="run-sections-pill" class="context-metric-value">0 total</strong>
          <span id="run-sections-meta" class="context-metric-meta">0 changed sections</span>
        </div>
        <div class="context-metric">
          <span class="context-metric-label">Visible</span>
          <strong id="run-visible-pill" class="context-metric-value">0/0</strong>
          <span id="run-visible-meta" class="context-metric-meta">No sections in current scope</span>
        </div>
        <div class="context-metric">
          <span class="context-metric-label">Pending</span>
          <strong id="run-pending-pill" class="context-metric-value">0 open</strong>
          <span id="run-pending-meta" class="context-metric-meta">No pending decisions</span>
        </div>
        <div class="context-metric context-metric--progress">
          <span class="context-metric-label">Decision Coverage</span>
          <strong id="run-decision-pill" class="context-metric-value">0% decided</strong>
          <div class="context-progress"><span id="run-progress-fill" class="context-progress-fill"></span></div>
          <span id="run-progress-label" class="context-metric-meta">0/0 decided · review not started</span>
        </div>
      </div>
    </div>
      <div class="header-right">
        <div class="command-bar">
          <div class="command-group command-group--primary">
            <button id="btn-export" class="primary-btn export-btn">Export Final Doc</button>
            <div id="dl-group" class="actions-group primary-actions"></div>
            <button id="btn-zen" class="primary-btn">Zen Mode</button>
          </div>
          <div class="command-group command-group--nav" role="group" aria-label="Section navigation">
            <span class="command-group-label">Navigate</span>
            <button id="btn-prev-section" class="nav-command" type="button" aria-label="Previous visible section" aria-keyshortcuts="K">
              <span>Prev</span>
              <span class="nav-command-hint"><kbd>K</kbd><kbd>↑</kbd></span>
            </button>
            <button id="btn-next-section" class="nav-command" type="button" aria-label="Next visible section" aria-keyshortcuts="J">
              <span>Next</span>
              <span class="nav-command-hint"><kbd>J</kbd><kbd>↓</kbd></span>
            </button>
            <div class="jump-inline">
              <span class="jump-inline-label">Jump</span>
              <div class="jump-row">
                <input id="jump-index" type="number" min="1" step="1" inputmode="numeric" placeholder="Section # (G)" aria-label="Jump to section number" aria-keyshortcuts="G" />
                <button id="jump-btn" type="button" aria-label="Jump to section">Go</button>
              </div>
            </div>
            <span class="nav-inline-hint"><kbd>G</kbd> jump <span aria-hidden="true">·</span> <kbd>Enter</kbd> go</span>
          </div>
          <div class="command-group command-group--secondary">
            <div id="batch-switcher" class="batch-switcher" hidden>
              <span class="batch-switch-label">Batch</span>
              <select id="batch-run-select" class="batch-switch-select" aria-label="Switch revised version"></select>
              <span id="batch-switch-meta" class="batch-switch-meta"></span>
            <button id="batch-run-go" class="batch-switch-go" type="button">Open</button>
          </div>
          <button id="btn-shortcuts" class="pill-btn shortcut-launch" type="button">Shortcuts</button>
          <div class="view-mode-segmented" role="radiogroup" aria-label="Review preview mode">
            <button id="btn-inline" type="button" class="view-mode-option active" role="radio" aria-checked="true" aria-label="Inline mode" data-view-mode="inline">Inline</button>
            <button id="btn-split" type="button" class="view-mode-option" role="radio" aria-checked="false" aria-label="Split mode" data-view-mode="split">Split</button>
            <button id="btn-tri" type="button" class="view-mode-option" role="radio" aria-checked="false" aria-label="Tri-pane mode" data-view-mode="tri">Tri</button>
          </div>
        </div>
      </div>
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
            <div class="find-hint"><kbd>/</kbd> search <span aria-hidden="true">·</span> <kbd>J</kbd><kbd>K</kbd> browse <span aria-hidden="true">·</span> <kbd>G</kbd> jump</div>
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
          <span class="change-list-note">Select to inspect or use J/K</span>
        </div>
        <div id="detail-list" class="change-list"></div>
      </aside>
    <div class="floating-inspector" id="inspector">
      <div class="insp-head">
        <div class="insp-head-copy">
          <h3 id="insp-title">Change</h3>
          <div id="insp-subtitle" class="insp-subtitle">Section Details</div>
        </div>
        <button id="close-insp" class="icon-btn icon-btn-sm" aria-label="Close inspector">✕</button>
      </div>
      <div id="insp-body" class="insp-body"></div>
    </div>
    <button id="btn-exit-zen" class="zen-exit">Exit Zen Mode (Esc)</button>
    <div id="shortcut-overlay" class="shortcut-overlay" hidden>
      <div class="shortcut-panel" role="dialog" aria-modal="true" aria-labelledby="shortcut-title" aria-describedby="shortcut-subtitle">
        <div class="shortcut-head">
          <div>
            <div class="shortcut-kicker">Shortcut layer</div>
            <h3 id="shortcut-title" class="shortcut-title">Keyboard controls, grouped by intent</h3>
            <div id="shortcut-subtitle" class="shortcut-subtitle">Move through sections, make decisions, and change scope without breaking review flow.</div>
          </div>
          <div class="shortcut-actions">
            <div class="shortcut-legend">Primary keys carry the action</div>
            <button id="shortcut-close" class="icon-btn icon-btn-sm" type="button" aria-label="Close shortcut overlay">✕</button>
          </div>
        </div>
        <div class="shortcut-grid">
          <section class="shortcut-section">
            <div class="shortcut-section-head">
              <div class="shortcut-section-eyebrow">Navigate</div>
              <h4 class="shortcut-section-title">Move through the current scope</h4>
              <p class="shortcut-section-copy">These stay locked to whatever search and filters are active.</p>
            </div>
            <div class="shortcut-list">
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Next or previous visible section</span>
                  <span class="shortcut-meta">Step through the filtered list without reaching for the mouse.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key">J</kbd><span class="shortcut-or">or</span><kbd class="shortcut-key ghost">↓</kbd></span>
                  <span class="shortcut-seq"><kbd class="shortcut-key">K</kbd><span class="shortcut-or">or</span><kbd class="shortcut-key ghost">↑</kbd></span>
                </div>
              </div>
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Jump straight to a section</span>
                  <span class="shortcut-meta">Focus the jump field, then confirm the target section.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key">G</kbd></span>
                  <span class="shortcut-seq"><kbd class="shortcut-key ghost">Enter</kbd></span>
                </div>
              </div>
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Hop to pending, format-only, or changed sections</span>
                  <span class="shortcut-meta">Use the lane that best matches the pass you are making.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key">N</kbd></span>
                  <span class="shortcut-seq"><kbd class="shortcut-key">M</kbd></span>
                  <span class="shortcut-seq"><kbd class="shortcut-key">C</kbd></span>
                </div>
              </div>
            </div>
          </section>
          <section class="shortcut-section">
            <div class="shortcut-section-head">
              <div class="shortcut-section-eyebrow">Decide</div>
              <h4 class="shortcut-section-title">Record section outcomes</h4>
              <p class="shortcut-section-copy">The action key is emphasized so the decision path reads left to right.</p>
            </div>
            <div class="shortcut-list">
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Accept, reject, or clear the current section</span>
                  <span class="shortcut-meta">Use clear when you want to move a changed section back to pending.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key">A</kbd></span>
                  <span class="shortcut-seq"><kbd class="shortcut-key">R</kbd></span>
                  <span class="shortcut-seq"><kbd class="shortcut-key">U</kbd></span>
                </div>
              </div>
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Undo the last decision change</span>
                  <span class="shortcut-meta">Works for the latest single or bulk decision action.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key modifier">Ctrl/Cmd</kbd><span class="shortcut-join">+</span><kbd class="shortcut-key">Z</kbd></span>
                </div>
              </div>
            </div>
          </section>
          <section class="shortcut-section">
            <div class="shortcut-section-head">
              <div class="shortcut-section-eyebrow">Scope</div>
              <h4 class="shortcut-section-title">Search and shift filters quickly</h4>
              <p class="shortcut-section-copy">Keep the active review lane in view, then use arrows once a filter row is focused.</p>
            </div>
            <div class="shortcut-list">
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Focus search</span>
                  <span class="shortcut-meta">Start a fresh query or jump back into the existing one.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key">/</kbd></span>
                  <span class="shortcut-or">or</span>
                  <span class="shortcut-seq"><kbd class="shortcut-key modifier">Ctrl/Cmd</kbd><span class="shortcut-join">+</span><kbd class="shortcut-key">K</kbd></span>
                </div>
              </div>
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Toggle the formatting-only filter</span>
                  <span class="shortcut-meta">Useful for cleanup passes after decision-heavy review is done.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key">F</kbd></span>
                </div>
              </div>
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Focus kind, facet, or decision filters</span>
                  <span class="shortcut-meta">Press 1, 2, or 3, then use left and right arrows to move across the row.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key">1</kbd></span>
                  <span class="shortcut-seq"><kbd class="shortcut-key">2</kbd></span>
                  <span class="shortcut-seq"><kbd class="shortcut-key">3</kbd></span>
                </div>
              </div>
            </div>
          </section>
          <section class="shortcut-section">
            <div class="shortcut-section-head">
              <div class="shortcut-section-eyebrow">Workspace</div>
              <h4 class="shortcut-section-title">Adjust the shell around the review</h4>
              <p class="shortcut-section-copy">These controls change layout or dismiss chrome without altering the review state.</p>
            </div>
            <div class="shortcut-list">
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Cycle preview mode, toggle navigator, or enter Zen mode</span>
                  <span class="shortcut-meta">Use the shell mode that fits the pass you are on.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key">S</kbd></span>
                  <span class="shortcut-seq"><kbd class="shortcut-key">B</kbd></span>
                  <span class="shortcut-seq"><kbd class="shortcut-key">Z</kbd></span>
                </div>
              </div>
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Open or close this shortcut layer</span>
                  <span class="shortcut-meta">Bring the map back up any time you need a quick reminder.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key">?</kbd></span>
                </div>
              </div>
              <div class="shortcut-item">
                <div class="shortcut-copy">
                  <span class="shortcut-label">Dismiss overlays, inputs, or Zen mode</span>
                  <span class="shortcut-meta">Use escape to back out one level without changing decisions.</span>
                </div>
                <div class="shortcut-keyset">
                  <span class="shortcut-seq"><kbd class="shortcut-key ghost">Esc</kbd></span>
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
      <div class="kbd-hints">
        <div class="kbd-hint"><kbd>J</kbd> / <kbd>K</kbd> / <kbd>↑</kbd> / <kbd>↓</kbd> Nav</div>
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
        <div class="kbd-hint"><kbd>G</kbd> Jump</div>
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
    const BATCH_HISTORY_KEY = "blackline_batch_history_v1";
    const REVIEW_READER_CSS_ID = "review-reader-theme-typography-v1";
    const REVIEW_EDITOR_CSS_ID = "review-editor-theme-typography-v1";
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
      const prevSectionBtn = D.getElementById("btn-prev-section");
      const nextSectionBtn = D.getElementById("btn-next-section");
      const bulkStatus = D.getElementById("bulk-status");
      const bulkAcceptBtn = D.getElementById("bulk-accept");
      const bulkRejectBtn = D.getElementById("bulk-reject");
    const bulkClearBtn = D.getElementById("bulk-clear");
    const bulkUndoBtn = D.getElementById("bulk-undo");
    const formatOnlyToggle = D.getElementById("format-only-toggle");
    const formatOnlyCount = D.getElementById("format-only-count");
    const navProgress = D.getElementById("nav-progress");
    const previewModeLabel = D.getElementById("preview-mode-label");
    const viewModeButtons = [
      ["inline", D.getElementById("btn-inline")],
      ["split", D.getElementById("btn-split")],
      ["tri", D.getElementById("btn-tri")],
    ];
    const minimap = D.getElementById("minimap");
    const runProfilePill = D.getElementById("run-profile-pill");
    const runSectionsPill = D.getElementById("run-sections-pill");
    const runSectionsMeta = D.getElementById("run-sections-meta");
    const runVisiblePill = D.getElementById("run-visible-pill");
    const runVisibleMeta = D.getElementById("run-visible-meta");
    const runPendingPill = D.getElementById("run-pending-pill");
    const runPendingMeta = D.getElementById("run-pending-meta");
    const runDecisionPill = D.getElementById("run-decision-pill");
    const runProgressFill = D.getElementById("run-progress-fill");
    const runProgressLabel = D.getElementById("run-progress-label");
    const shortcutsBtn = D.getElementById("btn-shortcuts");
    const shortcutOverlay = D.getElementById("shortcut-overlay");
    const shortcutClose = D.getElementById("shortcut-close");
    const nextPendingBtn = D.getElementById("next-pending-btn");
    const nextFormatBtn = D.getElementById("next-format-btn");
    const nextChangedBtn = D.getElementById("next-changed-btn");
    const nextUndecidedBtn = D.getElementById("next-undecided-btn");
    const nextUndecidedNote = D.getElementById("next-undecided-note");
    const batchSwitcher = D.getElementById("batch-switcher");
    const batchRunSelect = D.getElementById("batch-run-select");
    const batchSwitchMeta = D.getElementById("batch-switch-meta");
    const batchRunGo = D.getElementById("batch-run-go");
    const editorThemeFromQuery = new URLSearchParams(window.location.search).get("theme") || "";
    const isEditorTheme = new Set(["editor", "dark", "monaco"]).has(editorThemeFromQuery.toLowerCase());
    function syncBodyState() {{
      body.classList.toggle("review-editor-theme", isEditorTheme);
      body.classList.toggle("zen-mode", s.zen);
      body.classList.toggle("nav-hidden", !s.zen && s.navOff);
    }}
    syncBodyState();

    function loadBatchHistory() {{
      try {{
        const raw = window.localStorage.getItem(BATCH_HISTORY_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? parsed : [];
      }} catch (_error) {{
        return [];
      }}
    }}

    function findBatchSession(meta) {{
      const history = loadBatchHistory();
      if (!history.length) return null;
      for (const session of history) {{
        if (!session || !Array.isArray(session.items)) continue;
        if (session.items.some((item) => item && item.run_id === runId)) return session;
      }}
      if (!meta || !meta.original_name) return null;
      return history.find((session) => (
        session &&
        session.original_name === meta.original_name &&
        Array.isArray(session.items) &&
        session.items.length > 1
      )) || null;
    }}

    function applyReaderThemeRhythm() {{
      // Intentionally a no-op: the document stylesheet (from core.py) drives the
      // Word-like look. Prior versions injected heavy glass/serif styles here;
      // those frames and shadows fought the document and are removed.
      if (!frame.contentDocument) return;
      const doc = frame.contentDocument;
      const existingReader = doc.getElementById(REVIEW_READER_CSS_ID);
      const existingEditor = doc.getElementById(REVIEW_EDITOR_CSS_ID);
      if (existingReader) existingReader.remove();
      if (existingEditor) existingEditor.remove();
    }}

    function setBatchSwitcher(meta) {{
      if (!batchSwitcher || !batchRunSelect || !batchRunGo) return;
      const session = findBatchSession(meta);
      if (!session || !Array.isArray(session.items)) {{
        batchSwitcher.hidden = true;
        batchRunSelect.innerHTML = "";
        if (batchSwitchMeta) batchSwitchMeta.textContent = "";
        return;
      }}
      const items = session.items.filter((item) => item && item.run_id);
      if (items.length < 2) {{
        batchSwitcher.hidden = true;
        batchRunSelect.innerHTML = "";
        if (batchSwitchMeta) batchSwitchMeta.textContent = "";
        return;
      }}
      batchRunSelect.innerHTML = items.map((item, idx) => {{
        const rev = item.revised_name || `Version ${{idx + 1}}`;
        const current = item.run_id === runId ? " (current)" : "";
        return `<option value="${{enc(item.run_id)}}">${{idx + 1}}. ${{enc(rev)}}${{current}}</option>`;
      }}).join("");
      if (items.some((item) => item.run_id === runId)) {{
        batchRunSelect.value = runId;
      }} else {{
        batchRunSelect.value = items[0].run_id;
      }}
      if (batchSwitchMeta) {{
        batchSwitchMeta.textContent = `${{items.length}} versions`;
      }}
      batchRunGo.disabled = batchRunSelect.value === runId;
      batchSwitcher.hidden = false;
    }}

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
        updateNavControlState(sections);
        const changedVisible = sections.filter(sec => sec.is_changed).length;
        const selIndex = sections.findIndex(sec => sec.index === s.sel);
        const currentVisible = sections.length ? (selIndex >= 0 ? selIndex + 1 : 1) : 0;
        navProgress.textContent = `Visible ${{currentVisible}}/${{sections.length}} · ${{changedVisible}} changed`;
        if (runVisiblePill) {{
          runVisiblePill.textContent = `${{currentVisible}}/${{sections.length}}`;
        }}
        if (runVisibleMeta) {{
          if (!sections.length) {{
            runVisibleMeta.textContent = "No sections in current scope";
          }} else if (!changedVisible) {{
            runVisibleMeta.textContent = "Current scope is unchanged only";
          }} else {{
            runVisibleMeta.textContent = `${{changedVisible}} changed in current scope`;
          }}
        }}
      }}

      function updateNavControlState(sections = null) {{
        const visibleSections = Array.isArray(sections) ? sections : fSec();
        const disabled = !visibleSections.length;
        [prevSectionBtn, nextSectionBtn, jumpBtn].forEach(btn => {{
          if (!btn) return;
          btn.disabled = disabled;
        }});
        if (jumpInput) {{
          jumpInput.disabled = disabled;
        }}
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
        badges.push('<span class="facet-badge format-only">Format-only</span>');
      }}
      const formatFacets = sectionFormatFacets(sec).filter(facet => facet !== "formatting");
      formatFacets.slice(0, 4).forEach(facet => {{
        badges.push(`<span class="facet-badge">${{enc(FACET_LABELS[facet] || facet)}}</span>`);
      }});
      return badges.join("");
    }}
    
    function applyViewMode(mode) {{
      if (!VIEW_ORDER.includes(mode)) return;
      s.viewMode = mode;
      const label = VIEW_LABELS[mode] || mode;
      const legacyBtn = D.getElementById("btn-split");
      if (legacyBtn && !legacyBtn.classList.contains("view-mode-option")) {{
        legacyBtn.textContent = `View: ${{label}}`;
      }}
      if (previewModeLabel) {{
        previewModeLabel.textContent = label;
      }}
      viewModeButtons.forEach(([modeKey, button]) => {{
        if (!button) return;
        const active = modeKey === mode;
        button.classList.toggle("active", active);
        button.setAttribute("aria-checked", active ? "true" : "false");
        button.setAttribute("aria-pressed", active ? "true" : "false");
      }});
      if (s.iframe && s.iframe.body) {{
        s.iframe.body.classList.remove("view-inline", "view-split", "view-tri");
        s.iframe.body.classList.add(`view-${{mode}}`);
      }}
    }}

    function cycleViewMode() {{
      const currentIndex = VIEW_ORDER.indexOf(s.viewMode);
      const nextMode = VIEW_ORDER[(currentIndex + 1) % VIEW_ORDER.length];
      applyViewMode(nextMode);
      if (s.sel) syncFrame(s.sel);
    }}

    function setShortcutOverlay(open) {{
      if (!shortcutOverlay) return;
      const shouldOpen = !!open;
      shortcutOverlay.hidden = !shouldOpen;
      shortcutOverlay.classList.toggle("open", shouldOpen);
      body.classList.toggle("shortcuts-open", shouldOpen);
      if (shouldOpen) {{
        shortcutClose && shortcutClose.focus();
      }} else if (shortcutsBtn) {{
        shortcutsBtn.focus();
      }}
    }}

    // Commands
    function z() {{ s.zen = !s.zen; syncBodyState(); if(s.zen) insp.classList.remove("visible"); else if(s.insp) insp.classList.add("visible"); }}
    function n() {{ if(s.zen) z(); s.navOff = !s.navOff; syncBodyState(); }}
    
    D.getElementById("btn-zen").onclick = z; D.getElementById("btn-exit-zen").onclick = z; D.getElementById("btn-nav").onclick = n;
    const hasViewModeButtons = viewModeButtons.some(([, button]) => !!button);
    if (hasViewModeButtons) {{
      viewModeButtons.forEach(([mode, button]) => {{
        if (!button) return;
        button.addEventListener("click", () => applyViewMode(mode));
      }});
    }} else {{
      const legacyModeBtn = D.getElementById("btn-split");
      if (legacyModeBtn) {{
        legacyModeBtn.onclick = cycleViewMode;
      }}
    }}
    D.getElementById("close-insp").onclick = () => {{ s.insp = false; insp.classList.remove("visible", "with-selection"); }};
    insp.addEventListener("click", (e) => {{
      const actionButton = e.target.closest("[data-review-inspector-action]");
      if (!actionButton || !insp.contains(actionButton)) return;
      const decision = actionButton.dataset.reviewInspectorAction;
      if (!decision || !s.sel || actionButton.disabled || s.decisionBusy) return;
      makeDecision(s.sel, decision);
    }});
    D.getElementById("btn-export").onclick = () => {{ window.open(`/api/runs/${{encodeURIComponent(runId)}}/export-clean`, "_blank"); }};
    if (shortcutsBtn) {{
      shortcutsBtn.onclick = () => setShortcutOverlay(true);
    }}
    if (shortcutClose) {{
      shortcutClose.onclick = () => setShortcutOverlay(false);
    }}
    if (shortcutOverlay) {{
      shortcutOverlay.onclick = (e) => {{
        if (e.target === shortcutOverlay) setShortcutOverlay(false);
      }};
    }}
    if (batchRunSelect) {{
      batchRunSelect.onchange = () => {{
        batchRunGo.disabled = batchRunSelect.value === runId;
      }};
    }}
    if (batchRunGo) {{
      batchRunGo.onclick = () => {{
        const targetRunId = batchRunSelect ? batchRunSelect.value : "";
        if (!targetRunId || targetRunId === runId) return;
        window.location.assign(`/runs/${{encodeURIComponent(targetRunId)}}`);
      }};
    }}
    bulkAcceptBtn.onclick = () => applyBulkDecision("accept");
    bulkRejectBtn.onclick = () => applyBulkDecision("reject");
    bulkClearBtn.onclick = () => applyBulkDecision("pending");
    bulkUndoBtn.onclick = () => undoLastDecisionChange();
    formatOnlyToggle.onclick = toggleFormatOnlyFilter;
      nextPendingBtn.onclick = () => nextPendingSection();
      nextFormatBtn.onclick = () => nextFormattingOnlySection();
      nextChangedBtn.onclick = () => nextChangedSection();
      nextUndecidedBtn.onclick = () => nextPendingSection();
      if (prevSectionBtn) {{
        prevSectionBtn.onclick = () => previousVisibleSection();
      }}
      if (nextSectionBtn) {{
        nextSectionBtn.onclick = () => nextVisibleSection();
      }}
      jumpBtn.onclick = jumpToSection;
      jumpInput.oninput = () => jumpInput.setCustomValidity("");
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
        el.classList.add("selection-glow");
        setTimeout(() => el.classList.remove("selection-glow"), 1400);
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
      applyReaderThemeRhythm();
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
      if(!s.meta) {{ s.insp = false; insp.classList.remove("visible", "with-selection"); return; }}
      const a = s.meta.sections.find(x => x.index === s.sel);
      if(!a) {{ s.insp = false; insp.classList.remove("visible", "with-selection"); return; }}
      s.insp = true; if(!s.zen) insp.classList.add("visible");
      insp.classList.add("with-selection");
      D.getElementById("insp-title").textContent = a.kind_label || a.kind;
      const decision = a.is_changed ? decisionForSection(a) : "pending";
      const decisionLabel = decision.charAt(0).toUpperCase() + decision.slice(1);
      const saveState = decisionSaveStateForIndex(a.index);
      const saveLabel = saveState ? ` · ${{DECISION_STATE_LABELS[saveState] || saveState}}` : "";
      D.getElementById("insp-subtitle").textContent = `${{decisionLabel}}${{saveLabel}}`;
      const formatFacets = sectionFormatFacets(a).filter(facet => facet !== "formatting");
      const facetBadges = sectionBadgeMarkup(a);
      const canDecide = !!a.is_changed;
      const decisionActions = canDecide
        ? `
          <div class="insp-action-wrap">
            <div class="insp-action-label">Section decisions</div>
            <div class="insp-actions">
              <button class="insp-action-btn" type="button" data-review-inspector-action="accept">Accept</button>
              <button class="insp-action-btn" type="button" data-review-inspector-action="reject">Reject</button>
              <button class="insp-action-btn" type="button" data-review-inspector-action="pending">Clear</button>
            </div>
          </div>
        `
        : `
          <div class="insp-action-wrap">
            <div class="insp-no-action">No decision action is available for this unchanged section.</div>
          </div>
        `;
      const formattingBlock = formatFacets.length
        ? `
          <div class="diff-block diff-block-meta">
            <div class="diff-hdr">Formatting Deltas</div>
            <div class="diff-content">${{enc(formatFacets.map(facet => FACET_LABELS[facet] || facet).join(", "))}}</div>
          </div>
          <div class="insp-divider"></div>
        `
        : "";
      D.getElementById("insp-body").innerHTML = `
        <div class="insp-label-row">
          <div class="insp-label">${{enc(a.label)}}</div>
          <div class="insp-label-subtext">Section ${{a.index}}</div>
        </div>
        <div class="insp-facets-wrap">
          <div class="insp-facets">${{facetBadges || '<span class="facet-badge">No Facets</span>'}}</div>
          <div class="insp-divider"></div>
        </div>
        ${{formattingBlock}}
        <div class="diff-block diff-block-original"><div class="diff-hdr">Original</div><div class="diff-content">${{enc(a.original_text||"—")}}</div></div>
        <div class="insp-divider"></div>
        <div class="diff-block diff-block-revised"><div class="diff-hdr">Revised</div><div class="diff-content">${{enc(a.revised_text||"—")}}</div></div>
        ${{decisionActions}}
      `;
      insp.querySelectorAll('[data-review-inspector-action]').forEach((button) => {{
        const action = button.dataset.reviewInspectorAction;
        button.classList.toggle("is-current", action === decision);
        button.disabled = !canDecide || s.decisionBusy;
      }});
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
          const kind = sec.kind || "insert";
          const isActive = sec.index === s.sel;
          const activeClass = isActive ? " active" : "";
          html += `<div class="minimap-tick ${{kind}}${{activeClass}}" style="top:${{topPc}}%" onclick="setSel(${{sec.index}}); syncFrame(${{sec.index}})"></div>`;
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
          x.kind ? `kind-${{x.kind}}` : "",
        ].filter(Boolean).join(" ");
        const decisionMeta = x.is_changed
          ? `<span class="status-chips"><span class="decision-tag ${{decision}}">${{decision}}</span>${{decisionStatusBadgeMarkup(x.index)}}</span>`
          : '<span class="status-chip">Unchanged</span>';
        return `
        <div class="${{cardClasses}}" data-index="${{x.index}}" style="animation-delay: ${{Math.min(i*0.03, 0.4)}}s">
          <div class="detail-title">
            <span class="detail-index" aria-hidden="true">${{x.index}}</span>
            <span class="detail-title-text">${{enc(x.label||"Section "+x.index)}}</span>
          </div>
          <div class="detail-meta">
            <div class="detail-meta-left"><span class="detail-kind">${{enc(x.kind_label || x.kind || "Section")}}</span><span class="detail-dot">•</span><span class="detail-sec">sec ${{x.index}}</span></div>
            <div class="detail-meta-right detail-meta-state">
              ${{decisionMeta}}
            </div>
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
      decisionSummary.textContent = `Decision ${{decided}}/${{decisionCounts.any}} decided · ${{decisionCounts.pending}} pending`;
      decisionSummary.classList.toggle("is-pending", decisionCounts.pending > 0);
      decisionSummary.classList.toggle("is-complete", !!decisionCounts.any && decisionCounts.pending === 0);
      const pct = decisionCounts.any ? Math.round((decided / decisionCounts.any) * 100) : 0;
      if (runDecisionPill) {{
        runDecisionPill.textContent = `${{pct}}% decided`;
      }}
      if (runProgressFill) {{
        runProgressFill.style.width = `${{pct}}%`;
      }}
      if (runProgressLabel) {{
        if (!decisionCounts.any) {{
          runProgressLabel.textContent = "No changed sections require decisions";
        }} else if (!decisionCounts.pending) {{
          runProgressLabel.textContent = `${{decided}}/${{decisionCounts.any}} decided · ready to export`;
        }} else {{
          runProgressLabel.textContent = `${{decided}}/${{decisionCounts.any}} decided · ${{decisionCounts.pending}} pending`;
        }}
      }}
    }}

    function renderPendingSummary(decisionCounts) {{
      const pending = pendingVisibleSections();
      const next = nextItemFromList(pending);
      if (runPendingPill) {{
        runPendingPill.textContent = `${{decisionCounts.pending}} open`;
      }}
      if (runPendingMeta) {{
        if (!decisionCounts.any) {{
          runPendingMeta.textContent = "No changed sections require review";
        }} else if (!decisionCounts.pending) {{
          runPendingMeta.textContent = "All changed sections are decided";
        }} else if (pending.length && next) {{
          runPendingMeta.textContent = `${{pending.length}} visible · next sec ${{next.index}}`;
        }} else {{
          runPendingMeta.textContent = `${{decisionCounts.pending}} across run · none visible`;
        }}
      }}
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
        decisionSummary.textContent = "Decision 0/0 decided · 0 pending";
        decisionSummary.classList.remove("is-pending", "is-complete");
        if (navProgress) navProgress.textContent = "Visible 0/0 · 0 changed";
        if (runVisiblePill) runVisiblePill.textContent = "0/0";
        if (runVisibleMeta) runVisibleMeta.textContent = "No sections in current scope";
        if (runPendingPill) runPendingPill.textContent = "0 open";
        if (runPendingMeta) runPendingMeta.textContent = "Pending guidance unavailable.";
        if (runDecisionPill) runDecisionPill.textContent = "0% decided";
        if (runProgressFill) runProgressFill.style.width = "0%";
        if (runProgressLabel) runProgressLabel.textContent = "No changed sections require decisions";
        decisionRow.innerHTML = "";
        if (nextUndecidedNote) nextUndecidedNote.textContent = "Pending guidance unavailable.";
        if (nextUndecidedBtn) nextUndecidedBtn.disabled = true;
        updateUndoUi();
        return;
      }}
      const decisionCounts = buildDecisionCounts(s.meta.sections || []);
      renderDecisionSummary(decisionCounts);
      renderPendingSummary(decisionCounts);
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
          jumpInput.focus();
          jumpInput.select();
          jumpInput.setCustomValidity("Enter a valid section number.");
          jumpInput.reportValidity();
          return false;
        }}
        const hasSection = s.meta.sections.some(sec => sec.index === idx);
        if (!hasSection) {{
          jumpInput.focus();
          jumpInput.select();
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
      if (runProfilePill) {{
        const rawProfileName = String(m.profile_name || "default").replace(/[_-]+/g, " ").trim();
        const profileName = rawProfileName.replace(/\b\w/g, (ch) => ch.toUpperCase()) || "Default";
        runProfilePill.textContent = profileName;
      }}
      if (runSectionsPill) {{
        const allSections = Array.isArray(m.sections) ? m.sections : [];
        const sectionCount = allSections.length;
        const changedCount = allSections.filter(section => section.is_changed).length;
        runSectionsPill.textContent = `${{sectionCount}} total`;
        if (runSectionsMeta) {{
          runSectionsMeta.textContent = `${{changedCount}} changed ${{changedCount === 1 ? "section" : "sections"}}`;
        }}
      }}
      s.meta.decisions = s.meta.decisions || {{}};
      setBatchSwitcher(m);
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
      if (shortcutOverlay && shortcutOverlay.classList.contains("open")) {{
        if (e.key === "Escape" || e.key === "?" || (e.key === "/" && e.shiftKey)) {{
          e.preventDefault();
          setShortcutOverlay(false);
        }}
        return;
      }}
      if(e.target.tagName==="INPUT") {{
        if(e.key==="Escape") {{
          e.target.blur();
          if (e.target === jumpInput) jumpInput.setCustomValidity("");
        }}
        return;
      }}
      if (e.key === "?" || (e.key === "/" && e.shiftKey)) {{
        e.preventDefault();
        setShortcutOverlay(true);
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
