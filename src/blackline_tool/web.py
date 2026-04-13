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
        if parsed.path == "/api/compare":
            self._handle_compare(handler)
            return
        _send_error(handler, HTTPStatus.NOT_FOUND, "Not found")

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
  <title>Blackline Review Studio</title>
  <style>
    :root {{
      --ink: #142235;
      --ink-soft: #20324b;
      --muted: #5f6f85;
      --muted-strong: #415066;
      --line: #d5dee8;
      --line-strong: #b9c7d7;
      --panel: rgba(255, 255, 255, 0.92);
      --panel-soft: rgba(247, 250, 253, 0.86);
      --navy: #163866;
      --navy-deep: #0d2548;
      --teal: #0f6b62;
      --gold: #a67c3b;
      --red: #b42318;
      --nav-width: 320px;
      --tray-height: 250px;
      --canvas: #eef2f7;
      --shadow: 0 30px 60px rgba(20, 34, 53, 0.12);
      --shadow-soft: 0 12px 28px rgba(20, 34, 53, 0.08);
      --ui: "Aptos", "Inter", "SF Pro Text", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      --serif: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Palatino, Georgia, serif;
    }}
    * {{ box-sizing: border-box; }}
    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(16px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15, 107, 98, 0.08), transparent 24%),
        radial-gradient(circle at 84% 11%, rgba(22, 56, 102, 0.1), transparent 24%),
        linear-gradient(180deg, #f7f9fc 0%, #eef2f7 58%, #e8edf4 100%);
      font-family: var(--ui);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(255,255,255,0.16) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.16) 1px, transparent 1px);
      background-size: 64px 64px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.42), transparent 88%);
    }}
    .page {{
      max-width: 1360px;
      margin: 0 auto;
      padding: 1.2rem 1.1rem 2.4rem;
    }}
    .masthead {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      margin-bottom: 1rem;
      padding: 0.95rem 1.05rem;
      border: 1px solid rgba(185, 199, 215, 0.8);
      border-radius: 24px;
      background: rgba(255,255,255,0.68);
      backdrop-filter: blur(18px);
      box-shadow: var(--shadow-soft);
      animation: rise 360ms ease both;
    }}
    .masthead-brand {{
      display: flex;
      align-items: center;
      gap: 0.85rem;
    }}
    .brand-mark {{
      width: 2.5rem;
      height: 2.5rem;
      border-radius: 0.95rem;
      background: linear-gradient(135deg, var(--navy-deep), var(--navy));
      position: relative;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.18);
    }}
    .brand-mark::before,
    .brand-mark::after {{
      content: "";
      position: absolute;
      inset: 0.56rem;
      border: 1.5px solid rgba(255,255,255,0.72);
      border-top: 0;
      border-left: 0;
      border-radius: 0.3rem;
      transform: skewX(-8deg);
    }}
    .brand-mark::after {{
      inset: 0.82rem;
      opacity: 0.5;
    }}
    .brand-copy strong {{
      display: block;
      color: var(--navy-deep);
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-size: 0.72rem;
    }}
    .brand-copy span {{
      color: var(--muted);
      font-size: 0.93rem;
    }}
    .masthead-note {{
      display: inline-flex;
      align-items: center;
      gap: 0.55rem;
      padding: 0.48rem 0.72rem;
      border-radius: 999px;
      background: rgba(255,255,255,0.76);
      border: 1px solid rgba(185, 199, 215, 0.72);
      color: var(--muted-strong);
      font-size: 0.86rem;
    }}
    .masthead-note::before {{
      content: "";
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--teal), var(--navy));
      box-shadow: 0 0 0 0.18rem rgba(15, 107, 98, 0.1);
    }}
    .workspace {{
      display: grid;
      grid-template-columns: minmax(0, 1.18fr) minmax(350px, 0.82fr);
      gap: 1rem;
      align-items: start;
    }}
    .panel {{
      border: 1px solid rgba(185, 199, 215, 0.8);
      border-radius: 32px;
      background: linear-gradient(180deg, var(--panel), var(--panel-soft));
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      animation: rise 460ms ease both;
    }}
    .intake {{
      padding: 1.4rem;
      display: grid;
      gap: 1.25rem;
    }}
    .hero-copy {{
      display: grid;
      gap: 0.95rem;
      padding: 0.2rem 0.2rem 0;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 0.52rem;
      margin: 0;
      padding: 0.42rem 0.72rem;
      border-radius: 999px;
      background: rgba(22, 56, 102, 0.08);
      color: var(--navy);
      text-transform: uppercase;
      letter-spacing: 0.14em;
      font-size: 0.73rem;
      font-weight: 700;
      width: fit-content;
    }}
    .eyebrow::before {{
      content: "";
      width: 0.46rem;
      height: 0.46rem;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--teal), var(--navy));
    }}
    h1 {{
      margin: 0;
      font-family: var(--serif);
      font-size: clamp(2.05rem, 3.2vw, 3.55rem);
      line-height: 0.96;
      letter-spacing: -0.03em;
      max-width: 12ch;
      color: var(--navy-deep);
    }}
    .deck {{
      margin: 0;
      max-width: 56rem;
      color: var(--muted);
      font-size: 0.98rem;
      line-height: 1.66;
    }}
    .utility-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
    }}
    .utility-chip {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.58rem 0.8rem;
      border-radius: 999px;
      background: rgba(255,255,255,0.8);
      border: 1px solid rgba(185, 199, 215, 0.76);
      color: var(--muted-strong);
      font-size: 0.87rem;
      box-shadow: var(--shadow-soft);
    }}
    .utility-chip strong {{
      color: var(--ink);
    }}
    .intake-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 1rem;
      align-items: start;
    }}
    .form-card {{
      padding: 1.2rem;
      border-radius: 28px;
      border: 1px solid rgba(185, 199, 215, 0.76);
      background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(246,249,252,0.86));
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.78);
    }}
    .form-head {{
      display: grid;
      gap: 0.45rem;
      margin-bottom: 1rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid rgba(185, 199, 215, 0.62);
    }}
    .form-head small,
    .section-title,
    .field label {{
      font-size: 0.75rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--navy);
      font-weight: 700;
    }}
    .form-head h2 {{
      margin: 0;
      font-size: 1.34rem;
      color: var(--ink);
    }}
    .form-head p,
    .helper {{
      margin: 0;
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.58;
    }}
    form {{
      display: grid;
      gap: 1rem;
    }}
    .upload-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.9rem;
    }}
    .upload-input {{
      position: absolute;
      inset: 0;
      opacity: 0;
      pointer-events: none;
    }}
    .upload-card {{
      position: relative;
      display: grid;
      gap: 0.82rem;
      min-height: 196px;
      padding: 1.05rem;
      border-radius: 24px;
      border: 1px solid rgba(185, 199, 215, 0.76);
      background:
        radial-gradient(circle at top right, rgba(22, 56, 102, 0.08), transparent 40%),
        linear-gradient(180deg, rgba(255,255,255,0.94), rgba(244,248,252,0.82));
      box-shadow: var(--shadow-soft);
      cursor: pointer;
      transition: transform 140ms ease, box-shadow 140ms ease, border-color 140ms ease;
    }}
    .upload-card:hover {{
      transform: translateY(-2px);
      border-color: rgba(22, 56, 102, 0.34);
      box-shadow: 0 18px 34px rgba(20, 34, 53, 0.1);
    }}
    .upload-card.has-file {{
      border-color: rgba(15, 107, 98, 0.34);
      background:
        radial-gradient(circle at top right, rgba(15, 107, 98, 0.1), transparent 42%),
        linear-gradient(180deg, rgba(255,255,255,0.96), rgba(241,251,248,0.9));
    }}
    .upload-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 0.8rem;
    }}
    .upload-icon {{
      width: 2.8rem;
      height: 2.8rem;
      border-radius: 0.95rem;
      display: grid;
      place-items: center;
      background: linear-gradient(135deg, rgba(22, 56, 102, 0.12), rgba(15, 107, 98, 0.12));
      color: var(--navy-deep);
      font-size: 1.06rem;
      font-weight: 700;
    }}
    .upload-badge {{
      padding: 0.34rem 0.56rem;
      border-radius: 999px;
      background: rgba(22, 56, 102, 0.08);
      color: var(--navy);
      font-size: 0.7rem;
      letter-spacing: 0.13em;
      text-transform: uppercase;
      font-weight: 700;
    }}
    .upload-title {{
      margin: 0;
      color: var(--ink);
      font-size: 1rem;
      font-weight: 700;
    }}
    .upload-copy {{
      margin: 0.28rem 0 0;
      color: var(--muted);
      font-size: 0.9rem;
      line-height: 1.55;
    }}
    .upload-meta {{
      margin-top: auto;
      display: grid;
      gap: 0.42rem;
      padding-top: 0.2rem;
      border-top: 1px solid rgba(185, 199, 215, 0.52);
    }}
    .upload-file-name {{
      font-size: 0.93rem;
      font-weight: 700;
      color: var(--ink-soft);
      word-break: break-word;
    }}
    .upload-file-state {{
      font-size: 0.82rem;
      color: var(--muted);
      line-height: 1.45;
    }}
    .field {{
      display: grid;
      gap: 0.48rem;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.9rem;
    }}
    input[type="text"],
    select {{
      width: 100%;
      border-radius: 18px;
      border: 1px solid rgba(185, 199, 215, 0.88);
      background: rgba(255,255,255,0.96);
      padding: 0.94rem 1rem;
      font-size: 0.97rem;
      color: var(--ink);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.82);
    }}
    input[type="text"]:focus,
    select:focus {{
      outline: 2px solid rgba(22, 56, 102, 0.14);
      border-color: rgba(22, 56, 102, 0.5);
    }}
    .pill-row,
    .toggle-grid {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.58rem;
    }}
    .check-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.48rem;
      padding: 0.62rem 0.86rem;
      border-radius: 999px;
      border: 1px solid rgba(185, 199, 215, 0.78);
      background: rgba(255,255,255,0.88);
      box-shadow: var(--shadow-soft);
      font-size: 0.89rem;
      color: var(--ink);
      transition: transform 140ms ease, border-color 140ms ease, background 140ms ease;
    }}
    .check-pill:hover {{
      transform: translateY(-1px);
      border-color: rgba(22, 56, 102, 0.34);
      background: rgba(255,255,255,0.96);
    }}
    .check-pill input {{
      accent-color: var(--navy);
    }}
    details {{
      border: 1px solid rgba(185, 199, 215, 0.78);
      border-radius: 22px;
      background: rgba(249, 251, 253, 0.84);
      padding: 0.9rem 0.95rem 0.95rem;
    }}
    details summary {{
      cursor: pointer;
      list-style: none;
      font-weight: 700;
      color: var(--navy-deep);
    }}
    details summary::-webkit-details-marker {{
      display: none;
    }}
    .toggle-grid {{
      margin-top: 0.85rem;
    }}
    .actions {{
      display: flex;
      align-items: center;
      gap: 1rem;
      margin-top: 0.25rem;
      flex-wrap: wrap;
    }}
    button {{
      appearance: none;
      border: 0;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--navy-deep), var(--navy));
      color: white;
      padding: 0.98rem 1.46rem;
      font-size: 0.97rem;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 18px 34px rgba(22, 56, 102, 0.22);
      transition: transform 150ms ease, box-shadow 150ms ease;
    }}
    button:hover {{
      transform: translateY(-1px);
      box-shadow: 0 22px 40px rgba(22, 56, 102, 0.24);
    }}
    button[disabled] {{
      cursor: wait;
      opacity: 0.74;
      transform: none;
    }}
    .status {{
      min-height: 1.2rem;
      max-width: 24rem;
      color: var(--muted);
      font-size: 0.93rem;
      line-height: 1.48;
    }}
    .status.error {{
      color: var(--red);
    }}
    .briefing {{
      padding: 1.2rem;
      display: grid;
      gap: 0.9rem;
      position: sticky;
      top: 1rem;
    }}
    .brief-card {{
      padding: 1rem;
      border-radius: 24px;
      border: 1px solid rgba(185, 199, 215, 0.76);
      background: rgba(255,255,255,0.74);
      box-shadow: var(--shadow-soft);
    }}
    .brief-card h3 {{
      margin: 0 0 0.4rem;
      font-size: 1rem;
      color: var(--navy-deep);
    }}
    .brief-card p {{
      margin: 0;
      color: var(--muted);
      font-size: 0.91rem;
      line-height: 1.55;
    }}
    .brief-kicker {{
      margin: 0 0 0.62rem;
      color: var(--navy);
      font-size: 0.73rem;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }}
    .artifact-list,
    .profile-list {{
      display: grid;
      gap: 0.72rem;
      margin-top: 0.76rem;
    }}
    .artifact {{
      display: grid;
      gap: 0.2rem;
      padding: 0.78rem 0.85rem;
      border-radius: 18px;
      background: rgba(247, 250, 253, 0.86);
      border: 1px solid rgba(185, 199, 215, 0.64);
    }}
    .artifact strong {{
      font-size: 0.9rem;
      color: var(--ink);
    }}
    .artifact span {{
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.45;
    }}
    .profile-list div {{
      padding-left: 0.95rem;
      position: relative;
      color: var(--muted);
      font-size: 0.87rem;
      line-height: 1.5;
    }}
    .profile-list div::before {{
      content: "";
      position: absolute;
      left: 0;
      top: 0.52rem;
      width: 0.42rem;
      height: 0.42rem;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--gold), var(--navy));
    }}
    .checklist {{
      display: grid;
      gap: 0.6rem;
      margin-top: 0.76rem;
    }}
    .checklist div {{
      display: flex;
      gap: 0.65rem;
      color: var(--muted);
      font-size: 0.87rem;
      line-height: 1.48;
    }}
    .checklist div::before {{
      content: "•";
      color: var(--teal);
      font-weight: 700;
    }}
    @media (max-width: 1120px) {{
      .workspace,
      .intake-grid {{
        grid-template-columns: 1fr;
      }}
      .briefing {{
        position: static;
      }}
    }}
    @media (max-width: 760px) {{
      .masthead {{
        flex-direction: column;
        align-items: flex-start;
      }}
      .upload-grid,
      .grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="masthead">
      <div class="masthead-brand">
        <span class="brand-mark" aria-hidden="true"></span>
        <div class="brand-copy">
          <strong>Blackline Studio</strong>
          <span>Native DOCX legal redlines with a practical local review workspace</span>
        </div>
      </div>
      <div class="masthead-note">Private by default. Runs and exports stay on this machine.</div>
    </header>
    <main class="workspace">
      <section class="panel intake">
        <div class="hero-copy">
          <p class="eyebrow">Matter Intake</p>
          <h1>Build a legal blackline without leaving the browser.</h1>
          <p class="deck">
            Start with the original and revised drafts, choose the comparison profile, and generate a review run with downloadable
            DOCX, PDF, HTML, and JSON outputs. The layout is tuned for lawyers who need clarity first: matter intake on the left,
            artifact expectations on the right, and no noisy setup steps.
          </p>
          <div class="utility-row">
            <div class="utility-chip"><strong>Word-native</strong> tracked changes and preserved structure</div>
            <div class="utility-chip"><strong>Review-ready</strong> browser preview and change navigator</div>
            <div class="utility-chip"><strong>Shared engine</strong> same outputs as the CLI</div>
          </div>
        </div>
        <div class="intake-grid">
          <div class="form-card">
            <div class="form-head">
              <small>Start a run</small>
              <h2>Compare two drafts and open the review workspace</h2>
              <p>Use DOCX for the most faithful legal blackline. TXT remains available for quick smoke tests and lightweight drafts.</p>
            </div>
            <form id="compare-form">
              <div class="field">
                <div class="section-title">Inputs</div>
                <div class="upload-grid">
                  <label class="upload-card" for="original">
                    <input class="upload-input" id="original" name="original" type="file" required />
                    <div class="upload-header">
                      <div class="upload-icon">O</div>
                      <div class="upload-badge">Baseline</div>
                    </div>
                    <div>
                      <p class="upload-title">Original draft</p>
                      <p class="upload-copy">The source document whose structure and layout the blackline should follow.</p>
                    </div>
                    <div class="upload-meta">
                      <div class="upload-file-name" data-file-name="original">No file selected</div>
                      <div class="upload-file-state" data-file-state="original">Accepted: `.docx`, `.txt`</div>
                    </div>
                  </label>
                  <label class="upload-card" for="revised">
                    <input class="upload-input" id="revised" name="revised" type="file" required />
                    <div class="upload-header">
                      <div class="upload-icon">R</div>
                      <div class="upload-badge">Latest</div>
                    </div>
                    <div>
                      <p class="upload-title">Revised draft</p>
                      <p class="upload-copy">The newer version to overlay onto the original with tracked changes and change review.</p>
                    </div>
                    <div class="upload-meta">
                      <div class="upload-file-name" data-file-name="revised">No file selected</div>
                      <div class="upload-file-state" data-file-state="revised">Accepted: `.docx`, `.txt`</div>
                    </div>
                  </label>
                </div>
              </div>

              <div class="grid">
                <div class="field">
                  <label for="profile">Comparison Profile</label>
                  <select id="profile" name="profile">{profile_options}</select>
                  <p class="helper">Use matter-specific rules to suppress noise and tune legal alignment.</p>
                </div>
                <div class="field">
                  <label for="base-name">Base Output Name</label>
                  <input id="base-name" name="base_name" type="text" value="blackline_report" />
                  <p class="helper">This name is used for the generated DOCX, PDF, HTML, and JSON artifacts.</p>
                </div>
              </div>

              <div class="field">
                <div class="section-title">Output Formats</div>
                <div class="pill-row">{format_controls}</div>
              </div>

              <details>
                <summary>Advanced rules</summary>
                <div class="toggle-grid">
                  <label class="check-pill"><input type="checkbox" name="strict_legal" /> <span>Strict legal alias</span></label>
                  <label class="check-pill"><input type="checkbox" name="ignore_case" /> <span>Ignore case</span></label>
                  <label class="check-pill"><input type="checkbox" name="ignore_whitespace" /> <span>Ignore whitespace</span></label>
                  <label class="check-pill"><input type="checkbox" name="ignore_smart_punctuation" /> <span>Normalize smart punctuation</span></label>
                  <label class="check-pill"><input type="checkbox" name="ignore_punctuation" /> <span>Ignore punctuation</span></label>
                  <label class="check-pill"><input type="checkbox" name="ignore_numbering" /> <span>Ignore numbering</span></label>
                  <label class="check-pill"><input type="checkbox" name="detect_moves" checked /> <span>Detect moves</span></label>
                </div>
              </details>

              <div class="actions">
                <button id="submit-button" type="submit">Generate Review Run</button>
                <div id="status" class="status"></div>
              </div>
            </form>
          </div>
          <aside class="briefing">
            <section class="brief-card">
              <p class="brief-kicker">Output package</p>
              <h3>One run, all review artifacts</h3>
              <p>Every compare run keeps its preview and exports together so you can reopen it later without rerunning the comparison.</p>
              <div class="artifact-list">
                <div class="artifact">
                  <strong>DOCX and PDF</strong>
                  <span>Formal blackline outputs for circulation, filing, and record keeping.</span>
                </div>
                <div class="artifact">
                  <strong>HTML preview</strong>
                  <span>Fast browser review with the same redline content that powers the export set.</span>
                </div>
                <div class="artifact">
                  <strong>JSON report</strong>
                  <span>Structured changes for QA, automation, or downstream analysis.</span>
                </div>
              </div>
            </section>
            <section class="brief-card">
              <p class="brief-kicker">Profiles</p>
              <h3>Use the profile that matches the matter</h3>
              <div class="profile-list">
                <div><strong>Contract</strong> keeps numbering and defined-term handling conservative.</div>
                <div><strong>Litigation</strong> is better when headings, citations, and argument moves matter.</div>
                <div><strong>Factum and presentation</strong> reduce formatting noise when structure is stable but prose is moving.</div>
              </div>
            </section>
            <section class="brief-card">
              <p class="brief-kicker">Best results</p>
              <h3>Practical guidance before you run</h3>
              <div class="checklist">
                <div>Use DOCX pairs when you want native tracked changes, preserved tables, and higher-fidelity structure.</div>
                <div>Keep the original file as the baseline if you want the output to retain its styles and layout assumptions.</div>
                <div>Leave move detection on for legal drafts unless you are intentionally reviewing every relocation as a delete and insert.</div>
              </div>
            </section>
          </aside>
        </div>
      </section>
    </main>
  </div>
  <script>
    const form = document.getElementById("compare-form");
    const statusNode = document.getElementById("status");
    const submitButton = document.getElementById("submit-button");
    const fileInputs = Array.from(document.querySelectorAll(".upload-input"));

    async function fileToBase64(file) {{
      const buffer = await file.arrayBuffer();
      let binary = "";
      const bytes = new Uint8Array(buffer);
      const chunk = 0x8000;
      for (let index = 0; index < bytes.length; index += chunk) {{
        binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
      }}
      return btoa(binary);
    }}

    function selectedFormats(formData) {{
      return formData.getAll("formats");
    }}

    function syncUploadCard(input) {{
      const card = input.closest(".upload-card");
      const label = card?.querySelector("[data-file-name]");
      const meta = card?.querySelector("[data-file-state]");
      if (!card || !label || !meta) {{
        return;
      }}
      const file = input.files && input.files[0];
      if (file) {{
        card.classList.add("has-file");
        label.textContent = file.name;
        const size = file.size > 1024 * 1024
          ? (file.size / (1024 * 1024)).toFixed(1) + " MB"
          : Math.max(1, Math.round(file.size / 1024)) + " KB";
        meta.textContent = "Ready for compare • " + size;
      }} else {{
        card.classList.remove("has-file");
        label.textContent = "No file selected";
        meta.textContent = "Accepted: `.docx`, `.txt`";
      }}
    }}

    for (const input of fileInputs) {{
      input.addEventListener("change", () => syncUploadCard(input));
      syncUploadCard(input);
    }}

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      statusNode.className = "status";
      statusNode.textContent = "Packaging files and generating outputs...";
      submitButton.disabled = true;

      try {{
        const formData = new FormData(form);
        const originalFile = formData.get("original");
        const revisedFile = formData.get("revised");
        if (!(originalFile instanceof File) || !(revisedFile instanceof File) || !originalFile.name || !revisedFile.name) {{
          throw new Error("Select both files before starting the compare run.");
        }}
        const formats = selectedFormats(formData);
        if (!formats.length) {{
          throw new Error("Select at least one output format.");
        }}

        const payload = {{
          original_name: originalFile.name,
          original_content: await fileToBase64(originalFile),
          revised_name: revisedFile.name,
          revised_content: await fileToBase64(revisedFile),
          base_name: formData.get("base_name") || "blackline_report",
          profile: formData.get("profile") || "default",
          formats,
          strict_legal: formData.get("strict_legal") === "on",
          ignore_case: formData.get("ignore_case") === "on",
          ignore_whitespace: formData.get("ignore_whitespace") === "on",
          ignore_smart_punctuation: formData.get("ignore_smart_punctuation") === "on",
          ignore_punctuation: formData.get("ignore_punctuation") === "on",
          ignore_numbering: formData.get("ignore_numbering") === "on",
          detect_moves: formData.get("detect_moves") === "on",
        }};

        const response = await fetch("/api/compare", {{
          method: "POST",
          headers: {{
            "Content-Type": "application/json",
          }},
          body: JSON.stringify(payload),
        }});
        const result = await response.json();
        if (!response.ok) {{
          throw new Error(result.error || "Compare run failed.");
        }}
        window.location.assign(result.run_url);
      }} catch (error) {{
        statusNode.className = "status error";
        statusNode.textContent = error.message || String(error);
        submitButton.disabled = false;
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
    :root {{
      --ink: #142235;
      --ink-soft: #20324b;
      --muted: #607189;
      --muted-strong: #46566d;
      --line: rgba(185, 199, 215, 0.78);
      --panel: rgba(255, 255, 255, 0.92);
      --panel-soft: rgba(247, 250, 253, 0.84);
      --navy: #163866;
      --navy-deep: #0d2548;
      --teal: #0f6b62;
      --gold: #a67c3b;
      --red: #b42318;
      --shadow: 0 28px 58px rgba(20, 34, 53, 0.12);
      --shadow-soft: 0 12px 28px rgba(20, 34, 53, 0.08);
      --ui: "Aptos", "Inter", "SF Pro Text", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      --serif: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Palatino, Georgia, serif;
    }}
    * {{ box-sizing: border-box; }}
    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(14px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        radial-gradient(circle at 10% 10%, rgba(15, 107, 98, 0.08), transparent 22%),
        radial-gradient(circle at 86% 12%, rgba(22, 56, 102, 0.1), transparent 24%),
        linear-gradient(180deg, #f7f9fc 0%, #eef2f7 58%, #e7edf4 100%);
      font-family: var(--ui);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(255,255,255,0.18) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.18) 1px, transparent 1px);
      background-size: 70px 70px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.42), transparent 88%);
    }}
    body.resizing {{
      user-select: none;
      cursor: col-resize;
    }}
    body.resizing.row {{
      cursor: row-resize;
    }}
    .shell {{
      max-width: 1500px;
      margin: 0 auto;
      min-height: 100vh;
      padding: 0.9rem;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 0.9rem;
    }}
    .command-bar {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(420px, auto);
      gap: 1rem;
      align-items: start;
      padding: 0.92rem 1rem;
      border: 1px solid var(--line);
      border-radius: 24px;
      background: linear-gradient(180deg, rgba(255,255,255,0.92), rgba(247,250,253,0.84));
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      animation: rise 360ms ease both;
    }}
    .command-copy {{
      display: grid;
      gap: 0.45rem;
      align-content: start;
    }}
    .command-copy small {{
      font-size: 0.74rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--navy);
      font-weight: 700;
    }}
    .command-copy h1 {{
      margin: 0;
      font-family: var(--serif);
      font-size: clamp(1.5rem, 2vw, 2.15rem);
      line-height: 1.02;
      letter-spacing: -0.03em;
      color: var(--navy-deep);
    }}
    .command-copy p {{
      margin: 0;
      max-width: 52rem;
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.52;
    }}
    .command-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.52rem;
      margin-top: 0.2rem;
    }}
    .command-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.48rem 0.72rem;
      border-radius: 999px;
      background: rgba(255,255,255,0.84);
      border: 1px solid rgba(185, 199, 215, 0.74);
      color: var(--muted-strong);
      font-size: 0.83rem;
      box-shadow: var(--shadow-soft);
    }}
    .command-pill strong {{
      color: var(--ink);
    }}
    .command-side {{
      display: grid;
      gap: 0.62rem;
      justify-items: end;
    }}
    .summary-shell,
    .download-shell,
    .navigator-section {{
      padding: 0.84rem;
      border-radius: 20px;
      border: 1px solid rgba(185, 199, 215, 0.74);
      background: rgba(255,255,255,0.74);
      box-shadow: var(--shadow-soft);
    }}
    .shell-title,
    .sidebar-title {{
      margin: 0 0 0.68rem;
      font-size: 0.74rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--navy);
      font-weight: 700;
    }}
    .summary-stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.46rem;
    }}
    .stat {{
      display: inline-grid;
      gap: 0.06rem;
      min-width: 76px;
      border: 1px solid rgba(185, 199, 215, 0.76);
      border-radius: 16px;
      background: rgba(248,251,253,0.92);
      padding: 0.56rem 0.68rem;
    }}
    .stat strong {{
      display: block;
      color: var(--navy-deep);
      font-size: 1rem;
    }}
    .stat span {{
      color: var(--muted);
      font-size: 0.71rem;
      text-transform: uppercase;
      letter-spacing: 0.11em;
    }}
    .download-list,
    .filter-row,
    .view-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
    }}
    .chip,
    .filter-chip,
    .toolbar-button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.36rem;
      padding: 0.62rem 0.88rem;
      border-radius: 999px;
      border: 1px solid rgba(185, 199, 215, 0.8);
      background: rgba(255,255,255,0.88);
      color: var(--ink);
      text-decoration: none;
      font-size: 0.86rem;
      font-weight: 700;
      box-shadow: var(--shadow-soft);
      transition: transform 140ms ease, border-color 140ms ease, background 140ms ease;
      appearance: none;
      cursor: pointer;
    }}
    .chip:hover,
    .filter-chip:hover,
    .toolbar-button:hover {{
      transform: translateY(-1px);
      border-color: rgba(22, 56, 102, 0.38);
      background: rgba(255,255,255,0.96);
    }}
    .filter-chip.active {{
      background: linear-gradient(135deg, var(--navy-deep), var(--navy));
      color: white;
      border-color: transparent;
      box-shadow: 0 16px 30px rgba(22, 56, 102, 0.22);
    }}
    .toolbar-button.primary {{
      background: linear-gradient(135deg, var(--navy-deep), var(--navy));
      color: white;
      border-color: transparent;
      box-shadow: 0 16px 30px rgba(22, 56, 102, 0.22);
    }}
    .toolbar-button[disabled] {{
      opacity: 0.5;
      cursor: default;
      transform: none;
    }}
    .workspace {{
      display: grid;
      grid-template-columns: minmax(0, var(--nav-width)) 10px minmax(0, 1fr);
      gap: 0.9rem;
      min-height: 0;
    }}
    .navigator {{
      display: grid;
      grid-template-rows: auto auto auto minmax(0, 1fr) auto auto;
      gap: 0.82rem;
      padding: 1rem;
      border: 1px solid var(--line);
      border-radius: 24px;
      background: linear-gradient(180deg, rgba(255,255,255,0.9), rgba(247,250,253,0.82));
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      animation: rise 420ms ease both;
      overflow: hidden;
    }}
    .navigator-kicker {{
      display: grid;
      gap: 0.24rem;
      padding-bottom: 0.12rem;
      border-bottom: 1px solid rgba(185, 199, 215, 0.54);
    }}
    .navigator-kicker small {{
      font-size: 0.73rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--navy);
      font-weight: 700;
    }}
    .navigator-kicker strong {{
      color: var(--navy-deep);
      font-size: 0.98rem;
    }}
    .navigator-kicker span {{
      color: var(--muted);
      font-size: 0.85rem;
      line-height: 1.45;
    }}
    .search {{
      width: 100%;
      border-radius: 16px;
      border: 1px solid rgba(185, 199, 215, 0.82);
      padding: 0.88rem 0.96rem;
      background: rgba(255,255,255,0.94);
      font-size: 0.93rem;
      color: var(--ink);
    }}
    .search:focus {{
      outline: 2px solid rgba(22, 56, 102, 0.14);
      border-color: rgba(22, 56, 102, 0.48);
    }}
    .list-section {{
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      min-height: 0;
    }}
    .detail-list {{
      overflow: auto;
      min-height: 0;
      padding-right: 0.1rem;
    }}
    .sidebar-footnote {{
      color: var(--muted);
      font-size: 0.82rem;
      line-height: 1.45;
      padding: 0 0.15rem;
    }}
    .resize-handle {{
      position: relative;
      border-radius: 999px;
      background: linear-gradient(180deg, rgba(185, 199, 215, 0.16), rgba(185, 199, 215, 0.6), rgba(185, 199, 215, 0.16));
    }}
    .resize-handle::before {{
      content: "";
      position: absolute;
      inset: 50%;
      transform: translate(-50%, -50%);
      border-radius: 999px;
      background: rgba(22, 56, 102, 0.26);
      box-shadow: 0 0 0 0.24rem rgba(22, 56, 102, 0.08);
    }}
    .resize-handle:hover::before {{
      background: rgba(22, 56, 102, 0.42);
    }}
    .resize-handle.vertical {{
      width: 10px;
      cursor: col-resize;
      align-self: stretch;
    }}
    .resize-handle.vertical::before {{
      width: 4px;
      height: 84px;
    }}
    .resize-handle.horizontal {{
      height: 10px;
      width: 100%;
      cursor: row-resize;
    }}
    .resize-handle.horizontal::before {{
      width: 84px;
      height: 4px;
    }}
    .stage {{
      display: grid;
      gap: 0.9rem;
      grid-template-rows: minmax(0, 1fr) 10px minmax(0, var(--tray-height));
      min-width: 0;
      min-height: 0;
      animation: rise 520ms ease both;
    }}
    .preview-panel,
    .detail-view-panel {{
      border: 1px solid var(--line);
      border-radius: 24px;
      background: linear-gradient(180deg, rgba(255,255,255,0.92), rgba(247,250,253,0.84));
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .preview-head,
    .detail-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding: 0.82rem 0.92rem;
      border-bottom: 1px solid rgba(185, 199, 215, 0.68);
      background: rgba(248,251,253,0.8);
    }}
    .preview-copy,
    .detail-copy {{
      display: grid;
      gap: 0.12rem;
    }}
    .window-dots {{
      display: inline-flex;
      gap: 0.42rem;
    }}
    .window-dots span {{
      width: 0.7rem;
      height: 0.7rem;
      border-radius: 999px;
      display: inline-block;
      background: rgba(20, 34, 53, 0.18);
    }}
    .window-dots span:nth-child(1) {{ background: #e87979; }}
    .window-dots span:nth-child(2) {{ background: #f2c56b; }}
    .window-dots span:nth-child(3) {{ background: #63c39e; }}
    .preview-head strong,
    .detail-head strong {{
      color: var(--navy);
      font-size: 0.74rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    .preview-head span,
    .detail-head span {{
      color: var(--muted);
      font-size: 0.84rem;
    }}
    .head-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
    }}
    iframe {{
      width: 100%;
      height: 100%;
      border: 0;
      background: white;
    }}
    .detail-card {{
      padding: 0.95rem;
      border-radius: 20px;
      border: 1px solid rgba(185, 199, 215, 0.66);
      background: rgba(255,255,255,0.72);
      cursor: pointer;
      transition: transform 140ms ease, border-color 140ms ease, background 140ms ease, box-shadow 140ms ease;
      box-shadow: var(--shadow-soft);
      margin-bottom: 0.7rem;
    }}
    .detail-card:hover {{
      transform: translateY(-1px);
      border-color: rgba(22, 56, 102, 0.22);
      background: rgba(255,255,255,0.88);
    }}
    .detail-card.active {{
      border-color: rgba(22, 56, 102, 0.42);
      background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(242,248,255,0.86));
      box-shadow: 0 16px 30px rgba(20, 34, 53, 0.1);
    }}
    .detail-card strong {{
      display: block;
      font-size: 0.93rem;
      margin-bottom: 0.32rem;
      color: var(--navy-deep);
    }}
    .detail-card .meta {{
      color: var(--muted);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      margin-bottom: 0.48rem;
    }}
    .detail-card .excerpt {{
      color: var(--muted);
      font-size: 0.88rem;
      line-height: 1.5;
      white-space: pre-wrap;
    }}
    .detail-view {{
      padding: 1.2rem;
      overflow: auto;
      background: rgba(255,255,255,0.56);
    }}
    .detail-view h2 {{
      margin: 0 0 0.3rem;
      font-family: var(--serif);
      font-size: 1.52rem;
      line-height: 1.02;
      letter-spacing: -0.03em;
    }}
    .detail-view .subhead {{
      color: var(--muted);
      margin-bottom: 0.92rem;
      line-height: 1.54;
    }}
    .columns {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 1rem;
    }}
    .column {{
      border: 1px solid rgba(185, 199, 215, 0.74);
      border-radius: 24px;
      background: rgba(255,255,255,0.88);
      padding: 1rem;
      box-shadow: var(--shadow-soft);
    }}
    .column h3 {{
      margin: 0 0 0.65rem;
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.13em;
      color: var(--navy);
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--serif);
      font-size: 0.98rem;
      line-height: 1.64;
    }}
    .empty {{
      padding: 2rem 1.25rem;
      color: var(--muted);
    }}
    .shell.nav-collapsed {{
      --nav-width: 0px;
    }}
    .shell.tray-collapsed {{
      --tray-height: 0px;
    }}
    .shell.nav-collapsed .workspace {{
      grid-template-columns: 0px 0px minmax(0, 1fr);
    }}
    .shell.tray-collapsed .stage {{
      grid-template-rows: minmax(0, 1fr) 0px 0px;
    }}
    .shell.nav-collapsed .navigator,
    .shell.nav-collapsed #nav-resize-handle,
    .shell.tray-collapsed .detail-view-panel,
    .shell.tray-collapsed #tray-resize-handle {{
      opacity: 0;
      pointer-events: none;
    }}
    .shell.nav-collapsed .navigator,
    .shell.tray-collapsed .detail-view-panel {{
      border-width: 0;
      padding: 0;
      min-height: 0;
    }}
    @media (max-width: 1120px) {{
      .command-bar,
      .workspace,
      .columns {{
        grid-template-columns: 1fr;
      }}
      .workspace {{
        grid-template-columns: 1fr;
      }}
      .resize-handle.vertical {{
        display: none;
      }}
      .command-side {{
        justify-items: start;
      }}
      .stat {{
        min-width: 0;
        flex: 1 1 120px;
      }}
    }}
    @media (max-width: 760px) {{
      .command-copy h1 {{
        font-size: 1.34rem;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <header class="command-bar">
      <div class="command-copy">
        <small>Review workspace</small>
        <h1 id="run-title">Loading...</h1>
        <p id="run-meta">Preparing review data.</p>
        <div class="command-meta">
          <div class="command-pill"><strong>Document first</strong> preview is the primary workspace</div>
          <div class="command-pill"><strong>Resizable</strong> drag the gutters to rebalance the view</div>
        </div>
      </div>
      <div class="command-side">
        <section class="summary-shell">
          <p class="shell-title">Run summary</p>
          <div id="summary-stats" class="summary-stats"></div>
        </section>
        <section class="download-shell">
          <p class="shell-title">Downloads</p>
          <div id="download-list" class="download-list"></div>
        </section>
        <div class="view-actions">
          <button id="toggle-nav" class="toolbar-button" type="button">Hide navigator</button>
          <button id="toggle-inspector" class="toolbar-button" type="button">Hide inspector</button>
          <button id="toggle-focus" class="toolbar-button primary" type="button">Focus document</button>
        </div>
      </div>
    </header>
    <div class="workspace" id="workspace">
      <aside class="navigator">
        <div class="navigator-kicker">
          <small>Navigator</small>
          <strong>Review in document order</strong>
          <span>Use the navigator and inspector as support panels while the blacklined document stays center stage.</span>
        </div>
        <section class="navigator-section">
          <p class="sidebar-title">Filter changes</p>
          <div class="filter-row" id="filter-row"></div>
        </section>
        <section class="navigator-section">
          <p class="sidebar-title">Search</p>
          <input id="search" class="search" type="search" placeholder="Filter by heading, change type, or text" />
        </section>
        <section class="navigator-section list-section">
          <p class="sidebar-title">Change navigator</p>
          <div id="detail-list" class="detail-list"></div>
        </section>
        <div class="sidebar-footnote">Tip: drag the vertical gutter to grow the marked-up document, or use focus mode when you only want the blackline on screen.</div>
        <a class="chip" href="/">Start a new compare</a>
      </aside>
      <div class="resize-handle vertical" id="nav-resize-handle" role="separator" aria-orientation="vertical" title="Drag to resize navigator"></div>
      <main class="stage" id="stage">
        <section class="preview-panel">
          <div class="preview-head">
            <div class="preview-copy">
              <strong>Blacklined document</strong>
              <span>The marked-up document is the main attraction. Resize the supporting panels around it as needed.</span>
            </div>
            <div class="head-actions">
              <div class="window-dots" aria-hidden="true"><span></span><span></span><span></span></div>
            </div>
          </div>
          <iframe id="preview-frame" title="Blackline document preview"></iframe>
        </section>
        <div class="resize-handle horizontal" id="tray-resize-handle" role="separator" aria-orientation="horizontal" title="Drag to resize inspector"></div>
        <section class="detail-view-panel">
          <div class="detail-head">
            <div class="detail-copy">
              <strong>Selected change</strong>
              <span>Original and revised text stay available below without taking over the page.</span>
            </div>
            <div class="head-actions">
              <button id="toggle-inspector-inline" class="toolbar-button" type="button">Collapse inspector</button>
            </div>
          </div>
          <div id="detail-view" class="detail-view">
            <div class="empty">Select a changed section to inspect the original and revised text side by side.</div>
          </div>
        </section>
      </main>
    </div>
  </div>
  <script>
    const runId = {json.dumps(run_id)};
    const state = {{
      metadata: null,
      filter: "changed",
      search: "",
      selectedIndex: null,
      navCollapsed: false,
      trayCollapsed: false,
      focusMode: false,
      resizing: null,
    }};

    const summaryStats = document.getElementById("summary-stats");
    const downloadList = document.getElementById("download-list");
    const filterRow = document.getElementById("filter-row");
    const detailList = document.getElementById("detail-list");
    const detailView = document.getElementById("detail-view");
    const searchInput = document.getElementById("search");
    const previewFrame = document.getElementById("preview-frame");
    const shell = document.querySelector(".shell");
    const workspace = document.getElementById("workspace");
    const stage = document.getElementById("stage");
    const navResizeHandle = document.getElementById("nav-resize-handle");
    const trayResizeHandle = document.getElementById("tray-resize-handle");
    const toggleNavButton = document.getElementById("toggle-nav");
    const toggleInspectorButton = document.getElementById("toggle-inspector");
    const toggleInspectorInlineButton = document.getElementById("toggle-inspector-inline");
    const toggleFocusButton = document.getElementById("toggle-focus");

    function slug(value) {{
      return String(value || "").toLowerCase();
    }}

    function clamp(value, min, max) {{
      return Math.min(max, Math.max(min, value));
    }}

    function excerpt(section) {{
      const source = section.revised_text || section.original_text || "";
      return source.length > 180 ? source.slice(0, 180) + "…" : source;
    }}

    function sectionCounts(metadata) {{
      const counts = {{
        all: metadata.sections.length,
        changed: 0,
        move: 0,
        replace: 0,
        insert: 0,
        delete: 0,
      }};
      for (const section of metadata.sections) {{
        if (section.is_changed) {{
          counts.changed += 1;
        }}
        if (Object.prototype.hasOwnProperty.call(counts, section.kind)) {{
          counts[section.kind] += 1;
        }}
      }}
      return counts;
    }}

    function filteredSections() {{
      if (!state.metadata) {{
        return [];
      }}
      const term = slug(state.search).trim();
      return state.metadata.sections.filter((section) => {{
        const byFilter =
          state.filter === "all" ? true :
          state.filter === "changed" ? section.is_changed :
          section.kind === state.filter;
        if (!byFilter) {{
          return false;
        }}
        if (!term) {{
          return true;
        }}
        const haystack = slug([
          section.label,
          section.kind,
          section.kind_label,
          section.original_text,
          section.revised_text,
          section.move_from_label,
          section.move_to_label,
        ].filter(Boolean).join(" "));
        return haystack.includes(term);
      }});
    }}

    function renderLayoutState() {{
      shell.classList.toggle("nav-collapsed", state.navCollapsed || state.focusMode);
      shell.classList.toggle("tray-collapsed", state.trayCollapsed || state.focusMode);
      toggleNavButton.textContent = (state.navCollapsed || state.focusMode) ? "Show navigator" : "Hide navigator";
      toggleInspectorButton.textContent = (state.trayCollapsed || state.focusMode) ? "Show inspector" : "Hide inspector";
      toggleInspectorInlineButton.textContent = (state.trayCollapsed || state.focusMode) ? "Show inspector" : "Collapse inspector";
      toggleFocusButton.textContent = state.focusMode ? "Exit focus" : "Focus document";
      toggleNavButton.disabled = state.focusMode;
      toggleInspectorButton.disabled = state.focusMode;
      toggleInspectorInlineButton.disabled = state.focusMode;
    }}

    function startResize(kind, event) {{
      if (state.focusMode) {{
        return;
      }}
      event.preventDefault();
      state.resizing = kind;
      document.body.classList.add("resizing");
      document.body.classList.toggle("row", kind === "tray");
    }}

    function stopResize() {{
      state.resizing = null;
      document.body.classList.remove("resizing", "row");
    }}

    window.addEventListener("pointermove", (event) => {{
      if (!state.resizing) {{
        return;
      }}
      if (state.resizing === "nav" && !(state.navCollapsed || state.focusMode)) {{
        const rect = workspace.getBoundingClientRect();
        const nextWidth = clamp(event.clientX - rect.left, 240, 460);
        shell.style.setProperty("--nav-width", `${{nextWidth}}px`);
      }}
      if (state.resizing === "tray" && !(state.trayCollapsed || state.focusMode)) {{
        const rect = stage.getBoundingClientRect();
        const nextHeight = clamp(rect.bottom - event.clientY, 170, rect.height - 180);
        shell.style.setProperty("--tray-height", `${{nextHeight}}px`);
      }}
    }});

    window.addEventListener("pointerup", stopResize);
    navResizeHandle.addEventListener("pointerdown", (event) => startResize("nav", event));
    trayResizeHandle.addEventListener("pointerdown", (event) => startResize("tray", event));

    toggleNavButton.addEventListener("click", () => {{
      state.navCollapsed = !state.navCollapsed;
      renderLayoutState();
    }});

    function toggleInspector() {{
      state.trayCollapsed = !state.trayCollapsed;
      renderLayoutState();
    }}

    toggleInspectorButton.addEventListener("click", toggleInspector);
    toggleInspectorInlineButton.addEventListener("click", toggleInspector);

    toggleFocusButton.addEventListener("click", () => {{
      state.focusMode = !state.focusMode;
      renderLayoutState();
    }});

    function renderSummary(metadata) {{
      document.getElementById("run-title").textContent = metadata.original_name + " → " + metadata.revised_name;
      document.getElementById("run-meta").textContent = metadata.profile_summary;
      previewFrame.src = metadata.preview_url;
      const counts = sectionCounts(metadata);

      const stats = [
        ["Changed", metadata.summary.changed_sections],
        ["Moved", metadata.summary.moved_sections],
        ["Inserted", metadata.summary.inserted_sections],
        ["Deleted", metadata.summary.deleted_sections],
      ];
      summaryStats.innerHTML = stats.map(([label, value]) => `
        <div class="stat">
          <strong>${{value}}</strong>
          <span>${{label}}</span>
        </div>
      `).join("");

      const downloads = [
        ...Object.entries(metadata.downloads).map(([fmt, href]) =>
          `<a class="chip" href="${{href}}" download>${{fmt.toUpperCase()}}</a>`
        ),
        `<a class="chip" href="${{metadata.preview_url}}" target="_blank" rel="noopener">Preview HTML</a>`,
      ];
      downloadList.innerHTML = downloads.join("");

      const filters = [
        ["changed", "Changed", counts.changed],
        ["move", "Moves", counts.move],
        ["replace", "Replaced", counts.replace],
        ["insert", "Inserted", counts.insert],
        ["delete", "Deleted", counts.delete],
        ["all", "All", counts.all],
      ];
      filterRow.innerHTML = filters.map(([value, label, count]) =>
        `<button class="filter-chip ${{state.filter === value ? "active" : ""}}" data-filter="${{value}}" type="button">${{label}} · ${{count}}</button>`
      ).join("");
      for (const button of filterRow.querySelectorAll(".filter-chip")) {{
        button.addEventListener("click", () => {{
          state.filter = button.dataset.filter;
          renderSections();
          renderSummary(metadata);
        }});
      }}
    }}

    function renderSections() {{
      const sections = filteredSections();
      if (!sections.length) {{
        detailList.innerHTML = '<div class="empty">No sections match the current filter.</div>';
        detailView.innerHTML = '<div class="empty">Adjust the filters or search to inspect another section.</div>';
        return;
      }}

      if (!sections.some((section) => section.index === state.selectedIndex)) {{
        state.selectedIndex = sections[0].index;
      }}

      detailList.innerHTML = sections.map((section) => `
        <article class="detail-card ${{section.index === state.selectedIndex ? "active" : ""}}" data-index="${{section.index}}">
          <strong>${{escapeHtml(section.label || ("Change " + section.index))}}</strong>
          <div class="meta">${{section.kind_label || section.kind}} · ${{section.block_kind}} · section ${{section.index}}</div>
          <div class="excerpt">${{escapeHtml(excerpt(section))}}</div>
        </article>
      `).join("");

      for (const card of detailList.querySelectorAll(".detail-card")) {{
        card.addEventListener("click", () => {{
          state.selectedIndex = Number(card.dataset.index);
          renderSections();
        }});
      }}

      const active = sections.find((section) => section.index === state.selectedIndex) || sections[0];
      detailView.innerHTML = `
        <h2>${{escapeHtml(active.kind_label || active.kind)}}</h2>
        <div class="subhead">
          ${{
            escapeHtml(active.label) +
            (active.move_from_label && active.move_to_label
              ? ` · moved from ${{escapeHtml(active.move_from_label)}} to ${{escapeHtml(active.move_to_label)}}`
              : "")
          }}
        </div>
        <div class="columns">
          <section class="column">
            <h3>Original</h3>
            <pre>${{escapeHtml(active.original_text || " ")}}</pre>
          </section>
          <section class="column">
            <h3>Revised</h3>
            <pre>${{escapeHtml(active.revised_text || " ")}}</pre>
          </section>
        </div>
      `;
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }}

    searchInput.addEventListener("input", () => {{
      state.search = searchInput.value;
      renderSections();
    }});

    renderLayoutState();

    fetch(`/api/runs/${{encodeURIComponent(runId)}}`)
      .then((response) => response.json().then((payload) => [response, payload]))
      .then(([response, payload]) => {{
        if (!response.ok) {{
          throw new Error(payload.error || "Unable to load review data.");
        }}
        state.metadata = payload;
        renderSummary(payload);
        renderSections();
      }})
      .catch((error) => {{
        document.getElementById("run-title").textContent = "Review load failed";
        document.getElementById("run-meta").textContent = error.message || String(error);
      }});
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
