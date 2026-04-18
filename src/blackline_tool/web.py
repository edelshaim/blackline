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
      --primary-soft: rgba(30, 58, 138, 0.08);
      --ok: #1f7a4f;
      --warn: #b45309;
      --bad: #b91c1c;
      --shadow-lg: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
      --font-sans: 'Inter', system-ui, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: var(--font-sans);
      color: var(--ink);
      background: radial-gradient(circle at 10% 10%, rgba(30,58,138,0.06) 0%, transparent 40%), var(--canvas);
      display: flex;
      justify-content: center;
      padding: 4rem 1.5rem;
      position: relative;
      overflow-x: hidden;
    }}
    body.is-processing .live-metric {{
      border-color: rgba(30, 58, 138, 0.3);
      box-shadow: 0 18px 26px -22px rgba(30, 58, 138, 0.5);
    }}
    body::before,
    body::after {{
      content: "";
      position: fixed;
      width: 48vw;
      height: 48vw;
      max-width: 560px;
      max-height: 560px;
      border-radius: 999px;
      filter: blur(40px);
      z-index: -1;
      pointer-events: none;
      opacity: 0.36;
      animation: auroraDrift 16s ease-in-out infinite alternate;
    }}
    body::before {{
      top: -20vw;
      left: -10vw;
      background: radial-gradient(circle, rgba(59, 130, 246, 0.34) 0%, rgba(59, 130, 246, 0.02) 66%);
    }}
    body::after {{
      right: -14vw;
      bottom: -22vw;
      background: radial-gradient(circle, rgba(15, 118, 110, 0.28) 0%, rgba(15, 118, 110, 0.02) 66%);
      animation-delay: -5.5s;
    }}
    @keyframes auroraDrift {{
      0% {{ transform: translate3d(0, 0, 0) scale(1); opacity: 0.28; }}
      50% {{ transform: translate3d(1.6vw, -1.2vw, 0) scale(1.05); opacity: 0.42; }}
      100% {{ transform: translate3d(-1.4vw, 1.8vw, 0) scale(0.98); opacity: 0.3; }}
    }}
    .shell {{ width: 100%; max-width: 980px; display: flex; flex-direction: column; gap: 1.35rem; }}
    h1 {{ font-size: 2.5rem; font-weight: 700; margin: 0; text-align: center; letter-spacing: -0.02em; }}
    p.subtitle {{ color: var(--muted); text-align: center; font-size: 1.02rem; margin-top: 0.45rem; }}
    .hero-strip {{
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 0.46rem;
      margin-top: -0.22rem;
    }}
    .hero-chip {{
      display: inline-flex;
      align-items: center;
      gap: 0.34rem;
      padding: 0.3rem 0.6rem;
      border-radius: 999px;
      border: 1px solid rgba(30, 58, 138, 0.16);
      background: rgba(255, 255, 255, 0.74);
      color: #30425d;
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.02em;
      box-shadow: 0 8px 18px -18px rgba(15, 23, 42, 0.56);
    }}
    .hero-chip::before {{
      content: "";
      width: 0.38rem;
      height: 0.38rem;
      border-radius: 999px;
      background: linear-gradient(150deg, #1d4ed8 0%, #2563eb 100%);
      flex: none;
    }}
    .workflow-rail {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 0.62rem;
    }}
    .workflow-step {{
      display: flex;
      align-items: center;
      gap: 0.55rem;
      padding: 0.66rem 0.74rem;
      border-radius: 12px;
      border: 1px solid rgba(30, 58, 138, 0.12);
      background: rgba(255, 255, 255, 0.72);
      box-shadow: 0 10px 20px -20px rgba(15, 23, 42, 0.45);
    }}
    .workflow-step strong {{
      width: 1.28rem;
      height: 1.28rem;
      border-radius: 999px;
      background: #1e3a8a;
      color: #fff;
      font-size: 0.72rem;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: none;
    }}
    .workflow-step span {{
      font-size: 0.75rem;
      color: #334155;
      font-weight: 600;
      letter-spacing: 0.02em;
    }}
    .live-deck {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 0.6rem;
    }}
    .live-metric {{
      position: relative;
      border-radius: 14px;
      border: 1px solid rgba(30, 58, 138, 0.18);
      background: linear-gradient(165deg, rgba(255, 255, 255, 0.86) 0%, rgba(247, 251, 255, 0.78) 100%);
      padding: 0.7rem 0.72rem;
      overflow: hidden;
      box-shadow: 0 16px 22px -24px rgba(15, 23, 42, 0.68);
    }}
    .live-metric::after {{
      content: "";
      position: absolute;
      inset: 0;
      border-radius: inherit;
      pointer-events: none;
      background: linear-gradient(110deg, transparent 0%, rgba(255,255,255,0.46) 42%, transparent 74%);
      transform: translateX(-120%);
      animation: metricSweep 7.8s ease-in-out infinite;
      opacity: 0.56;
    }}
    .live-metric:nth-child(2)::after {{ animation-delay: -1.8s; }}
    .live-metric:nth-child(3)::after {{ animation-delay: -3.5s; }}
    .live-metric:nth-child(4)::after {{ animation-delay: -5.2s; }}
    @keyframes metricSweep {{
      0%, 68%, 100% {{ transform: translateX(-120%); }}
      82% {{ transform: translateX(130%); }}
    }}
    .metric-label {{
      display: block;
      font-size: 0.64rem;
      color: #64748b;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
      margin-bottom: 0.32rem;
    }}
    .metric-value {{
      display: block;
      font-size: 1rem;
      color: #0f172a;
      font-weight: 700;
      line-height: 1.2;
      letter-spacing: -0.01em;
    }}
    .metric-value.pulse {{
      animation: metricPulse 360ms ease;
    }}
    @keyframes metricPulse {{
      0% {{ transform: scale(0.98); opacity: 0.7; }}
      75% {{ transform: scale(1.03); opacity: 1; }}
      100% {{ transform: scale(1); opacity: 1; }}
    }}
    .metric-meta {{
      display: block;
      margin-top: 0.2rem;
      font-size: 0.7rem;
      color: #4b5563;
      min-height: 1.15rem;
    }}
    .metric-track {{
      margin-top: 0.4rem;
      height: 6px;
      border-radius: 999px;
      background: rgba(148, 163, 184, 0.24);
      overflow: hidden;
    }}
    .metric-fill {{
      display: block;
      height: 100%;
      width: 0%;
      border-radius: inherit;
      background: linear-gradient(90deg, #1e40af 0%, #2563eb 50%, #0ea5e9 100%);
      transition: width 220ms ease;
    }}
    .card {{
      background: rgba(255, 255, 255, 0.85);
      backdrop-filter: blur(24px);
      -webkit-backdrop-filter: blur(24px);
      border: 1px solid var(--border-soft);
      border-radius: 20px;
      padding: 2.5rem;
      box-shadow: var(--shadow-lg);
    }}
    .form-step {{
      border: 1px solid rgba(148, 163, 184, 0.24);
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.82) 0%, rgba(248, 251, 255, 0.74) 100%);
      border-radius: 16px;
      padding: 1rem 1rem 0.24rem;
      box-shadow: 0 14px 24px -24px rgba(15, 23, 42, 0.45);
      margin-bottom: 1rem;
    }}
    .form-step:last-of-type {{ margin-bottom: 0; }}
    .section-title {{
      font-size: 0.875rem;
      font-weight: 600;
      color: var(--muted);
      text-transform: uppercase;
      margin-bottom: 1rem;
      letter-spacing: 0.05em;
    }}
    .mode-row {{
      display: flex;
      gap: 0.6rem;
      flex-wrap: wrap;
      margin-bottom: 1rem;
    }}
    .mode-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.55rem 0.95rem;
      border: 1px solid var(--border-soft);
      border-radius: 999px;
      font-size: 0.875rem;
      font-weight: 500;
      background: #fff;
      cursor: pointer;
      transition: 0.2s;
    }}
    .mode-pill:hover {{ background: #f9fafb; border-color: rgba(30, 58, 138, 0.28); }}
    .mode-pill:has(input:checked) {{
      background: linear-gradient(180deg, rgba(30,58,138,0.12) 0%, rgba(30,58,138,0.06) 100%);
      border-color: rgba(30, 58, 138, 0.38);
      color: #1e3a8a;
    }}
    .mode-pill input {{ accent-color: var(--primary); }}
    .mode-summary {{
      margin: -0.2rem 0 0.85rem;
      font-size: 0.8rem;
      color: #475569;
      min-height: 1.1rem;
    }}
    .upload-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; margin-bottom: 1.8rem; }}
    .upload-zone {{
      position: relative;
      border: 2px dashed var(--border-soft);
      border-radius: 16px;
      padding: 1.8rem 1.2rem;
      text-align: center;
      cursor: pointer;
      transition: all 0.2s;
      background: rgba(249, 250, 251, 0.5);
      min-height: 180px;
    }}
    .upload-zone:hover,
    .upload-zone.dragover {{ border-color: var(--primary); background: var(--primary-soft); }}
    .upload-zone.has-file {{ border-style: solid; border-color: var(--primary); background: var(--surface); }}
    .upload-input {{ position: absolute; inset: 0; opacity: 0; cursor: pointer; }}
    .icon {{
      width: 44px;
      height: 44px;
      border-radius: 22px;
      background: var(--canvas);
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 0 auto 1rem;
      color: var(--muted);
      font-weight: 600;
    }}
    .upload-zone.has-file .icon {{ background: var(--primary); color: #fff; }}
    .lbl {{ font-weight: 600; font-size: 1.06rem; }}
    .sub {{ color: var(--muted); font-size: 0.82rem; margin-top: 0.25rem; }}
    .fname {{
      color: var(--primary);
      font-weight: 500;
      font-size: 0.85rem;
      margin-top: 0.5rem;
      display: none;
      line-height: 1.35;
    }}
    .upload-zone.has-file .fname {{ display: block; }}
    .file-list {{
      margin: 0.65rem 0 0;
      padding: 0;
      list-style: none;
      display: none;
      text-align: left;
      max-height: 112px;
      overflow: auto;
      border: 1px solid rgba(30, 58, 138, 0.14);
      border-radius: 10px;
      background: rgba(30, 58, 138, 0.04);
    }}
    .file-list.show {{ display: block; }}
    .file-list li {{
      font-size: 0.76rem;
      color: #1f2937;
      padding: 0.4rem 0.55rem;
      border-bottom: 1px solid rgba(30, 58, 138, 0.08);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .file-list li:last-child {{ border-bottom: 0; }}
    .field-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.2rem; margin-bottom: 1.6rem; }}
    .field label {{ display: block; font-size: 0.875rem; font-weight: 500; margin-bottom: 0.5rem; }}
    input[type="text"],
    select {{
      width: 100%;
      border-radius: 12px;
      border: 1px solid var(--border-soft);
      padding: 0.85rem 0.95rem;
      font-family: inherit;
      font-size: 0.98rem;
      transition: border-color 0.2s;
      background: #fff;
    }}
    input[type="text"]:focus,
    select:focus {{ outline: none; border-color: var(--primary); }}
    .pill-group {{ display: flex; flex-wrap: wrap; gap: 0.7rem; margin-bottom: 1.35rem; }}
    .check-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.46rem 0.85rem;
      border-radius: 999px;
      border: 1px solid var(--border-soft);
      font-size: 0.83rem;
      cursor: pointer;
      background: var(--surface);
      transition: 0.2s;
    }}
    .check-pill:hover {{ background: #f9fafb; }}
    .check-pill input {{ accent-color: var(--primary); }}
    details {{ margin-bottom: 1.5rem; }}
    summary {{
      color: var(--primary);
      font-weight: 500;
      font-size: 0.875rem;
      cursor: pointer;
      list-style: none;
      user-select: none;
    }}
    summary::-webkit-details-marker {{ display: none; }}
    .btn {{
      width: 100%;
      background: var(--primary);
      color: #fff;
      border: none;
      border-radius: 12px;
      padding: 1.16rem;
      font-size: 1.07rem;
      font-weight: 600;
      cursor: pointer;
      transition: 0.2s;
      box-shadow: 0 4px 6px rgba(30,58,138,0.2);
    }}
    .btn:hover {{
      background: var(--primary-hover);
      transform: translateY(-1px);
      box-shadow: 0 8px 12px rgba(30,58,138,0.22);
    }}
    .btn:disabled {{ opacity: 0.72; cursor: wait; transform: none; }}
    #status {{
      margin-top: 0.82rem;
      min-height: 1.2rem;
      font-size: 0.82rem;
      display: none;
      align-items: center;
      gap: 0.46rem;
      border-radius: 10px;
      border: 1px solid rgba(100, 116, 139, 0.2);
      background: rgba(248, 250, 252, 0.94);
      color: #334155;
      padding: 0.46rem 0.62rem;
    }}
    #status.show {{ display: inline-flex; }}
    #status::before {{
      content: "";
      width: 0.54rem;
      height: 0.54rem;
      border-radius: 999px;
      background: #64748b;
      flex: none;
    }}
    #status.tone-working::before {{
      border: 2px solid rgba(30, 58, 138, 0.28);
      border-top-color: #1e3a8a;
      background: transparent;
      animation: spin 640ms linear infinite;
    }}
    #status.tone-warning {{ border-color: rgba(180, 83, 9, 0.3); background: rgba(255, 247, 237, 0.86); color: #9a3412; }}
    #status.tone-warning::before {{ background: #d97706; }}
    #status.tone-error {{ border-color: rgba(185, 28, 28, 0.3); background: rgba(254, 242, 242, 0.88); color: #991b1b; }}
    #status.tone-error::before {{ background: #dc2626; }}
    #status.tone-success {{ border-color: rgba(31, 122, 79, 0.3); background: rgba(240, 253, 244, 0.88); color: #166534; }}
    #status.tone-success::before {{ background: #16a34a; }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .batch-panel {{
      margin-top: 1.1rem;
      border: 1px solid rgba(30, 58, 138, 0.14);
      border-radius: 14px;
      padding: 0.85rem 0.9rem;
      background: linear-gradient(180deg, rgba(248,250,255,0.88) 0%, rgba(255,255,255,0.92) 100%);
      display: none;
    }}
    .batch-panel.show {{ display: block; }}
    .batch-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.6rem;
      margin-bottom: 0.65rem;
    }}
    .batch-label {{
      font-size: 0.78rem;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: #334155;
      font-weight: 700;
    }}
    .batch-summary {{
      font-size: 0.8rem;
      color: #475569;
      font-weight: 500;
    }}
    .batch-progress {{
      height: 6px;
      border-radius: 999px;
      background: rgba(30, 58, 138, 0.12);
      overflow: hidden;
      margin: 0.35rem 0 0.5rem;
    }}
    .batch-progress-fill {{
      width: 0%;
      height: 100%;
      background: linear-gradient(90deg, #1e3a8a 0%, #2563eb 100%);
      transition: width 180ms ease-out;
    }}
    .batch-progress-label {{
      font-size: 0.78rem;
      color: #475569;
      margin-bottom: 0.5rem;
    }}
    .batch-results {{
      margin: 0;
      padding: 0;
      list-style: none;
      border: 1px solid rgba(30, 58, 138, 0.12);
      border-radius: 10px;
      max-height: 240px;
      overflow: auto;
      background: #fff;
    }}
    .batch-row {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto auto;
      gap: 0.45rem 0.6rem;
      align-items: center;
      padding: 0.52rem 0.62rem;
      border-bottom: 1px solid rgba(30, 58, 138, 0.08);
      transition: background 120ms ease;
    }}
    .batch-row:hover {{ background: rgba(30, 58, 138, 0.05); }}
    .batch-row:last-child {{ border-bottom: 0; }}
    .batch-index {{
      width: 1.4rem;
      height: 1.4rem;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.72rem;
      font-weight: 700;
      color: #334155;
      background: #e2e8f0;
    }}
    .batch-name {{
      font-size: 0.8rem;
      color: #1f2937;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .batch-state {{
      font-size: 0.74rem;
      font-weight: 700;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }}
    .batch-row.pending .batch-state {{ color: #64748b; }}
    .batch-row.running .batch-state {{ color: var(--warn); }}
    .batch-row.done .batch-state {{ color: var(--ok); }}
    .batch-row.failed .batch-state {{ color: var(--bad); }}
    .batch-row.done .batch-index {{ background: rgba(22, 163, 74, 0.15); color: #166534; }}
    .batch-row.failed .batch-index {{ background: rgba(220, 38, 38, 0.15); color: #991b1b; }}
    .batch-row.running .batch-index {{ background: rgba(180, 83, 9, 0.16); color: #92400e; }}
    .batch-empty {{
      padding: 0.82rem 0.75rem;
      text-align: center;
      color: #64748b;
      font-size: 0.78rem;
      font-weight: 500;
    }}
    .batch-link a {{
      font-size: 0.75rem;
      color: var(--primary);
      text-decoration: none;
      font-weight: 600;
    }}
    .batch-link a:hover {{ text-decoration: underline; }}
    .batch-actions {{ margin-top: 0.6rem; display: flex; justify-content: flex-end; }}
    .batch-btn {{
      border: 1px solid rgba(30, 58, 138, 0.22);
      border-radius: 999px;
      padding: 0.38rem 0.78rem;
      font-size: 0.75rem;
      font-weight: 600;
      color: var(--primary);
      background: #fff;
      cursor: pointer;
    }}
    .batch-btn[hidden] {{ display: none; }}
    .batch-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .batch-open-row {{
      margin-top: 0.6rem;
      display: none;
      align-items: center;
      gap: 0.45rem;
      flex-wrap: wrap;
    }}
    .batch-open-row.show {{ display: flex; }}
    .batch-open-label {{
      font-size: 0.72rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: #475569;
      font-weight: 700;
    }}
    .batch-open-select {{
      flex: 1 1 250px;
      border-radius: 999px;
      border: 1px solid rgba(30, 58, 138, 0.22);
      background: #fff;
      color: #1f2937;
      min-height: 2rem;
      padding: 0.35rem 0.65rem;
      font-family: inherit;
      font-size: 0.76rem;
    }}
    .batch-open-select:focus {{ outline: none; border-color: var(--primary); }}
    @media (max-width: 860px) {{
      body {{ padding: 2rem 0.8rem; }}
      .card {{ padding: 1.35rem; }}
      .hero-strip {{ justify-content: flex-start; }}
      .workflow-rail {{ grid-template-columns: 1fr; }}
      .live-deck {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .upload-grid {{ grid-template-columns: 1fr; }}
      .field-grid {{ grid-template-columns: 1fr; }}
      .batch-row {{ grid-template-columns: auto minmax(0, 1fr); }}
      .batch-state, .batch-link {{ justify-self: end; }}
    }}
    @media (max-width: 560px) {{
      .live-deck {{ grid-template-columns: 1fr; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *,
      *::before,
      *::after {{
        animation-duration: 1ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 1ms !important;
      }}
    }}
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
      --canvas-0: #e9eef6;
      --canvas-1: #f7f9fd;
      --canvas-zen: #0d1828;
      --surface: rgba(255, 255, 255, 0.87);
      --surface-solid: #ffffff;
      --surface-deep: rgba(255, 255, 255, 0.94);
      --surface-elevated: rgba(255, 255, 255, 0.92);
      --surface-glass: rgba(247, 252, 255, 0.66);
      --border-soft: rgba(128, 146, 173, 0.24);
      --border-strong: rgba(74, 95, 129, 0.34);
      --primary: #1e4a87;
      --primary-hover: #183f73;
      --accent: #0f5f87;
      --focus-ring: rgba(22, 81, 150, 0.24);
      --accept-soft: rgba(24, 123, 101, 0.12);
      --reject-soft: rgba(188, 65, 54, 0.1);
      --shadow-softest: 0 10px 24px -16px rgba(13, 24, 40, 0.16);
      --shadow-soft: 0 14px 32px -24px rgba(16, 32, 58, 0.30);
      --shadow-float: 0 30px 52px -34px rgba(13, 24, 40, 0.60), 0 18px 30px -24px rgba(16, 32, 58, 0.46);
      --shadow-elev: 0 26px 50px -36px rgba(13, 24, 40, 0.58), 0 10px 28px -24px rgba(13, 24, 40, 0.34);
      --radius-xxl: 28px;
      --radius-xl: 22px;
      --radius-lg: 18px;
      --radius-md: 14px;
      --radius-sm: 10px;
      --font-ui: 'IBM Plex Sans', 'Avenir Next', 'Segoe UI', sans-serif;
      --font-display: 'Source Serif 4', 'Georgia', serif;
      --ins: #137f6f;
      --del: #c64b40;
      --rep: #b87b16;
      --mov: #1f5ea3;
      --timing: 230ms cubic-bezier(0.2, 0.74, 0.24, 1);
      --timing-soft: 200ms cubic-bezier(0.2, 0.7, 0.35, 1);
      --timing-glow: 0.35s ease;
      --gloss: linear-gradient(140deg, rgba(255, 255, 255, 0.94) 0%, rgba(235, 244, 255, 0.76) 100%);
      --gloss-zen: linear-gradient(140deg, rgba(25, 48, 75, 0.72) 0%, rgba(17, 36, 63, 0.74) 100%);
      --review-page-bg: radial-gradient(1120px 720px at -9% 120%, rgba(68, 108, 170, 0.2) 0%, transparent 54%), radial-gradient(900px 620px at 108% -16%, rgba(15, 95, 135, 0.18) 0%, transparent 52%), radial-gradient(720px 360px at 84% 8%, rgba(80, 119, 177, 0.12) 0%, transparent 64%), linear-gradient(170deg, #e9eef6 0%, #f7f9fd 45%, #f3f6fb 100%);
      --review-page-bg-zen: radial-gradient(940px 560px at -8% 120%, rgba(29, 92, 132, 0.26) 0%, transparent 58%), radial-gradient(760px 500px at 108% -15%, rgba(34, 80, 128, 0.3) 0%, transparent 52%), radial-gradient(460px 300px at 50% 80%, rgba(34, 74, 124, 0.18) 0%, transparent 72%), linear-gradient(168deg, #0d1828 0%, #101f33 50%, #14253c 100%);
      --review-grid-line: rgba(255, 255, 255, 0.30);
      --review-grid-line-soft: rgba(255, 255, 255, 0.22);
      --review-grid-size: 48px 48px;
      --review-grid-offset: 24px 24px;
      --review-page-overlay: 0.34;
      --review-page-overlay-zen: 0.18;
      --review-shell-border: rgba(120, 141, 171, 0.24);
      --review-shell-border-zen: rgba(137, 164, 205, 0.26);
      --review-shell-bg: var(--gloss), linear-gradient(180deg, rgba(252, 252, 255, 0.96) 0%, rgba(237, 245, 252, 0.72) 100%), radial-gradient(600px 300px at 10% -6%, rgba(73, 116, 180, 0.12) 0%, transparent 60%);
      --review-shell-bg-zen: var(--gloss-zen), linear-gradient(180deg, rgba(14, 27, 44, 0.74) 0%, rgba(16, 31, 52, 0.7) 100%), radial-gradient(620px 360px at 16% -5%, rgba(42, 97, 162, 0.24) 0%, transparent 58%);
      --review-shell-shadow: var(--shadow-elev);
      --review-shell-shadow-zen: 0 30px 50px -34px rgba(3, 9, 20, 0.82), 0 20px 34px -28px rgba(3, 9, 20, 0.68);
      --review-shell-edge: 1px solid rgba(255, 255, 255, 0.48);
      --review-shell-edge-zen: 1px solid rgba(201, 221, 255, 0.16);
      --review-shell-edge-opacity: 0.52;
      --review-shell-edge-opacity-zen: 0.42;
      --review-shell-edge-blend: screen;
      --review-shell-edge-blend-zen: normal;
      --review-chrome-border: rgba(130, 148, 174, 0.32);
      --review-chrome-border-zen: rgba(133, 159, 198, 0.24);
      --review-chrome-bg: linear-gradient(178deg, rgba(255, 255, 255, 0.8) 0%, rgba(244, 249, 255, 0.62) 100%);
      --review-chrome-bg-zen: linear-gradient(178deg, rgba(20, 40, 66, 0.74) 0%, rgba(17, 33, 55, 0.62) 100%);
      --review-chrome-shadow: inset 0 -1px 0 rgba(255, 255, 255, 0.52);
      --review-title: #627592;
      --review-title-zen: #a7bcdd;
      --review-dot: linear-gradient(140deg, #2e6fbf 0%, #5da0e5 100%);
      --review-dot-ring: rgba(77, 133, 203, 0.2);
      --review-mode-border: rgba(108, 128, 158, 0.34);
      --review-mode-border-zen: rgba(136, 163, 201, 0.32);
      --review-mode-bg: rgba(255, 255, 255, 0.8);
      --review-mode-bg-zen: rgba(22, 45, 73, 0.68);
      --review-mode-text: #52647f;
      --review-mode-text-zen: #a8bfdd;
      --review-mode-strong: #1d447d;
      --review-mode-strong-zen: #d4e4fb;
      --review-body-bg: linear-gradient(180deg, rgba(255, 255, 255, 0.98) 0%, rgba(250, 252, 255, 0.94) 100%);
      --review-body-bg-zen: linear-gradient(180deg, rgba(18, 35, 57, 0.88) 0%, rgba(16, 30, 48, 0.86) 100%);
      --review-body-overlay: inset 0 0 0 1px rgba(130, 149, 176, 0.18), inset 0 24px 40px -34px rgba(12, 27, 48, 0.36);
      --review-body-overlay-zen: inset 0 0 0 1px rgba(124, 152, 195, 0.18), inset 0 24px 40px -34px rgba(5, 12, 24, 0.7);
      --review-header-bg: radial-gradient(420px 190px at 7% 8%, rgba(255, 255, 255, 0.72) 0%, rgba(255, 255, 255, 0.4) 45%, transparent 100%), linear-gradient(160deg, rgba(255, 255, 255, 0.9) 0%, rgba(246, 250, 255, 0.84) 100%);
      --review-header-shadow: 0 14px 24px -18px rgba(16, 32, 58, 0.42);
      --review-header-brand: #405c80;
      --review-header-pill-border: rgba(126, 145, 172, 0.3);
      --review-header-pill-bg: linear-gradient(165deg, rgba(255, 255, 255, 0.96) 0%, rgba(246, 251, 255, 0.8) 100%);
      --review-header-pill-ink: #8a98ae;
      --review-context-pill-border: rgba(107, 128, 159, 0.27);
      --review-context-pill-bg: linear-gradient(180deg, rgba(255, 255, 255, 0.9) 0%, rgba(249, 252, 255, 0.78) 100%);
      --review-context-pill-text: #53657f;
      --review-progress-bg: rgba(148, 163, 184, 0.32);
      --review-progress-fill: linear-gradient(90deg, #1d4ed8 0%, #0ea5e9 60%, #10b981 100%);
      --review-batch-border: rgba(92, 114, 146, 0.34);
      --review-batch-bg: linear-gradient(180deg, rgba(255, 255, 255, 0.95) 0%, rgba(245, 250, 255, 0.88) 100%);
      --review-batch-label: #6f8098;
      --review-batch-select-border: rgba(126, 145, 172, 0.32);
      --review-batch-go-bg: linear-gradient(140deg, #1e4a87 0%, #255997 100%);
      --review-batch-go-bg-hover: linear-gradient(140deg, #194177 0%, #214f86 100%);
      --review-batch-go-text: #eef5ff;
      --review-pill-bg: var(--surface-deep);
      --review-pill-bg-hover: #fff;
      --review-pill-border-hover: var(--border-strong);
      --review-pill-text: var(--ink-soft);
      --review-primary-bg: linear-gradient(138deg, var(--primary) 0%, #285da0 100%);
      --review-primary-bg-hover: linear-gradient(138deg, var(--primary-hover) 0%, #204f8c 100%);
      --review-primary-text: #f7fbff;
      --review-primary-text-hover: var(--ink);
      --review-export-bg: linear-gradient(140deg, #12786a 0%, #1c907f 100%);
      --review-export-bg-hover: linear-gradient(140deg, #0f695d 0%, #1a7b6d 100%);
      --review-export-shadow: 0 10px 18px -12px rgba(18, 120, 106, 0.76);
      --review-export-shadow-hover: 0 14px 22px -14px rgba(18, 120, 106, 0.68);
      --review-card-border: rgba(146, 166, 194, 0.18);
      --review-card-border-hover: rgba(96, 123, 163, 0.3);
      --review-card-bg: linear-gradient(160deg, rgba(255, 255, 255, 0.78) 0%, rgba(246, 250, 255, 0.74) 100%);
      --review-card-bg-hover: linear-gradient(160deg, rgba(255, 255, 255, 0.94) 0%, rgba(249, 252, 255, 0.9) 100%);
      --review-card-shadow: 0 10px 16px -16px rgba(16, 32, 58, 0.54);
      --review-card-shadow-hover: 0 12px 22px -16px rgba(13, 37, 68, 0.4);
      --review-card-active-border: rgba(33, 74, 131, 0.34);
      --review-card-active-bg: linear-gradient(148deg, #ffffff 0%, #f4f8ff 100%);
      --review-card-active-shadow: 0 14px 26px -20px rgba(21, 56, 104, 0.76), 0 0 0 1px rgba(32, 75, 134, 0.3);
      --review-card-active-changed-border: #2a5e9f;
      --review-card-active-changed-shadow: 0 15px 28px -20px rgba(24, 60, 109, 0.8), 0 0 0 1px rgba(35, 83, 148, 0.32);
      --review-card-changed-border: rgba(95, 115, 141, 0.5);
      --review-card-pending-border: #8ea3c2;
      --review-card-pending-bg: linear-gradient(150deg, rgba(231, 238, 249, 0.58) 0%, rgba(255, 255, 255, 0.96) 62%);
      --review-card-accept-border: var(--ins);
      --review-card-accept-bg: linear-gradient(150deg, var(--accept-soft) 0%, rgba(255, 255, 255, 0.95) 58%);
      --review-card-reject-border: var(--del);
      --review-card-reject-bg: linear-gradient(150deg, var(--reject-soft) 0%, rgba(255, 255, 255, 0.95) 58%);
      --review-card-active-text: #2e4f79;
      --review-card-index-text: #2e4f79;
      --review-card-index-shadow: 0 8px 16px -14px rgba(19, 37, 64, 0.6);
      --review-premium-surface-white-95: rgba(255, 255, 255, 0.95);
      --review-premium-surface-white-94: rgba(255, 255, 255, 0.94);
      --review-premium-surface-white-92: rgba(255, 255, 255, 0.92);
      --review-premium-surface-white-90: rgba(255, 255, 255, 0.9);
      --review-premium-surface-white-88: rgba(255, 255, 255, 0.88);
      --review-premium-surface-white-82: rgba(255, 255, 255, 0.82);
      --review-premium-surface-white-78: rgba(255, 255, 255, 0.78);
      --review-premium-surface-white-68: rgba(255, 255, 255, 0.68);
      --review-premium-surface-ivory: rgba(253, 254, 255, 0.82);
      --review-premium-surface-mist: rgba(247, 252, 255, 0.9);
      --review-premium-surface-mist-2: rgba(239, 245, 255, 0.68);
      --review-premium-surface-mist-3: rgba(244, 248, 253, 0.94);
      --review-premium-surface-mist-4: rgba(251, 253, 255, 0.66);
      --review-premium-surface-mist-5: rgba(252, 253, 255, 0.85);
      --review-premium-surface-inked: rgba(248, 251, 255, 0.72);
      --review-premium-surface-inked-2: rgba(251, 253, 255, 0.66);
      --review-premium-border-soft: rgba(130, 146, 173, 0.24);
      --review-premium-border-soft-2: rgba(136, 153, 179, 0.34);
      --review-premium-border-soft-3: rgba(117, 138, 173, 0.28);
      --review-premium-stroke-soft: rgba(136, 154, 178, 0.22);
      --review-premium-border-ink: rgba(72, 97, 130, 0.42);
      --review-premium-text-subtle: #5e6e86;
      --review-premium-text-subtle-2: #7e8ca3;
      --review-premium-text-subtle-3: #8190a8;
      --review-premium-text-soft: #6e7f98;
      --review-premium-text-muted: #49566d;
      --review-premium-brand: #7aa8e0;
      --review-premium-run-slash: #8a98ae;
      --review-premium-run-id: #6c7a90;
      --review-premium-progress: #1f4d8f;
      --review-premium-active-border: #176d8b;
      --review-premium-accept-text: #0d6658;
      --review-premium-reject-text: #ab3f34;
      --review-premium-warn-text: #8a4c08;
      --review-premium-accept-rail: rgba(13, 102, 88, 0.28);
      --review-premium-reject-rail: rgba(171, 63, 52, 0.26);
      --review-premium-warn-rail: rgba(138, 76, 8, 0.26);
      --review-premium-accept-fill: rgba(19, 127, 111, 0.1);
      --review-premium-reject-fill: rgba(198, 75, 64, 0.1);
      --review-premium-warn-fill: rgba(184, 123, 22, 0.16);
      --review-premium-dist-unc: #dbe5f2;
      --review-premium-kbd-bg: rgba(255, 255, 255, 0.92);
      --review-premium-kbd-border: rgba(120, 140, 168, 0.3);
      --review-premium-kbd-text: #1f3555;
      --review-premium-diff-head-text: #5d6c83;
      --review-premium-diff-copy: #344157;
      --review-premium-diff-bg: rgba(255, 255, 255, 0.95);
      --review-premium-diff-border: rgba(128, 146, 173, 0.24);
      --review-premium-diff-sep: rgba(134, 153, 180, 0.24);
      --review-premium-shortcut-bg: rgba(255, 255, 255, 0.78);
      --review-premium-shortcut-overlay: rgba(129, 150, 180, 0.28);
      --review-premium-shortcut-title: #173251;
      --review-premium-shortcut-subtitle: #607495;
      --review-premium-shortcut-item: rgba(255, 255, 255, 0.78);
      --review-premium-token-page-bg: var(--review-page-bg);
      --review-premium-token-page-bg-zen: var(--review-page-bg-zen);
      --review-premium-token-page-overlay: var(--review-page-overlay);
      --review-premium-token-page-overlay-zen: var(--review-page-overlay-zen);
      --review-premium-token-grid-line: var(--review-grid-line);
      --review-premium-token-grid-line-soft: var(--review-grid-line-soft);
      --review-premium-token-grid-size: var(--review-grid-size);
      --review-premium-token-grid-offset: var(--review-grid-offset);
      --review-premium-token-shell-bg: var(--review-shell-bg);
      --review-premium-token-shell-bg-zen: var(--review-shell-bg-zen);
      --review-premium-token-shell-border: var(--review-shell-border);
      --review-premium-token-shell-border-zen: var(--review-shell-border-zen);
      --review-premium-token-shell-edge: var(--review-shell-edge);
      --review-premium-token-shell-edge-zen: var(--review-shell-edge-zen);
      --review-premium-token-shell-edge-opacity: var(--review-shell-edge-opacity);
      --review-premium-token-shell-edge-opacity-zen: var(--review-shell-edge-opacity-zen);
      --review-premium-token-shell-edge-blend: var(--review-shell-edge-blend);
      --review-premium-token-shell-edge-blend-zen: var(--review-shell-edge-blend-zen);
      --review-premium-token-shell-shadow: var(--review-shell-shadow);
      --review-premium-token-shell-shadow-zen: var(--review-shell-shadow-zen);
      --review-premium-token-shell-title: var(--review-title);
      --review-premium-token-shell-title-zen: var(--review-title-zen);
      --review-premium-token-shell-dot: var(--review-dot);
      --review-premium-token-shell-dot-ring: var(--review-dot-ring);
      --review-premium-token-shell-mode-border: var(--review-mode-border);
      --review-premium-token-shell-mode-border-zen: var(--review-mode-border-zen);
      --review-premium-token-shell-mode-bg: var(--review-mode-bg);
      --review-premium-token-shell-mode-bg-zen: var(--review-mode-bg-zen);
      --review-premium-token-shell-mode-text: var(--review-mode-text);
      --review-premium-token-shell-mode-text-zen: var(--review-mode-text-zen);
      --review-premium-token-shell-mode-strong: var(--review-mode-strong);
      --review-premium-token-shell-mode-strong-zen: var(--review-mode-strong-zen);
      --review-premium-token-chrome-bg: var(--review-chrome-bg);
      --review-premium-token-chrome-bg-zen: var(--review-chrome-bg-zen);
      --review-premium-token-chrome-border: var(--review-chrome-border);
      --review-premium-token-chrome-border-zen: var(--review-chrome-border-zen);
      --review-premium-token-chrome-shadow: var(--review-chrome-shadow);
      --review-premium-token-body-bg: var(--review-body-bg);
      --review-premium-token-body-bg-zen: var(--review-body-bg-zen);
      --review-premium-token-body-overlay: var(--review-body-overlay);
      --review-premium-token-body-overlay-zen: var(--review-body-overlay-zen);
      --review-premium-token-header-bg: var(--review-header-bg);
      --review-premium-token-header-shadow: var(--review-header-shadow);
      --review-premium-token-header-brand: var(--review-header-brand);
      --review-premium-token-header-pill-bg: var(--review-header-pill-bg);
      --review-premium-token-header-pill-border: var(--review-header-pill-border);
      --review-premium-token-header-pill-text: var(--review-header-pill-ink);
      --review-premium-token-context-pill-bg: var(--review-context-pill-bg);
      --review-premium-token-context-pill-border: var(--review-context-pill-border);
      --review-premium-token-context-pill-text: var(--review-context-pill-text);
      --review-premium-token-progress-fill: var(--review-progress-fill);
      --review-premium-token-batch-bg: var(--review-batch-bg);
      --review-premium-token-batch-border: var(--review-batch-border);
      --review-premium-token-batch-go-bg: var(--review-batch-go-bg);
      --review-premium-token-batch-go-bg-hover: var(--review-batch-go-bg-hover);
      --review-premium-token-batch-go-text: var(--review-batch-go-text);
      --review-premium-token-batch-select-border: var(--review-batch-select-border);
      --review-premium-token-filter-bg: var(--review-premium-surface-white-94);
      --review-premium-token-filter-bg-hover: var(--review-premium-surface-white-95);
      --review-premium-token-filter-border: var(--review-premium-border-soft-2);
      --review-premium-token-filter-border-hover: var(--review-premium-border-soft-3);
      --review-premium-token-filter-shadow: 0 8px 14px -12px rgba(28, 46, 72, 0.28);
      --review-premium-token-filter-active-bg-generic: var(--review-premium-text-muted);
      --review-premium-token-filter-active-text-generic: var(--review-premium-surface-white-95);
      --review-premium-token-filter-text: var(--review-premium-text-muted);
      --review-premium-token-filter-active-bg: var(--review-premium-active-border);
      --review-premium-token-filter-active-text: #fff;
      --review-premium-token-filter-active-accept-bg: var(--review-premium-active-border);
      --review-premium-token-filter-active-accept-text: #fff;
      --review-premium-token-filter-active-decision-bg: var(--review-premium-progress);
      --review-premium-token-filter-active-decision-text: #fff;
      --review-premium-token-filter-focus-shadow: var(--focus-ring);
      --review-premium-token-jump-btn-bg: var(--review-premium-surface-white-95);
      --review-premium-token-jump-btn-bg-hover: var(--review-premium-surface-white-95);
      --review-premium-token-jump-btn-border: var(--review-premium-border-soft-3);
      --review-premium-token-jump-btn-border-hover: var(--review-premium-border-soft-2);
      --review-premium-token-jump-btn-text: var(--review-premium-text-muted);
      --review-premium-token-jump-btn-shadow: 0 8px 16px -14px rgba(30, 54, 89, 0.48);
      --review-premium-token-jump-btn-hover-shadow: 0 8px 16px -14px rgba(30, 54, 89, 0.48);
      --review-premium-token-quick-btn-bg: var(--review-premium-surface-white-95);
      --review-premium-token-quick-btn-bg-hover: var(--review-premium-surface-white-95);
      --review-premium-token-quick-btn-bg-active: var(--review-premium-active-border);
      --review-premium-token-quick-btn-border: var(--review-premium-border-soft-2);
      --review-premium-token-quick-btn-border-hover: var(--review-premium-border-soft-3);
      --review-premium-token-quick-btn-text: var(--review-premium-text-muted);
      --review-premium-token-quick-btn-text-active: #fff;
      --review-premium-token-quick-btn-shadow: 0 8px 12px -12px rgba(31, 54, 87, 0.4);
      --review-premium-token-quick-btn-active-shadow: 0 8px 18px -14px rgba(23, 109, 139, 0.86);
      --review-premium-token-bulk-btn-bg: var(--review-premium-surface-white-95);
      --review-premium-token-bulk-btn-bg-hover: var(--review-premium-surface-white-95);
      --review-premium-token-bulk-btn-border: var(--review-premium-border-soft-2);
      --review-premium-token-bulk-btn-border-hover: var(--review-premium-border-soft-3);
      --review-premium-token-bulk-btn-text: var(--review-premium-text-muted);
      --review-premium-token-bulk-btn-shadow: 0 8px 16px -12px rgba(30, 50, 80, 0.35);
      --review-premium-token-card-bg: var(--review-card-bg);
      --review-premium-token-card-bg-hover: var(--review-card-bg-hover);
      --review-premium-token-card-border: var(--review-card-border);
      --review-premium-token-card-border-hover: var(--review-card-border-hover);
      --review-premium-token-card-shadow: var(--review-card-shadow);
      --review-premium-token-card-shadow-hover: var(--review-card-shadow-hover);
      --review-premium-token-card-shadow-active: var(--review-card-active-shadow);
      --review-premium-token-card-border-active: var(--review-card-active-border);
      --review-premium-token-card-active-bg: var(--review-card-active-bg);
      --review-premium-token-card-active-edge: var(--primary);
      --review-premium-token-card-active-outline: 1px solid rgba(84, 133, 203, 0.2);
      --review-premium-token-card-border-active-changed: var(--review-card-active-changed-border);
      --review-premium-token-card-shadow-active-changed: var(--review-card-active-changed-shadow);
      --review-premium-token-card-bg-active-changed: linear-gradient(145deg, rgba(224, 234, 248, 0.9) 0%, #fdfefe 66%);
      --review-premium-token-card-border-changed: var(--review-card-changed-border);
      --review-premium-token-card-border-pending: var(--review-card-pending-border);
      --review-premium-token-card-bg-pending: var(--review-card-pending-bg);
      --review-premium-token-card-border-accept: var(--review-card-accept-border);
      --review-premium-token-card-border-reject: var(--review-card-reject-border);
      --review-premium-token-card-bg-accept: linear-gradient(145deg, rgba(201, 238, 230, 0.92) 0%, rgba(255, 255, 255, 0.96) 64%);
      --review-premium-token-card-bg-reject: linear-gradient(145deg, rgba(245, 218, 214, 0.9) 0%, rgba(255, 255, 255, 0.95) 64%);
      --review-premium-token-card-active-shadow-accept: 0 16px 28px -20px rgba(14, 94, 82, 0.56), 0 0 0 1px rgba(16, 109, 94, 0.32);
      --review-premium-token-card-active-shadow-reject: 0 16px 28px -20px rgba(143, 51, 42, 0.58), 0 0 0 1px rgba(171, 63, 52, 0.3);
      --review-premium-token-card-active-text: var(--review-premium-text-subtle);
      --review-premium-token-card-index-text: var(--review-card-index-text);
      --review-premium-token-card-index-border: var(--review-premium-border-soft-3);
      --review-premium-token-card-index-bg: var(--review-premium-surface-white-95);
      --review-premium-token-card-index-shadow: var(--review-card-index-shadow);
      --review-premium-token-nav-bg-search: linear-gradient(180deg, var(--review-premium-surface-white-94) 0%, var(--review-premium-surface-mist-3) 100%);
      --review-premium-token-nav-search-border: var(--review-premium-stroke-soft);
      --review-premium-token-nav-search-input-bg: var(--review-premium-surface-white-95);
      --review-premium-token-nav-search-input-border: var(--review-premium-border-soft-2);
      --review-premium-token-nav-search-input-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.68);
      --review-premium-token-nav-section-bg: var(--review-premium-surface-white-90);
      --review-premium-token-nav-section-border: var(--review-premium-border-soft-3);
      --review-premium-token-nav-section-border-hover: var(--review-premium-border-soft-2);
      --review-premium-token-nav-section-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.84), 0 6px 16px -16px rgba(18, 34, 58, 0.48);
      --review-premium-token-jump-input-bg: var(--review-premium-surface-white-94);
      --review-premium-token-jump-input-border: var(--review-premium-border-soft-2);
      --review-premium-token-jump-input-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.7);
      --review-premium-token-card-kind-text: var(--review-premium-text-subtle-2);
      --review-premium-token-card-sec-bg: var(--review-premium-surface-white-95);
      --review-premium-token-card-sec-border: rgba(115, 138, 171, 0.32);
      --review-premium-token-nav-bg: linear-gradient(164deg, var(--review-premium-surface-white-94) 0%, var(--review-premium-surface-mist-3) 100%);
      --review-premium-token-nav-bg-zen: linear-gradient(164deg, rgba(22, 45, 73, 0.68) 0%, rgba(16, 30, 48, 0.62) 100%);
      --review-premium-token-nav-border: var(--review-premium-border-soft);
      --review-premium-token-nav-border-zen: var(--review-premium-border-soft-2);
      --review-premium-token-nav-shadow: var(--shadow-float);
      --review-premium-token-nav-overlay: linear-gradient(180deg, var(--review-premium-surface-white-90) 0%, transparent 16%, transparent 84%, var(--review-premium-surface-white-78) 100%);
      --review-premium-token-nav-progress-bg: rgba(31, 93, 163, 0.09);
      --review-premium-token-nav-progress-border: var(--review-premium-accept-rail);
      --review-premium-token-nav-progress-text: var(--review-premium-progress);
      --review-premium-token-detail-kind-text: #5e6e86;
      --review-premium-token-detail-dot-color: var(--review-premium-border-soft-3);
      --review-premium-token-inspector-bg: var(--gloss);
      --review-premium-token-inspector-shadow: var(--shadow-float);
      --review-premium-token-inspector-border: var(--review-premium-border-soft);
      --review-premium-token-inspector-overlay: linear-gradient(180deg, rgba(255, 255, 255, 0.4) 0%, transparent 18%, transparent 82%, rgba(255, 255, 255, 0.26) 100%);
      --review-premium-token-inspector-head-bg: linear-gradient(180deg, var(--review-premium-surface-white-95) 0%, var(--review-premium-surface-mist-3) 100%);
      --review-premium-token-inspector-head-border: var(--review-premium-stroke-soft);
      --review-premium-token-inspector-title: var(--ink);
      --review-premium-token-inspector-subtitle-text: var(--review-premium-text-subtle);
      --review-premium-token-inspector-label-text: var(--review-premium-text-subtle);
      --review-premium-token-icon-btn-bg: linear-gradient(140deg, var(--review-premium-surface-white-94) 0%, var(--review-premium-surface-mist-2) 100%);
      --review-premium-token-icon-btn-bg-hover: var(--review-premium-surface-white-95);
      --review-premium-token-icon-btn-border: var(--review-premium-border-soft-3);
      --review-premium-token-icon-btn-border-hover: var(--review-premium-border-soft-2);
      --review-premium-token-icon-btn-text: var(--review-premium-text-subtle);
      --review-premium-token-icon-btn-text-hover: var(--review-premium-text-muted);
      --review-premium-token-icon-btn-focus-border: rgba(34, 83, 144, 0.35);
      --review-premium-token-icon-btn-focus-shadow: 0 0 0 3px var(--focus-ring);
      --review-premium-token-pill-border: var(--border-soft);
      --review-premium-token-pill-bg: var(--review-pill-bg);
      --review-premium-token-pill-bg-hover: var(--review-pill-bg-hover);
      --review-premium-token-pill-border-hover: var(--review-pill-border-hover);
      --review-premium-token-pill-text: var(--review-pill-text);
      --review-premium-token-primary-bg: var(--review-primary-bg);
      --review-premium-token-primary-bg-hover: var(--review-primary-bg-hover);
      --review-premium-token-primary-text: var(--review-primary-text);
      --review-premium-token-primary-text-hover: var(--review-primary-text-hover);
      --review-premium-token-primary-shadow: 0 10px 18px -12px rgba(26, 76, 137, 0.78);
      --review-premium-token-primary-shadow-hover: 0 14px 22px -14px rgba(26, 76, 137, 0.72);
      --review-premium-token-export-bg: var(--review-export-bg);
      --review-premium-token-export-bg-hover: var(--review-export-bg-hover);
      --review-premium-token-export-shadow: var(--review-export-shadow);
      --review-premium-token-export-shadow-hover: var(--review-export-shadow-hover);
      --review-premium-token-dl-bg: rgba(255, 255, 255, 0.94);
      --review-premium-token-dl-bg-hover: #fff;
      --review-premium-token-dl-border: 1px solid rgba(30, 74, 135, 0.26);
      --review-premium-token-dl-border-hover: 1px solid rgba(30, 74, 135, 0.45);
      --review-premium-token-dl-text: var(--primary);
      --review-premium-token-dl-text-hover: #143763;

      --review-premium-shell-surface: linear-gradient(180deg, var(--review-premium-surface-white-95) 0%, var(--review-premium-surface-mist-5) 100%);
      --review-premium-shell-surface-zen: linear-gradient(180deg, rgba(16, 31, 52, 0.74) 0%, rgba(16, 30, 48, 0.82) 100%);
      --review-premium-shell-edge: 1px solid var(--review-premium-border-soft-2);
      --review-premium-shell-edge-zen: 1px solid rgba(201, 221, 255, 0.16);
      --review-premium-shell-shadow: 0 28px 48px -34px rgba(8, 16, 31, 0.42);
      --review-premium-header-ribbon: linear-gradient(178deg, rgba(255, 255, 255, 0.95) 0%, rgba(247, 252, 255, 0.78) 100%);
      --review-premium-header-ribbon-zen: linear-gradient(178deg, rgba(22, 45, 73, 0.82) 0%, rgba(17, 33, 55, 0.74) 100%);

      --review-shell-border: var(--review-premium-border-soft);
      --review-shell-border-zen: var(--review-premium-border-soft-2);
      --review-shell-bg: var(--review-premium-shell-surface);
      --review-shell-bg-zen: var(--review-premium-shell-surface-zen);
      --review-shell-shadow: var(--review-premium-shell-shadow);
      --review-shell-shadow-zen: var(--review-shell-shadow);
      --review-shell-edge: var(--review-premium-shell-edge);
      --review-shell-edge-zen: var(--review-premium-shell-edge-zen);
      --review-shell-edge-opacity: 0.58;
      --review-shell-edge-opacity-zen: 0.38;
      --review-shell-edge-blend: screen;
      --review-shell-edge-blend-zen: normal;
      --review-editor-shell-bg: linear-gradient(180deg, #0b1220 0%, #0f172a 100%);
      --review-editor-shell-border: rgba(148, 163, 184, 0.38);
      --review-editor-shell-shadow: 0 24px 44px -34px rgba(2, 6, 23, 0.5), 0 0 0 1px rgba(148, 163, 184, 0.18);
      --review-editor-shell-edge: 1px solid rgba(148, 163, 184, 0.28);
      --review-editor-shell-overlay: inset 0 0 0 1px rgba(148, 163, 184, 0.2);
      --review-editor-chrome-bg: linear-gradient(180deg, rgba(23, 32, 51, 0.95) 0%, rgba(15, 23, 42, 0.92) 100%);
      --review-editor-chrome-border: rgba(148, 163, 184, 0.35);
      --review-editor-chrome-shadow: inset 0 -1px 0 rgba(148, 163, 184, 0.22);
      --review-editor-body-bg: #0a1020;
      --review-editor-body-overlay: inset 0 0 0 1px rgba(148, 163, 184, 0.2), inset 0 12px 28px -22px rgba(2, 6, 23, 0.55);

      --review-chrome-border: var(--review-premium-stroke-soft);
      --review-chrome-border-zen: var(--review-premium-border-soft-3);
      --review-chrome-bg: var(--review-premium-header-ribbon);
      --review-chrome-bg-zen: var(--review-premium-header-ribbon-zen);
      --review-chrome-shadow: inset 0 -1px 0 var(--review-premium-surface-white-90);

      --review-title: var(--review-premium-text-subtle-2);
      --review-title-zen: var(--review-premium-shortcut-title);
      --review-dot: linear-gradient(140deg, var(--review-premium-active-border) 0%, var(--review-premium-brand) 100%);
      --review-dot-ring: var(--review-premium-border-soft-3);
      --review-mode-border: var(--review-premium-border-soft-3);
      --review-mode-border-zen: var(--review-premium-border-soft-2);
      --review-mode-bg: var(--review-premium-surface-white-95);
      --review-mode-bg-zen: rgba(22, 45, 73, 0.6);
      --review-mode-text: var(--review-premium-text-subtle);
      --review-mode-text-zen: var(--review-premium-shortcut-title);
      --review-mode-strong: var(--review-premium-brand);
      --review-mode-strong-zen: var(--review-premium-surface-inked);

      --review-body-bg: var(--review-premium-shell-surface);
      --review-body-bg-zen: var(--review-shell-bg-zen);
      --review-body-overlay: inset 0 0 0 1px var(--review-premium-border-ink), inset 0 22px 34px -30px rgba(11, 21, 38, 0.36);
      --review-body-overlay-zen: inset 0 0 0 1px var(--review-premium-border-ink), inset 0 22px 34px -30px rgba(5, 12, 24, 0.62);

      --review-header-bg: linear-gradient(420px 190px at 7% 8%, var(--review-premium-surface-white-90) 0%, var(--review-premium-surface-white-78) 45%, transparent 100%),
        var(--review-premium-shell-surface);
      --review-header-shadow: 0 14px 24px -18px rgba(11, 22, 44, 0.36);
      --review-header-brand: var(--review-premium-brand);
      --review-header-pill-border: var(--review-premium-border-soft-2);
      --review-header-pill-bg: var(--review-premium-shell-surface);
      --review-header-pill-ink: var(--review-premium-text-subtle);
      --review-context-pill-border: var(--review-premium-border-soft-3);
      --review-context-pill-bg: var(--review-premium-surface-white-95);
      --review-context-pill-text: var(--review-premium-text-subtle-2);
      --review-progress-bg: var(--review-premium-shortcut-overlay);
      --review-progress-fill: linear-gradient(90deg, var(--review-premium-active-border) 0%, var(--review-premium-brand) 60%, var(--review-premium-accept-text) 100%);

      --review-batch-border: var(--review-premium-border-soft-3);
      --review-batch-bg: linear-gradient(180deg, var(--review-premium-surface-white-95) 0%, var(--review-premium-surface-mist-2) 100%);
      --review-batch-label: var(--review-premium-text-soft);
      --review-batch-select-border: var(--review-premium-border-soft-2);
      --review-batch-go-bg: linear-gradient(140deg, var(--review-premium-active-border) 0%, var(--review-premium-brand) 100%);
      --review-batch-go-bg-hover: linear-gradient(140deg, var(--review-premium-brand) 0%, #255997 100%);
      --review-batch-go-text: var(--review-premium-kbd-bg);

      --review-pill-bg: var(--review-premium-surface-white-94);
      --review-pill-bg-hover: var(--review-premium-surface-white-95);
      --review-pill-border-hover: var(--review-premium-border-soft-3);
      --review-pill-text: var(--review-premium-text-soft);
      --review-primary-bg: linear-gradient(138deg, var(--review-premium-active-border) 0%, var(--review-premium-brand) 100%);
      --review-primary-bg-hover: linear-gradient(138deg, var(--review-premium-brand) 0%, #204f8c 100%);
      --review-primary-text: var(--review-premium-surface-white-95);
      --review-primary-text-hover: var(--review-premium-shortcut-title);
      --review-export-bg: linear-gradient(140deg, #12786a 0%, #1c907f 100%);
      --review-export-bg-hover: linear-gradient(140deg, #0f695d 0%, #1a7b6d 100%);
      --review-export-shadow: 0 10px 18px -12px rgba(18, 120, 106, 0.74);
      --review-export-shadow-hover: 0 14px 22px -14px rgba(18, 120, 106, 0.62);

      --review-card-border: var(--review-premium-border-soft);
      --review-card-border-hover: var(--review-premium-border-soft-2);
      --review-card-bg: linear-gradient(160deg, var(--review-premium-surface-white-95) 0%, var(--review-premium-surface-mist-5) 100%);
      --review-card-bg-hover: linear-gradient(160deg, var(--review-premium-surface-white-95) 0%, var(--review-premium-surface-white-94) 100%);
      --review-card-shadow: 0 10px 16px -16px rgba(16, 32, 58, 0.54);
      --review-card-shadow-hover: 0 12px 22px -16px rgba(13, 37, 68, 0.4);
      --review-card-active-border: var(--review-premium-active-border);
      --review-card-active-bg: linear-gradient(148deg, var(--review-premium-surface-white-95) 0%, var(--review-premium-surface-mist-3) 100%);
      --review-card-active-shadow: 0 14px 26px -20px rgba(21, 56, 104, 0.68), 0 0 0 1px var(--review-premium-active-border);
      --review-card-active-changed-border: var(--review-premium-active-border);
      --review-card-active-changed-shadow: 0 15px 28px -20px rgba(24, 60, 109, 0.82), 0 0 0 1px var(--review-premium-active-border);
      --review-card-changed-border: var(--review-premium-border-soft-3);
      --review-card-pending-border: var(--review-premium-progress);
      --review-card-accept-border: var(--review-premium-accept-text);
      --review-card-accept-bg: linear-gradient(150deg, var(--review-premium-accept-fill) 0%, rgba(255, 255, 255, 0.95) 58%);
      --review-card-reject-border: var(--review-premium-reject-text);
      --review-card-reject-bg: linear-gradient(150deg, var(--review-premium-reject-fill) 0%, rgba(255, 255, 255, 0.95) 58%);
      --review-card-active-text: var(--review-premium-text-soft);
      --review-card-index-text: var(--review-premium-text-subtle);
      --review-card-index-shadow: 0 8px 16px -14px rgba(14, 30, 52, 0.54);
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
      background: var(--review-premium-token-page-bg);
      transition: background var(--timing), color var(--timing), box-shadow var(--timing);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(var(--review-premium-token-grid-line) 1px, transparent 1px),
        linear-gradient(90deg, var(--review-premium-token-grid-line-soft) 1px, transparent 1px);
      background-size: var(--review-premium-token-grid-size);
      background-position: 0 0, var(--review-premium-token-grid-offset);
      opacity: var(--review-premium-token-page-overlay);
      z-index: 0;
    }}
    body.zen-mode {{
      background: var(--review-premium-token-page-bg-zen);
    }}
    body.zen-mode::before {{ opacity: var(--review-premium-token-page-overlay-zen); }}

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
      border-radius: var(--radius-xxl);
      border: 1px solid var(--review-premium-token-shell-border);
      background: var(--review-premium-token-shell-bg);
      box-shadow: var(--review-premium-token-shell-shadow);
      overflow: hidden;
      isolation: isolate;
    }}
    body.zen-mode .preview-shell {{
      border-color: var(--review-premium-token-shell-border-zen);
      background: var(--review-premium-token-shell-bg-zen);
      box-shadow: var(--review-premium-token-shell-shadow-zen);
    }}
    body.review-editor-theme .preview-shell {{
      border-color: var(--review-editor-shell-border);
      background: var(--review-editor-shell-bg);
      box-shadow: var(--review-editor-shell-shadow);
    }}
    .preview-shell::before {{
      content: "";
      position: absolute;
      inset: 0;
      border-radius: inherit;
      border: var(--review-premium-token-shell-edge);
      pointer-events: none;
      mix-blend-mode: var(--review-premium-token-shell-edge-blend);
      opacity: var(--review-premium-token-shell-edge-opacity);
      z-index: 0;
    }}
    body.zen-mode .preview-shell::before {{
      border: var(--review-premium-token-shell-edge-zen);
      opacity: var(--review-premium-token-shell-edge-opacity-zen);
      mix-blend-mode: var(--review-premium-token-shell-edge-blend-zen);
    }}
    body.review-editor-theme .preview-shell::before {{
      border: var(--review-editor-shell-edge);
      opacity: 1;
      mix-blend-mode: normal;
      box-shadow: var(--review-editor-shell-overlay);
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
      border-bottom: 1px solid var(--review-premium-token-chrome-border);
      background: var(--review-premium-token-chrome-bg);
      box-shadow: var(--review-premium-token-chrome-shadow);
    }}
    body.zen-mode .preview-chrome {{
      border-bottom-color: var(--review-premium-token-chrome-border-zen);
      background: var(--review-premium-token-chrome-bg-zen);
    }}
    body.review-editor-theme .preview-chrome {{
      border-bottom-color: var(--review-editor-chrome-border);
      background: var(--review-editor-chrome-bg);
      box-shadow: var(--review-editor-chrome-shadow);
    }}
    .preview-title {{
      display: inline-flex;
      align-items: center;
      gap: 0.42rem;
      font-size: 0.68rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--review-premium-token-shell-title);
      font-weight: 700;
      white-space: nowrap;
    }}
    body.zen-mode .preview-title {{ color: var(--review-premium-token-shell-title-zen); }}
    body.review-editor-theme .preview-title {{ color: #e2e8f0; }}
    .preview-dot {{
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 50%;
      background: var(--review-premium-token-shell-dot);
      box-shadow: 0 0 0 2px var(--review-premium-token-shell-dot-ring);
    }}
    .preview-mode {{
      display: inline-flex;
      align-items: center;
      gap: 0.34rem;
      padding: 0.16rem 0.48rem;
      border-radius: 999px;
      border: 1px solid var(--review-premium-token-shell-mode-border);
      background: var(--review-premium-token-shell-mode-bg);
      color: var(--review-premium-token-shell-mode-text);
      font-size: 0.62rem;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    .preview-mode strong {{
      color: var(--review-premium-token-shell-mode-strong);
      font-weight: 700;
      letter-spacing: 0.02em;
      text-transform: none;
      font-size: 0.68rem;
    }}
    body.zen-mode .preview-mode {{
      border-color: var(--review-premium-token-shell-mode-border-zen);
      background: var(--review-premium-token-shell-mode-bg-zen);
      color: var(--review-premium-token-shell-mode-text-zen);
    }}
    body.zen-mode .preview-mode strong {{ color: var(--review-premium-token-shell-mode-strong-zen); }}
    .view-mode-segmented {{
      display: inline-flex;
      align-items: stretch;
      border-radius: 999px;
      border: 1px solid var(--review-premium-token-shell-mode-border);
      background: var(--review-premium-token-shell-mode-bg);
      box-shadow: var(--shadow-softest);
      overflow: hidden;
      min-height: 36px;
      max-height: 36px;
    }}
    body.zen-mode .view-mode-segmented {{
      border-color: var(--review-premium-token-shell-mode-border-zen);
      background: var(--review-premium-token-shell-mode-bg-zen);
    }}
    .view-mode-option {{
      border: none;
      margin: 0;
      border-right: 1px solid var(--review-premium-token-shell-mode-border);
      padding: 0.36rem 0.7rem;
      min-width: 3.1rem;
      color: var(--review-premium-token-shell-mode-text);
      background: transparent;
      font-size: 0.74rem;
      line-height: 1;
      font-weight: 600;
      letter-spacing: 0.02em;
      cursor: pointer;
      transition: background var(--timing), color var(--timing), border-color var(--timing), box-shadow var(--timing), transform 140ms ease;
    }}
    .view-mode-option:last-child {{
      border-right: 0;
    }}
    .view-mode-option:first-child {{
      border-radius: 999px 0 0 999px;
    }}
    .view-mode-option:last-child {{
      border-radius: 0 999px 999px 0;
    }}
    .view-mode-option:hover {{
      background: rgba(111, 147, 196, 0.16);
    }}
    body.zen-mode .view-mode-option:hover {{
      background: rgba(145, 178, 216, 0.2);
    }}
    .view-mode-option.active {{
      color: var(--review-premium-token-primary-text);
      background: var(--review-premium-token-primary-bg);
      border-right-color: transparent;
      font-weight: 700;
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.34);
    }}
    .view-mode-option:focus-visible {{
      outline: none;
      box-shadow: 0 0 0 3px var(--focus-ring);
      position: relative;
      z-index: 1;
    }}
    .view-mode-option[aria-checked="false"] {{
      font-weight: 600;
    }}
    .preview-body {{
      position: relative;
      z-index: 1;
      height: calc(100% - 40px);
      padding-right: 30px;
      border-radius: 0 0 var(--radius-xxl) var(--radius-xxl);
      overflow: hidden;
      background: var(--review-premium-token-body-bg);
    }}
    body.zen-mode .preview-body {{
      background: var(--review-premium-token-body-bg-zen);
    }}
    body.review-editor-theme .preview-body {{
      background: var(--review-editor-body-bg);
    }}
    .preview-body::after {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      box-shadow: var(--review-premium-token-body-overlay);
      z-index: 2;
    }}
    body.zen-mode .preview-body::after {{
      box-shadow: var(--review-premium-token-body-overlay-zen);
    }}
    body.review-editor-theme .preview-body::after {{
      box-shadow: var(--review-editor-body-overlay);
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
      min-height: 62px;
      padding: 0.54rem 0.92rem;
      background: var(--review-premium-token-header-bg);
      backdrop-filter: blur(20px) saturate(1.28);
      -webkit-backdrop-filter: blur(20px) saturate(1.28);
      border-bottom: 1px solid var(--review-premium-token-header-pill-border);
      box-shadow: var(--review-premium-token-header-shadow);
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 0.66rem 0.86rem;
      flex-wrap: wrap;
      z-index: 100;
      transition: transform var(--timing), background var(--timing), box-shadow var(--timing);
      overflow: clip;
    }}
    .slim-header::after {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background: var(--review-premium-token-header-pill-bg);
      z-index: 0;
    }}
    body.zen-mode .slim-header {{ transform: translateY(-100%); }}
    .slim-header > * {{ position: relative; z-index: 1; }}
    .header-left,
    .header-right,
    .command-bar {{
      display: flex;
      align-items: center;
      gap: 0.7rem;
      min-width: 0;
    }}
    .header-left {{ flex-wrap: wrap; }}
    .header-brand {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      white-space: nowrap;
      margin-right: 0.15rem;
      color: var(--review-premium-token-header-brand);
      font-size: 0.7rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .brand-mark {{
      width: 0.72rem;
      height: 0.72rem;
      border-radius: 50%;
      background: linear-gradient(130deg, var(--review-premium-active-border) 0%, var(--review-premium-progress) 55%, var(--review-premium-brand) 100%);
      box-shadow: 0 6px 12px -8px rgba(23, 109, 139, 0.72);
    }}
    .header-right {{ margin-left: auto; justify-content: flex-end; gap: 0.46rem; }}
    .command-bar {{
      justify-content: flex-end;
      gap: 0.46rem;
      flex-wrap: wrap;
    }}
    .command-group {{
      display: inline-flex;
      align-items: center;
      gap: 0.42rem;
      min-width: 0;
      border-radius: 999px;
      padding: 0.1rem;
    }}
    .command-group--primary {{
      background: linear-gradient(160deg, var(--review-premium-surface-white-95) 0%, var(--review-premium-surface-white-92) 100%);
      border: 1px solid var(--review-premium-token-shell-mode-border);
      box-shadow: var(--shadow-softest);
    }}
    .command-group--secondary {{
      background: var(--review-premium-surface-mist-2);
      border: 1px solid var(--review-premium-token-pill-border);
      box-shadow: var(--shadow-soft);
    }}
    .run-title {{
      font-size: 0.88rem;
      font-weight: 500;
      color: var(--review-premium-token-context-pill-text);
      max-width: min(56vw, 640px);
      display: inline-flex;
      flex-direction: column;
      gap: 0.3rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .run-title-main {{
      display: inline-flex;
      align-items: center;
      gap: 0.34rem;
      min-width: 0;
      max-width: 100%;
      border: 1px solid var(--review-premium-token-header-pill-border);
      background: var(--review-premium-token-header-pill-bg);
      border-radius: 999px;
      padding: 0.26rem 0.62rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      box-shadow: var(--shadow-softest);
    }}
    .run-title-pills {{
      display: inline-flex;
      align-items: center;
      gap: 0.28rem;
      flex-wrap: wrap;
      max-width: 100%;
    }}
    .run-title strong {{
      font-family: var(--font-display);
      color: var(--review-premium-text-muted);
      font-size: 0.97rem;
      font-weight: 600;
      letter-spacing: 0.01em;
    }}
    .run-slash {{ color: var(--review-premium-run-slash); margin-inline: 0.22rem; }}
    .run-id {{ color: var(--review-premium-run-id); }}
    .run-context {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      max-width: min(34vw, 392px);
      padding: 0.12rem 0;
      flex-wrap: wrap;
    }}
    .context-pill {{
      display: inline-flex;
      align-items: center;
      padding: 0.22rem 0.48rem;
      border-radius: 999px;
      border: 1px solid var(--review-premium-token-context-pill-border);
      background: var(--review-premium-token-context-pill-bg);
      font-size: 0.64rem;
      color: var(--review-premium-token-context-pill-text);
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.65), 0 8px 20px -16px rgba(28, 48, 80, 0.35);
      transition: border-color var(--timing), color var(--timing), background var(--timing);
    }}
    .context-progress {{
      position: relative;
      width: 108px;
      height: 8px;
      border-radius: 999px;
      background: var(--review-premium-token-context-pill-bg);
      overflow: hidden;
      border: 1px solid var(--review-premium-token-context-pill-border);
      box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.45);
    }}
    .context-progress-fill {{
      position: absolute;
      inset: 0 auto 0 0;
      width: 0%;
      border-radius: inherit;
      background: var(--review-premium-token-progress-fill);
      background-size: 190% 100%;
      animation: progressSheen 2.8s linear infinite;
      transition: width 260ms var(--timing-soft);
    }}
    @keyframes progressSheen {{
      0% {{ background-position: 0% 0%; }}
      100% {{ background-position: 190% 0%; }}
    }}
    .actions-group {{
      display: flex;
      align-items: center;
      gap: 0.45rem;
      flex-wrap: wrap;
    }}
    .actions-group.secondary-actions {{ flex-wrap: wrap; justify-content: flex-end; }}
    .batch-switcher {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.22rem 0.33rem;
      border-radius: 999px;
      border: 1px solid var(--review-premium-token-batch-border);
      background: var(--review-premium-token-batch-bg);
      box-shadow: var(--shadow-softest);
      max-width: min(44vw, 430px);
      white-space: nowrap;
      transition: box-shadow var(--timing), border-color var(--timing), transform var(--timing);
    }}
    .batch-switcher:hover {{
      box-shadow: 0 18px 28px -24px rgba(17, 33, 52, 0.5);
    }}
    .batch-switcher:focus-within {{
      border-color: var(--review-premium-active-border);
    }}
    .batch-switcher[hidden] {{ display: none; }}
    .batch-switch-label {{
      font-size: 0.62rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-weight: 700;
      color: var(--review-premium-text-subtle-3);
      white-space: nowrap;
    }}
    .batch-switch-select {{
      min-width: 180px;
      max-width: 300px;
      border-radius: 999px;
      border: 1px solid var(--review-batch-select-border);
      background: var(--review-premium-surface-white-95);
      color: var(--review-premium-text-subtle);
      font-family: inherit;
      font-size: 0.74rem;
      font-weight: 500;
      padding: 0.32rem 0.6rem;
      transition: border-color var(--timing), box-shadow var(--timing);
    }}
    .batch-switch-select:focus-visible {{
      outline: none;
      border-color: var(--review-premium-active-border);
      box-shadow: 0 0 0 3px var(--focus-ring);
    }}
    .batch-switch-meta {{
      font-size: 0.67rem;
      color: var(--review-premium-text-subtle);
      white-space: nowrap;
      max-width: 130px;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .batch-switch-go {{
      padding: 0.42rem 0.72rem;
      border-radius: 999px;
      border: 1px solid transparent;
      background: var(--review-batch-go-bg);
      color: var(--review-premium-surface-white-95);
      cursor: pointer;
      font-size: 0.72rem;
      font-weight: 700;
      transition: border-color var(--timing), color var(--timing), background var(--timing), box-shadow var(--timing);
      white-space: nowrap;
      height: 2rem;
    }}
    .batch-switch-go:hover {{
      background: var(--review-batch-go-bg-hover);
      box-shadow: 0 12px 18px -16px rgba(22, 67, 126, 0.8);
    }}
    .batch-switch-go:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    .icon-btn {{
      width: 36px;
      height: 36px;
      border-radius: 10px;
      border: 1px solid var(--review-premium-token-icon-btn-border);
      background: var(--review-premium-token-icon-btn-bg);
      color: var(--review-premium-token-icon-btn-text);
      cursor: pointer;
      transition: background var(--timing), border-color var(--timing), color var(--timing), transform 140ms ease;
    }}
    .icon-btn:hover {{ background: var(--review-premium-token-icon-btn-bg-hover); border-color: var(--review-premium-token-icon-btn-border-hover); color: var(--review-premium-token-icon-btn-text-hover); }}
    .icon-btn:active {{ transform: translateY(1px); }}
    .icon-btn:focus-visible {{
      outline: none;
      border-color: var(--review-premium-token-icon-btn-focus-border);
      box-shadow: var(--review-premium-token-icon-btn-focus-shadow);
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
    .shortcut-launch {{
      padding-inline: 0.78rem;
      gap: 0.36rem;
    }}
    .shortcut-launch::after {{
      content: "?";
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 1rem;
      height: 1rem;
      border-radius: 999px;
      border: 1px solid var(--review-premium-border-soft-3);
      background: var(--review-premium-shortcut-bg);
      font-size: 0.66rem;
      font-weight: 800;
      color: var(--review-premium-shortcut-subtitle);
      line-height: 1;
    }}
    .pill-btn {{
      padding: 0.5rem 0.88rem;
      border: 1px solid var(--review-premium-token-pill-border);
      background: var(--review-premium-token-pill-bg);
      color: var(--review-premium-token-pill-text);
      box-shadow: var(--shadow-soft);
    }}
    .pill-btn:hover {{ background: var(--review-premium-token-pill-bg-hover); border-color: var(--review-premium-token-pill-border-hover); color: var(--review-premium-token-pill-text); }}
    .primary-btn {{
      padding: 0.52rem 0.99rem;
      border: 1px solid transparent;
      background: var(--review-premium-token-primary-bg);
      color: var(--review-premium-token-primary-text);
      box-shadow: var(--review-premium-token-primary-shadow);
      letter-spacing: 0.01em;
      font-weight: 700;
    }}
    .primary-btn:hover {{ background: var(--review-premium-token-primary-bg-hover); box-shadow: var(--review-premium-token-primary-shadow-hover); color: var(--review-premium-token-primary-text-hover); }}
    .primary-btn:active {{ transform: translateY(1px); }}
    .command-group--primary .primary-btn {{
      padding: 0.55rem 1rem;
    }}
    .command-group--secondary .primary-btn,
    .command-group--secondary .pill-btn {{
      font-size: 0.74rem;
    }}
    .command-group--secondary .primary-btn {{
      background: linear-gradient(138deg, rgba(255, 255, 255, 0.96) 0%, rgba(241, 247, 255, 0.86) 100%);
      border-color: var(--review-premium-token-pill-border);
      color: var(--review-premium-token-primary-text-hover);
      box-shadow: var(--shadow-softest);
      font-size: 0.74rem;
    }}
    .command-group--secondary .primary-btn:hover {{
      background: linear-gradient(138deg, #f0f6ff 0%, #eff4ff 100%);
    }}
    .export-btn {{ background: var(--review-premium-token-export-bg); box-shadow: var(--review-premium-token-export-shadow); }}
    .export-btn:hover {{ background: var(--review-premium-token-export-bg-hover); box-shadow: var(--review-premium-token-export-shadow-hover); }}
    .dl-pill {{
      padding: 0.5rem 0.82rem;
      border: var(--review-premium-token-dl-border);
      background: var(--review-premium-token-dl-bg);
      color: var(--review-premium-token-dl-text);
      box-shadow: var(--shadow-soft);
    }}
    .dl-pill:hover {{ background: var(--review-premium-token-dl-bg-hover); border-color: var(--review-premium-token-dl-border-hover); color: var(--review-premium-token-dl-text-hover); }}
    .sec-pill {{
      padding: 0.2rem 0.56rem;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 600;
      color: var(--review-premium-text-subtle);
      border: 1px solid var(--review-premium-border-soft-3);
      background: var(--review-premium-surface-white-94);
      white-space: nowrap;
    }}
    .nav-progress {{
      margin-left: 0.4rem;
      padding: 0.2rem 0.56rem;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 600;
      color: var(--review-premium-token-nav-progress-text);
      border: 1px solid var(--review-premium-token-nav-progress-border);
      background: var(--review-premium-token-nav-progress-bg);
      white-space: nowrap;
    }}

    .floating-navigator {{
      position: absolute;
      top: 0.92rem;
      left: 0.95rem;
      bottom: 0.95rem;
      width: 350px;
      background: var(--review-premium-token-nav-bg);
      backdrop-filter: blur(20px) saturate(1.18);
      -webkit-backdrop-filter: blur(20px) saturate(1.18);
      border: 1px solid var(--review-premium-token-nav-border);
      border-radius: var(--radius-xl);
      box-shadow: var(--review-premium-token-nav-shadow);
      display: flex;
      flex-direction: column;
      transition: transform var(--timing), opacity var(--timing), box-shadow var(--timing);
      z-index: 50;
      overflow: hidden;
      isolation: isolate;
    }}
    .floating-navigator::before {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background: var(--review-premium-token-nav-overlay);
      z-index: -1;
    }}
    body.zen-mode .floating-navigator,
    body.nav-hidden .floating-navigator {{
      transform: translateX(calc(-100% - 2rem));
      opacity: 0;
    }}

    .nav-search {{
      padding: 0.9rem 0.95rem 0.86rem;
      border-bottom: 1px solid var(--review-premium-stroke-soft);
      display: flex;
      flex-direction: column;
      gap: 0.62rem;
      background: var(--review-premium-token-nav-bg-search);
    }}
    .nav-section {{
      border: 1px solid var(--review-premium-token-nav-section-border);
      border-radius: 12px;
      background: var(--review-premium-token-nav-section-bg);
      padding: 0.52rem 0.56rem 0.58rem;
      box-shadow: var(--review-premium-token-nav-section-shadow);
      transition: transform var(--timing), border-color var(--timing), box-shadow var(--timing);
    }}
    .nav-section:hover {{
      transform: translateY(-1px);
      border-color: var(--review-premium-token-nav-section-border-hover);
    }}
    .nav-section-title {{
      margin-bottom: 0.38rem;
      font-size: 0.62rem;
      letter-spacing: 0.08em;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--review-premium-text-subtle-3);
    }}
    .nav-section-distribution .quick-row:first-of-type {{ margin-top: 0.52rem; }}
    .nav-search input {{
      width: 100%;
      border-radius: 10px;
      border: 1px solid var(--review-premium-token-nav-search-input-border);
      background: var(--review-premium-token-nav-search-input-bg);
      color: var(--review-premium-text-muted);
      padding: 0.62rem 0.72rem;
      font-family: inherit;
      font-size: 0.8rem;
      transition: border-color var(--timing), box-shadow var(--timing), background var(--timing);
      box-shadow: var(--review-premium-token-nav-search-input-shadow);
    }}
    .nav-search input:focus-visible {{
      outline: none;
      border-color: var(--review-premium-token-filter-focus-shadow);
      box-shadow: 0 0 0 3px var(--focus-ring);
      background: var(--review-premium-token-nav-search-input-bg);
    }}
    .jump-row {{ margin-top: 0.48rem; display: flex; gap: 0.45rem; align-items: center; }}
    .jump-row input {{
      width: 100%;
      border-radius: 9px;
      border: 1px solid var(--review-premium-token-jump-input-border);
      background: var(--review-premium-token-jump-input-bg);
      padding: 0.52rem 0.64rem;
      font-family: inherit;
      font-size: 0.79rem;
      box-shadow: var(--review-premium-token-jump-input-shadow);
    }}
    .jump-row button {{
      border-radius: 9px;
      border: 1px solid var(--review-premium-token-jump-btn-border);
      background: var(--review-premium-token-jump-btn-bg);
      color: var(--review-premium-token-jump-btn-text);
      padding: 0.5rem 0.72rem;
      cursor: pointer;
      font-size: 0.75rem;
      font-weight: 700;
      white-space: nowrap;
      transition: border-color var(--timing), color var(--timing), background var(--timing), transform var(--timing), box-shadow var(--timing);
      box-shadow: var(--review-premium-token-jump-btn-shadow);
    }}
    .jump-row button:hover {{ border-color: var(--review-premium-token-jump-btn-border-hover); color: var(--review-premium-token-jump-btn-text); background: var(--review-premium-token-jump-btn-bg-hover); box-shadow: var(--review-premium-token-jump-btn-hover-shadow); }}

    .dist-bar {{ display: flex; height: 7px; border-radius: 4px; overflow: hidden; background: var(--review-premium-surface-mist-4); }}
    .dist-segment {{ height: 100%; }}
    .dist-ins {{ background: var(--ins); }}
    .dist-del {{ background: var(--del); }}
    .dist-rep {{ background: var(--rep); }}
    .dist-mov {{ background: var(--mov); }}
    .dist-unc {{ background: var(--review-premium-dist-unc); }}
    .quick-row {{ margin-top: 0.52rem; display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; }}
    .quick-btn {{
      border-radius: 999px;
      border: 1px solid var(--review-premium-token-quick-btn-border);
      background: var(--review-premium-token-quick-btn-bg);
      color: var(--review-premium-token-quick-btn-text);
      padding: 0.3rem 0.64rem;
      font-size: 0.7rem;
      font-weight: 700;
      cursor: pointer;
      white-space: nowrap;
      transition: border-color var(--timing), color var(--timing), background var(--timing), box-shadow var(--timing), transform var(--timing);
      box-shadow: var(--review-premium-token-quick-btn-shadow);
    }}
    .quick-btn:hover {{ border-color: var(--review-premium-token-quick-btn-border-hover); color: var(--review-premium-token-quick-btn-text); background: var(--review-premium-token-quick-btn-bg-hover); box-shadow: var(--review-premium-token-quick-btn-shadow); transform: translateY(-1px); }}
    .quick-btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    .quick-btn.active {{ background: var(--review-premium-token-quick-btn-bg-active); border-color: var(--review-premium-token-quick-btn-bg-active); color: var(--review-premium-token-quick-btn-text-active); box-shadow: var(--review-premium-token-quick-btn-active-shadow); }}
    .quick-btn.subtle {{ font-weight: 600; }}
    .quick-count {{ font-size: 0.7rem; color: var(--review-premium-text-subtle); }}

    .filter-group {{
      border-bottom: 1px solid var(--border-soft);
      padding: 0.24rem 0 0.22rem;
      background: linear-gradient(180deg, rgba(251, 253, 255, 0.66) 0%, rgba(247, 250, 255, 0.58) 100%);
    }}
    .filter-group:last-of-type {{ border-bottom: 1px solid var(--review-premium-stroke-soft); }}
    .filter-label {{
      padding: 0.34rem 1rem 0.2rem;
      font-size: 0.64rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      color: var(--review-premium-text-subtle-3);
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
      border: 1px solid var(--review-premium-token-filter-border);
      background: var(--review-premium-token-filter-bg);
      color: var(--review-premium-token-filter-text);
      cursor: pointer;
      white-space: nowrap;
      transition: background var(--timing), border-color var(--timing), color var(--timing), box-shadow var(--timing), transform var(--timing);
      box-shadow: 0 8px 14px -12px rgba(28, 46, 72, 0.28);
    }}
    .filter-btn {{ padding: 0.3rem 0.6rem; font-size: 0.74rem; }}
    .facet-filter-btn,
    .decision-filter-btn {{ padding: 0.28rem 0.55rem; font-size: 0.7rem; }}
    .filter-btn:hover,
    .facet-filter-btn:hover,
    .decision-filter-btn:hover {{
      border-color: var(--review-premium-token-filter-border-hover);
      background: var(--review-premium-token-filter-bg-hover);
      transform: translateY(-1px);
    }}
    .filter-btn:focus-visible,
    .facet-filter-btn:focus-visible,
    .decision-filter-btn:focus-visible {{
      outline: none;
      border-color: var(--review-premium-token-filter-border-hover);
      box-shadow: 0 0 0 3px var(--review-premium-token-filter-focus-shadow);
    }}
    .filter-btn.active {{ background: var(--review-premium-token-filter-active-bg-generic); color: var(--review-premium-token-filter-active-text-generic); border-color: var(--review-premium-token-filter-active-bg-generic); box-shadow: 0 9px 15px -12px rgba(15, 30, 54, 0.84); }}
    .facet-filter-btn.active {{ background: var(--review-premium-token-filter-active-accept-bg); color: var(--review-premium-token-filter-active-accept-text); border-color: var(--review-premium-token-filter-active-accept-bg); box-shadow: var(--review-premium-token-card-active-shadow-accept); }}
    .decision-filter-btn.active {{ background: var(--review-premium-token-filter-active-decision-bg); color: var(--review-premium-token-filter-active-decision-text); border-color: var(--review-premium-token-filter-active-decision-bg); box-shadow: var(--review-premium-token-card-active-shadow-reject); }}

    .bulk-row {{
      padding: 0.62rem 1rem;
      border-bottom: 1px solid var(--review-premium-stroke-soft);
      display: flex;
      gap: 0.42rem;
      align-items: center;
      flex-wrap: wrap;
      background: linear-gradient(180deg, var(--review-premium-surface-white-95) 0%, var(--review-premium-surface-mist-5) 100%);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.58);
    }}
    .bulk-btn {{
      border-radius: 9px;
      border: 1px solid var(--review-premium-token-bulk-btn-border);
      background: var(--review-premium-token-bulk-btn-bg);
      color: var(--review-premium-token-bulk-btn-text);
      padding: 0.38rem 0.6rem;
      font-size: 0.71rem;
      font-weight: 600;
      cursor: pointer;
      transition: border-color var(--timing), background var(--timing), color var(--timing), box-shadow var(--timing), transform var(--timing);
      box-shadow: var(--review-premium-token-bulk-btn-shadow);
    }}
    .bulk-btn:disabled {{ opacity: 0.55; cursor: not-allowed; }}
    .bulk-btn:hover {{ background: var(--review-premium-token-bulk-btn-bg-hover); border-color: var(--review-premium-token-bulk-btn-border-hover); color: var(--review-premium-token-bulk-btn-text); transform: translateY(-1px); }}
    .bulk-status {{ margin-left: auto; font-size: 0.71rem; color: var(--review-premium-text-muted); min-height: 1rem; }}
    .bulk-status.error {{ color: #b13e35; }}
    .decision-guide {{
      padding: 0.5rem 1rem 0.62rem;
      border-bottom: 1px solid var(--review-premium-stroke-soft);
      display: flex;
      gap: 0.45rem;
      align-items: center;
      flex-wrap: wrap;
      background: linear-gradient(180deg, var(--review-premium-surface-white-95) 0%, var(--review-premium-surface-mist-5) 100%);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.66);
    }}
    .decision-guide-note {{ font-size: 0.7rem; color: var(--review-premium-text-subtle); line-height: 1.2; }}
    .decision-summary {{
      margin-left: 0.42rem;
      padding: 0.2rem 0.56rem;
      border-radius: 999px;
      font-size: 0.7rem;
      font-weight: 600;
      color: var(--review-premium-accept-text);
      border: 1px solid var(--review-premium-accept-rail);
      background: var(--review-premium-accept-fill);
      white-space: nowrap;
    }}

    .change-list-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      padding: 0.46rem 1rem 0.4rem;
      border-bottom: 1px solid rgba(136, 154, 178, 0.22);
      background:
        linear-gradient(180deg, var(--review-premium-surface-white-95) 0%, var(--review-premium-surface-mist-3) 100%);
      position: sticky;
      top: 0;
      z-index: 3;
    }}
    .change-list-title {{
      font-size: 0.68rem;
      font-weight: 700;
      letter-spacing: 0.07em;
      text-transform: uppercase;
      color: var(--review-premium-text-subtle);
    }}
    .change-list-note {{
      font-size: 0.65rem;
      color: var(--review-premium-text-subtle);
      white-space: nowrap;
    }}
    .change-list {{
      flex: 1;
      overflow-y: auto;
      padding: 0.48rem 0.52rem 0.8rem;
      scroll-behavior: smooth;
      transition: opacity 180ms ease, transform 180ms ease, filter 220ms ease;
      scrollbar-color: rgba(133, 155, 186, 0.7) rgba(240, 245, 252, 0.2);
    }}
    .change-list::-webkit-scrollbar {{
      width: 10px;
    }}
    .change-list::-webkit-scrollbar-thumb {{
      background: rgba(131, 152, 179, 0.4);
      border-radius: 999px;
      border: 2px solid rgba(237, 243, 252, 0.4);
    }}
    .change-list::-webkit-scrollbar-track {{
      background: rgba(237, 243, 252, 0.45);
    }}
    .change-list.scope-shift {{ opacity: 0.68; transform: translateY(2px); filter: saturate(0.92); }}
    .empty-state {{
      padding: 1.7rem 1rem;
      text-align: center;
      color: var(--review-premium-text-subtle);
      font-size: 0.8rem;
      border-radius: 12px;
      border: 1px dashed var(--review-premium-border-soft-2);
      background: var(--review-premium-surface-white-95);
    }}
    .detail-card {{
      position: relative;
      padding: 0.74rem 0.78rem;
      border-radius: 14px;
      margin-bottom: 0.34rem;
      cursor: pointer;
      transition: transform var(--timing), border-color var(--timing), box-shadow var(--timing), background var(--timing);
      border: 1px solid var(--review-premium-token-card-border);
      border-left: 4px solid transparent;
      background: var(--review-premium-token-card-bg);
      animation: slideUpFade 420ms cubic-bezier(0.2, 0.74, 0.24, 1) backwards;
      box-shadow: var(--review-premium-token-card-shadow);
    }}
    .detail-card:hover {{
      background: var(--review-premium-token-card-bg-hover);
      border-color: var(--review-premium-token-card-border-hover);
      transform: translateX(2px);
      box-shadow: var(--review-premium-token-card-shadow-hover);
    }}
    .detail-card::after {{
      content: "";
      position: absolute;
      inset: auto 12px 10px 12px;
      height: 1px;
      background: linear-gradient(90deg, var(--review-premium-border-soft-3), transparent);
      z-index: 0;
      pointer-events: none;
      opacity: 0;
      transition: opacity var(--timing);
    }}
    .detail-card.active {{
      background: var(--review-premium-token-card-active-bg);
      border-left-color: var(--review-premium-token-card-active-edge);
      border-color: var(--review-premium-token-card-border-active);
      box-shadow: var(--review-premium-token-card-shadow-active);
      transform: translateX(4px);
      z-index: 10;
      outline: var(--review-premium-token-card-active-outline);
      outline-offset: -2px;
    }}
    .detail-card.active::after {{
      opacity: 1;
    }}
    .detail-card.active.is-changed {{
      border-left-color: var(--review-premium-token-card-border-active-changed);
      box-shadow: var(--review-premium-token-card-shadow-active-changed);
    }}
    .detail-card.is-changed {{ border-left-color: var(--review-premium-token-card-border-changed); }}
    .detail-card.decision-pending {{
      border-right: 4px solid var(--review-premium-token-card-border-pending);
      background: var(--review-premium-token-card-bg-pending);
    }}
    .detail-card.decision-accept {{
      border-right: 4px solid var(--review-premium-token-card-border-accept);
      background: var(--review-premium-token-card-bg-accept);
    }}
    .detail-card.decision-reject {{
      border-right: 4px solid var(--review-premium-token-card-border-reject);
      background: var(--review-premium-token-card-bg-reject);
    }}
      .detail-card.active.decision-pending {{
      border-left-color: var(--review-premium-token-card-border-pending);
      border-right-color: var(--review-premium-token-card-border-pending);
      background: var(--review-premium-token-card-bg-active-changed);
      box-shadow: var(--review-premium-token-card-shadow-active-changed);
    }}
    .detail-card.active.decision-accept {{
      border-left-color: var(--review-premium-token-card-border-accept);
      border-right-color: var(--review-premium-token-card-border-accept);
      background: var(--review-premium-token-card-bg-accept);
      box-shadow: var(--review-premium-token-card-active-shadow-accept);
    }}
    .detail-card.active.decision-reject {{
      border-left-color: var(--review-premium-token-card-border-reject);
      border-right-color: var(--review-premium-token-card-border-reject);
      background: var(--review-premium-token-card-bg-reject);
      box-shadow: var(--review-premium-token-card-active-shadow-reject);
    }}
    .detail-title {{
      font-size: 0.83rem;
      font-weight: 600;
      color: var(--review-premium-text-soft);
      line-height: 1.3;
      margin-bottom: 0.34rem;
      position: relative;
      z-index: 1;
      display: flex;
      align-items: center;
      gap: 0.39rem;
      min-width: 0;
    }}
    .detail-index {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 1.34rem;
      height: 1.34rem;
      border-radius: 50%;
      font-size: 0.63rem;
      font-weight: 700;
      color: var(--review-premium-token-card-index-text);
      border: 1px solid var(--review-premium-token-card-index-border);
      background: var(--review-premium-token-card-index-bg);
      flex-shrink: 0;
      box-shadow: var(--review-premium-token-card-index-shadow);
    }}
    .detail-title > span:last-child {{
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
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
    .detail-kind {{ color: var(--review-premium-token-detail-kind-text); font-weight: 700; }}
    .detail-dot {{ color: var(--review-premium-token-detail-dot-color); }}
    .detail-sec {{
      display: inline-block;
      padding: 0.08rem 0.35rem;
      border-radius: 999px;
      border: 1px solid var(--review-premium-token-card-sec-border);
      background: var(--review-premium-token-card-sec-bg);
      color: var(--review-premium-text-muted);
      font-weight: 700;
    }}
    .detail-excerpt {{
      font-size: 0.78rem;
      color: var(--review-premium-text-muted);
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
      border: 1px solid var(--review-premium-border-soft-3);
      color: var(--review-premium-text-subtle);
      background: var(--review-premium-surface-white-94);
      text-transform: uppercase;
    }}
    .facet-badge.format-only {{
      color: var(--review-premium-brand);
      border-color: rgba(12, 106, 116, 0.3);
      background: rgba(12, 106, 116, 0.13);
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
    .decision-tag.accept {{ color: var(--review-premium-accept-text); border-color: var(--review-premium-accept-rail); background: var(--review-premium-accept-fill); }}
    .decision-tag.reject {{ color: var(--review-premium-reject-text); border-color: var(--review-premium-reject-rail); background: var(--review-premium-reject-fill); }}
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
    .decision-state.saving {{ color: var(--review-premium-warn-text); border-color: var(--review-premium-warn-rail); background: var(--review-premium-warn-fill); }}
    .decision-state.saved {{ color: var(--review-premium-accept-text); border-color: var(--review-premium-accept-rail); background: var(--review-premium-accept-fill); }}
    .decision-state.error {{ color: var(--review-premium-reject-text); border-color: var(--review-premium-reject-rail); background: var(--review-premium-reject-fill); }}

    .floating-inspector {{
      position: absolute;
      bottom: 1.58rem;
      right: 1.7rem;
      width: 468px;
      max-height: 56vh;
      background: var(--review-premium-token-inspector-bg);
      backdrop-filter: blur(18px) saturate(1.1);
      -webkit-backdrop-filter: blur(18px) saturate(1.1);
      border: 1px solid var(--review-premium-token-inspector-border);
      border-radius: var(--radius-xl);
      box-shadow: var(--review-premium-token-inspector-shadow);
      display: flex;
      flex-direction: column;
      z-index: 60;
      transform: translateY(18px);
      opacity: 0;
      pointer-events: none;
      transition: transform var(--timing), opacity var(--timing);
      overflow: hidden;
      isolation: isolate;
    }}
    .floating-inspector::before {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      z-index: -1;
      background: var(--review-premium-token-inspector-overlay);
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
      border-bottom: 1px solid var(--review-premium-token-inspector-head-border);
      background: var(--review-premium-token-inspector-head-bg);
    }}
    .insp-head h3 {{
      margin: 0;
      font-family: var(--font-display);
      font-size: 0.96rem;
      letter-spacing: 0.01em;
      color: var(--review-premium-token-inspector-title);
      font-weight: 600;
    }}
    .insp-subtitle {{
      margin-top: 0.22rem;
      font-size: 0.67rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--review-premium-token-inspector-subtitle-text);
      font-weight: 700;
    }}
    .insp-body {{ padding: 0.95rem 1rem 1.05rem; overflow-y: auto; }}
    .insp-label {{ font-size: 0.73rem; color: var(--review-premium-token-inspector-label-text); margin-bottom: 0.68rem; }}
    .diff-block {{
      background: var(--review-premium-diff-bg);
      border: 1px solid var(--review-premium-diff-border);
      border-radius: 12px;
      margin-bottom: 0.96rem;
      overflow: hidden;
      box-shadow: 0 11px 18px -18px rgba(21, 38, 62, 0.48);
    }}
    .diff-hdr {{
      padding: 0.52rem 0.75rem;
      background: var(--review-premium-surface-mist-2);
      font-size: 0.69rem;
      font-weight: 700;
      color: var(--review-premium-diff-head-text);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      border-bottom: 1px solid var(--review-premium-diff-sep);
    }}
    .diff-content {{ padding: 0.75rem; font-size: 0.84rem; line-height: 1.44; white-space: pre-wrap; color: var(--review-premium-diff-copy); }}
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
      background: var(--review-premium-kbd-bg);
      backdrop-filter: blur(18px) saturate(1.08);
      border: 1px solid var(--review-premium-kbd-border);
      padding: 0.46rem 0.9rem;
      border-radius: 999px;
      z-index: 100;
      box-shadow: var(--shadow-float);
      transition: opacity var(--timing);
    }}
    body.zen-mode .kbd-hints {{ opacity: 0; pointer-events: none; }}
    .kbd-hint {{ font-size: 0.72rem; color: var(--review-premium-text-subtle); display: flex; align-items: center; gap: 0.38rem; white-space: nowrap; }}
    kbd {{
      background: var(--review-premium-kbd-bg);
      border: 1px solid var(--review-premium-kbd-border);
      border-radius: 5px;
      padding: 0.1rem 0.4rem;
      font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
      font-weight: 600;
      color: var(--review-premium-kbd-text);
    }}
    .shortcut-overlay {{
      position: fixed;
      inset: 0;
      z-index: 240;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 1rem;
      background: var(--review-premium-shortcut-overlay);
      backdrop-filter: blur(14px) saturate(1.08);
      -webkit-backdrop-filter: blur(14px) saturate(1.08);
    }}
    .shortcut-overlay.open {{ display: flex; }}
    .shortcut-panel {{
      width: min(820px, calc(100vw - 2.2rem));
      max-height: min(82vh, 760px);
      overflow: auto;
      border-radius: var(--radius-xl);
      border: 1px solid var(--review-premium-border-soft-3);
      background: linear-gradient(170deg, var(--review-premium-shortcut-item) 0%, var(--review-premium-shortcut-bg) 100%);
      box-shadow: 0 32px 60px -30px rgba(5, 12, 24, 0.84);
      padding: 1rem;
      animation: shortcutRise 180ms cubic-bezier(0.2, 0.74, 0.24, 1);
      backdrop-filter: blur(10px) saturate(1.15);
      -webkit-backdrop-filter: blur(10px) saturate(1.15);
    }}
    @keyframes shortcutRise {{
      from {{ transform: translateY(8px) scale(0.985); opacity: 0; }}
      to {{ transform: translateY(0) scale(1); opacity: 1; }}
    }}
    .shortcut-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      margin-bottom: 0.72rem;
      padding: 0.35rem 0.2rem 0.55rem;
      border-bottom: 1px solid var(--review-premium-border-soft-3);
    }}
    .shortcut-title {{
      margin: 0;
      font-family: var(--font-display);
      font-size: 1.02rem;
      font-weight: 600;
      color: var(--review-premium-shortcut-title);
      letter-spacing: 0.01em;
    }}
    .shortcut-subtitle {{
      margin-top: 0.18rem;
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--review-premium-shortcut-subtitle);
      font-weight: 700;
    }}
    .shortcut-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.55rem;
    }}
    .shortcut-item {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.65rem;
      padding: 0.55rem 0.64rem;
      border-radius: 12px;
      border: 1px solid var(--review-premium-border-soft-2);
      background: var(--review-premium-shortcut-item);
      box-shadow: 0 10px 20px -24px rgba(12, 28, 52, 0.6);
      transition: transform var(--timing-soft), box-shadow var(--timing-soft), border-color var(--timing-soft);
    }}
    .shortcut-item:hover {{
      transform: translateX(2px);
      border-color: rgba(93, 123, 170, 0.45);
      box-shadow: 0 12px 22px -22px rgba(14, 35, 66, 0.54);
    }}
    .shortcut-item span {{
      font-size: 0.76rem;
      color: var(--review-premium-diff-head-text);
      font-weight: 600;
    }}
    .shortcut-keyset {{
      display: inline-flex;
      gap: 0.26rem;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .shortcut-key {{
      min-width: 1.24rem;
      text-align: center;
      padding: 0.12rem 0.36rem;
      border-radius: 6px;
      border: 1px solid var(--review-premium-kbd-border);
      background: var(--review-premium-kbd-bg);
      font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace;
      font-size: 0.67rem;
      font-weight: 700;
      color: var(--review-premium-kbd-text);
      line-height: 1.2;
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
      border-top-right-radius: var(--radius-xl);
      border-bottom-right-radius: var(--radius-lg);
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
      .header-brand {{ display: none; }}
      .run-title {{ max-width: min(48vw, 520px); }}
      .run-title-pills {{ max-width: min(48vw, 520px); }}
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
      .command-bar {{ width: 100%; }}
      .command-group {{ width: 100%; justify-content: flex-start; flex-wrap: wrap; }}
      .command-group--primary,
      .command-group--secondary {{ width: 100%; }}
      .run-title-main {{ max-width: calc(100vw - 88px); }}
      .batch-switcher {{ max-width: calc(100vw - 2rem); }}
      .run-title {{ max-width: calc(100vw - 88px); }}
      .run-context {{ max-width: calc(100vw - 92px); }}
      .run-context {{ flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none; }}
      .run-context::-webkit-scrollbar {{ display: none; }}
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
    .view-mode-segmented {{
        min-height: 34px;
        max-height: 34px;
      }}
      .view-mode-option {{
        min-width: 2.9rem;
        font-size: 0.68rem;
        padding-inline: 0.56rem;
      }}
      .preview-body {{ height: calc(100% - 36px); border-radius: 0 0 14px 14px; }}
      .nav-progress {{ display: none; }}
      .run-context {{ display: none; }}
      .batch-switch-label {{ display: none; }}
      .batch-switch-meta {{ display: none; }}
      .batch-switch-select {{ min-width: 132px; max-width: 52vw; }}
      .batch-switch-go {{ padding-inline: 0.56rem; }}
      .command-group {{ width: 100%; }}
      .shortcut-launch {{ display: none; }}
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
      <span class="header-brand" aria-hidden="true"><span class="brand-mark"></span><span>Review Studio</span></span>
      <button id="btn-nav" class="icon-btn">☰</button>
      <div class="run-title">
        <div class="run-title-main"><strong>Review Run</strong><span class="run-slash">/</span><span id="r-title" class="run-id">...</span></div>
        <div class="run-title-pills">
          <span id="sec-pill" class="sec-pill">sec -</span>
          <span id="nav-progress" class="nav-progress">0/0 visible</span>
          <span id="decision-summary" class="decision-summary">0/0 decided</span>
        </div>
      </div>
      <div class="run-context">
        <span id="run-profile-pill" class="context-pill">Profile</span>
        <span id="run-sections-pill" class="context-pill">Sections</span>
        <span id="run-decision-pill" class="context-pill">0% decided</span>
        <div class="context-progress"><span id="run-progress-fill" class="context-progress-fill"></span></div>
      </div>
    </div>
    <div class="header-right">
      <div class="command-bar">
        <div class="command-group command-group--primary">
          <button id="btn-export" class="primary-btn export-btn">Export Final Doc</button>
          <div id="dl-group" class="actions-group primary-actions"></div>
          <button id="btn-zen" class="primary-btn">Zen Mode</button>
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
    <div id="shortcut-overlay" class="shortcut-overlay" hidden>
      <div class="shortcut-panel" role="dialog" aria-modal="true" aria-labelledby="shortcut-title">
        <div class="shortcut-head">
          <div>
            <h3 id="shortcut-title" class="shortcut-title">Command Shortcuts</h3>
            <div class="shortcut-subtitle">Keyboard-first review workflow</div>
          </div>
          <button id="shortcut-close" class="icon-btn icon-btn-sm" type="button">✕</button>
        </div>
        <div class="shortcut-grid">
          <div class="shortcut-item"><span>Next / Previous section</span><div class="shortcut-keyset"><kbd class="shortcut-key">J</kbd><kbd class="shortcut-key">K</kbd></div></div>
          <div class="shortcut-item"><span>Accept / Reject / Clear</span><div class="shortcut-keyset"><kbd class="shortcut-key">A</kbd><kbd class="shortcut-key">R</kbd><kbd class="shortcut-key">U</kbd></div></div>
          <div class="shortcut-item"><span>Next pending / fmt-only / changed</span><div class="shortcut-keyset"><kbd class="shortcut-key">N</kbd><kbd class="shortcut-key">M</kbd><kbd class="shortcut-key">C</kbd></div></div>
          <div class="shortcut-item"><span>Toggle formatting-only</span><div class="shortcut-keyset"><kbd class="shortcut-key">F</kbd></div></div>
          <div class="shortcut-item"><span>Focus search</span><div class="shortcut-keyset"><kbd class="shortcut-key">/</kbd><kbd class="shortcut-key">Ctrl/Cmd</kbd><kbd class="shortcut-key">K</kbd></div></div>
          <div class="shortcut-item"><span>Go to section number</span><div class="shortcut-keyset"><kbd class="shortcut-key">G</kbd></div></div>
          <div class="shortcut-item"><span>Cycle view mode</span><div class="shortcut-keyset"><kbd class="shortcut-key">S</kbd></div></div>
          <div class="shortcut-item"><span>Toggle navigator</span><div class="shortcut-keyset"><kbd class="shortcut-key">B</kbd></div></div>
          <div class="shortcut-item"><span>Toggle Zen Mode</span><div class="shortcut-keyset"><kbd class="shortcut-key">Z</kbd></div></div>
          <div class="shortcut-item"><span>Undo last decision action</span><div class="shortcut-keyset"><kbd class="shortcut-key">Ctrl/Cmd</kbd><kbd class="shortcut-key">Z</kbd></div></div>
          <div class="shortcut-item"><span>Open / close this palette</span><div class="shortcut-keyset"><kbd class="shortcut-key">?</kbd></div></div>
          <div class="shortcut-item"><span>Close palette / exit input</span><div class="shortcut-keyset"><kbd class="shortcut-key">Esc</kbd></div></div>
        </div>
      </div>
    </div>
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
    const runDecisionPill = D.getElementById("run-decision-pill");
    const runProgressFill = D.getElementById("run-progress-fill");
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
    if (isEditorTheme) {{
      body.classList.add("review-editor-theme");
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
      if (!frame.contentDocument) return;
      const doc = frame.contentDocument;
      const docBody = doc.body;
      if (!doc.head || !docBody) return;

      const existingReader = doc.getElementById(REVIEW_READER_CSS_ID);
      const existingEditor = doc.getElementById(REVIEW_EDITOR_CSS_ID);
      if (existingReader) {{
        existingReader.remove();
      }}
      if (existingEditor) {{
        existingEditor.remove();
      }}

      const shouldUseEditorTheme = isEditorTheme || docBody.classList.contains("preview-theme-editor");
      if (shouldUseEditorTheme) {{
        docBody.classList.add("preview-theme-editor");
        const editorStyle = doc.createElement("style");
        editorStyle.id = REVIEW_EDITOR_CSS_ID;
        editorStyle.textContent = `
          body.preview-theme-editor {{
            --editor-font-size: 0.84rem;
            --editor-line-height: 1.66;
            --editor-row-gap: 0.88rem;
            --editor-space-y: 0.58rem;
            background: #0b1220;
            color: #e5e7eb;
            font-family: "IBM Plex Mono", "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", monospace;
            font-size: var(--editor-font-size);
            line-height: var(--editor-line-height);
            letter-spacing: -0.01em;
          }}
          body.preview-theme-editor main {{
            max-width: min(1100px, 100% - 0.22in);
            margin: 0.95rem auto 1.2rem;
            padding: 0 0.45rem;
          }}
          body.preview-theme-editor .sheet {{
            border-radius: 12px;
            padding: 0.72in 0.68in 0.86in;
            background: #111827;
            border-color: rgba(148, 163, 184, 0.3);
            box-shadow: 0 20px 45px rgba(2, 6, 23, 0.45);
          }}
          body.preview-theme-editor .meta,
          body.preview-theme-editor .meta p {{
            color: #94a3b8;
          }}
          body.preview-theme-editor .meta-title {{
            color: #f1f5f9;
          }}
          body.preview-theme-editor .document {{
            color: #e5e7eb;
            font-size: 11.4pt;
          }}
          body.preview-theme-editor .document h2,
          body.preview-theme-editor .document h3,
          body.preview-theme-editor .document h4 {{
            color: #f8fafc;
          }}
          body.preview-theme-editor .doc-row {{
            margin-bottom: var(--editor-row-gap);
            padding: 0.18rem;
            border-radius: 10px;
            border: 1px solid rgba(148, 163, 184, 0.22);
            background: rgba(15, 23, 42, 0.18);
          }}
          body.preview-theme-editor .doc-row:hover {{
            background: rgba(59, 130, 246, 0.11);
            box-shadow: 0 8px 20px rgba(15, 23, 42, 0.4);
          }}
          body.preview-theme-editor .doc-row.active {{
            background: rgba(59, 130, 246, 0.2) !important;
            box-shadow: 0 0 0 1px rgba(96, 165, 250, 0.45), 0 8px 22px rgba(15, 23, 42, 0.45);
          }}
          body.preview-theme-editor .doc-block {{
            font-family: "IBM Plex Mono", "SFMono-Regular", Menlo, Monaco, Consolas, monospace;
            font-size: 10.9pt;
            margin: 0 0 var(--editor-space-y);
            line-height: 1.6;
          }}
          body.preview-theme-editor .doc-block:last-child {{
            margin-bottom: 0;
          }}
          body.preview-theme-editor .document h2,
          body.preview-theme-editor .document h3,
          body.preview-theme-editor .document h4 {{
            margin: 0 0 var(--editor-space-y);
          }}
          body.preview-theme-editor .view-headers {{
            background: rgba(30, 41, 59, 0.86);
            border-color: rgba(148, 163, 184, 0.32);
            padding-top: 0.9rem;
            padding-bottom: 0.9rem;
            border-bottom-width: 1px;
          }}
          body.preview-theme-editor .pane-original,
          body.preview-theme-editor .pane-redline,
          body.preview-theme-editor .pane-revised {{
            border-radius: 6px;
          }}
          body.preview-theme-editor .pane-redline {{
            background: rgba(59, 130, 246, 0.08);
            border: 1px solid rgba(59, 130, 246, 0.2);
          }}
          body.preview-theme-editor .doc-row.kind-insert .pane-revised {{
            background: rgba(16, 185, 129, 0.14);
            border-color: rgba(16, 185, 129, 0.32);
          }}
          body.preview-theme-editor .doc-row.kind-replace .pane-original {{
            background: rgba(220, 38, 38, 0.1);
            border-color: rgba(220, 38, 38, 0.35);
          }}
          body.preview-theme-editor .doc-row.kind-replace .pane-revised {{
            background: rgba(16, 185, 129, 0.1);
            border-color: rgba(16, 185, 129, 0.35);
          }}
          body.preview-theme-editor .doc-row.kind-delete .pane-original,
          body.preview-theme-editor .doc-row.kind-insert .pane-revised {{
            color: #cbd5e1;
          }}
          body.preview-theme-editor .doc-row.kind-delete .pane-original {{
            text-decoration: line-through;
            text-decoration-color: #f43f5e;
            opacity: 0.7;
          }}
          body.preview-theme-editor .ins {{
            color: #22c55e;
            text-decoration-thickness: 2.5px;
            text-decoration-color: #22c55e;
            text-underline-offset: 0.1em;
          }}
          body.preview-theme-editor .del {{
            color: #fb7185;
            text-decoration-thickness: 2px;
            text-decoration-color: #fb7185;
            text-underline-offset: 0.04em;
          }}
          body.preview-theme-editor code,
          body.preview-theme-editor pre,
          body.preview-theme-editor pre code {{
            background: rgba(15, 23, 42, 0.78);
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-radius: 8px;
          }}
          body.preview-theme-editor.view-split .doc-row,
          body.preview-theme-editor.view-tri .doc-row {{
            gap: 0.95rem;
          }}
          body.preview-theme-editor.view-split .doc-row.active,
          body.preview-theme-editor.view-tri .doc-row.active {{
            box-shadow: 0 0 0 2px rgba(96, 165, 250, 0.5), 0 10px 24px rgba(15, 23, 42, 0.6);
          }}
        `;
        doc.head.appendChild(editorStyle);
        return;
      }}

      if (!docBody.classList.contains("preview-theme-reader")) return;

      const style = doc.createElement("style");
      style.id = REVIEW_READER_CSS_ID;
      style.textContent = `
        body.preview-theme-reader {{
          --reader-font-size: 1.08rem;
          --reader-line-height: 1.78;
          --reader-row-gap: 1rem;
          --reader-space-y: 0.72rem;
          --reader-heading-size-1: 1.74rem;
          --reader-heading-size-2: 1.34rem;
          --reader-heading-size-3: 1.16rem;
          background: #dce7f7;
          color: var(--text);
          font-family: "Source Serif 4", "Georgia", "Times New Roman", serif;
          font-size: var(--reader-font-size);
          line-height: var(--reader-line-height);
          letter-spacing: 0.005em;
        }}
        body.preview-theme-reader main {{
          max-width: min(76ch, 100% - 1rem);
          margin: 1.35rem auto 2rem;
          padding: 0;
        }}
        body.preview-theme-reader .sheet {{
          border-radius: 14px;
          padding: 1rem 1.15rem 1.2rem;
          background: #ffffff;
          box-shadow: 0 16px 36px rgba(15, 23, 42, 0.12);
        }}
        body.preview-theme-reader .meta {{
          margin-bottom: 1.05rem;
        }}
        body.preview-theme-reader .meta-title {{
          margin: 0 0 0.34rem;
          font-size: 1.15rem;
          letter-spacing: 0.01em;
        }}
        body.preview-theme-reader .document {{
          font-size: 1rem;
          line-height: var(--reader-line-height);
        }}
        body.preview-theme-reader .doc-row {{
          margin-bottom: var(--reader-row-gap);
          padding: 0.5rem 0.55rem;
          border-radius: 12px;
          border: 1px solid rgba(138, 155, 178, 0.22);
          background: rgba(255, 255, 255, 0.96);
          box-shadow: 0 2px 10px rgba(15, 23, 42, 0.08);
        }}
        body.preview-theme-reader .doc-row:hover {{
          background: rgba(30, 58, 138, 0.035);
          box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
        }}
        body.preview-theme-reader .doc-row.active {{
          background: rgba(30, 58, 138, 0.055);
          box-shadow: 0 0 0 2px rgba(30, 58, 138, 0.2), 0 12px 24px rgba(30, 58, 138, 0.16);
        }}
        body.preview-theme-reader .doc-block {{
          margin: 0 0 var(--reader-space-y);
          line-height: 1.72;
        }}
        body.preview-theme-reader .doc-block:last-child {{
          margin-bottom: 0;
        }}
        body.preview-theme-reader .document h2,
        body.preview-theme-reader .document h3,
        body.preview-theme-reader .document h4 {{
          color: var(--text);
          margin: 0 0 var(--reader-space-y);
        }}
        body.preview-theme-reader .document h2 {{
          font-size: var(--reader-heading-size-1);
          font-weight: 600;
        }}
        body.preview-theme-reader .document h3 {{
          font-size: var(--reader-heading-size-2);
        }}
        body.preview-theme-reader .document h4 {{
          font-size: var(--reader-heading-size-3);
        }}
        body.preview-theme-reader .ins {{
          text-decoration-thickness: 2.5px;
          text-underline-offset: 0.1em;
        }}
        body.preview-theme-reader .del {{
          text-decoration-thickness: 2px;
          text-underline-offset: 0.04em;
        }}
        body.preview-theme-reader.view-split .doc-row,
        body.preview-theme-reader.view-tri .doc-row {{
          gap: 1.2rem;
        }}
      `;
      doc.head.appendChild(style);
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
    function z() {{ s.zen = !s.zen; body.className = s.zen ? "zen-mode" : (s.navOff ? "nav-hidden" : ""); if(s.zen) insp.classList.remove("visible"); else if(s.insp) insp.classList.add("visible"); }}
    function n() {{ if(s.zen) z(); s.navOff = !s.navOff; body.classList.toggle("nav-hidden", s.navOff); }}
    
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
    D.getElementById("close-insp").onclick = () => {{ s.insp = false; insp.classList.remove("visible"); }};
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
      const pct = decisionCounts.any ? Math.round((decided / decisionCounts.any) * 100) : 0;
      if (runDecisionPill) {{
        runDecisionPill.textContent = `${{pct}}% decided`;
      }}
      if (runProgressFill) {{
        runProgressFill.style.width = `${{pct}}%`;
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
        decisionSummary.textContent = "0/0 decided";
        if (runDecisionPill) runDecisionPill.textContent = "0% decided";
        if (runProgressFill) runProgressFill.style.width = "0%";
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
      if (runProfilePill) {{
        const profileName = String(m.profile_name || "default").replace(/[_-]+/g, " ");
        runProfilePill.textContent = `Profile ${{profileName}}`;
      }}
      if (runSectionsPill) {{
        const sectionCount = Array.isArray(m.sections) ? m.sections.length : 0;
        runSectionsPill.textContent = `${{sectionCount}} sections`;
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
