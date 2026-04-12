from __future__ import annotations

import html
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence

try:
    from docx import Document
    from docx.enum.text import WD_UNDERLINE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import RGBColor
except ModuleNotFoundError:  # pragma: no cover - exercised in environments without optional deps
    Document = None
    WD_UNDERLINE = None
    OxmlElement = None
    qn = None
    RGBColor = None

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
except ModuleNotFoundError:  # pragma: no cover - exercised in environments without optional deps
    colors = None
    LETTER = None
    getSampleStyleSheet = None
    Paragraph = None
    SimpleDocTemplate = None
    Spacer = None

from .strict import substantive_key, tokens_equivalent_for_strict

WORD_PATTERN = re.compile(r"\w+|[^\w\s]+|\s+")


@dataclass(slots=True)
class Token:
    text: str
    kind: str  # "equal", "insert", "delete"


@dataclass(slots=True)
class RedlineParagraph:
    tokens: list[Token]


@dataclass(slots=True)
class StyledToken:
    text: str
    normalized: str
    style: dict[str, object]


def load_text(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return path.read_text(encoding="utf-8").splitlines()
    if suffix == ".docx":
        _require_docx()
        doc = Document(path)
        return [paragraph.text for paragraph in doc.paragraphs]
    raise ValueError(f"Unsupported file type: {path.suffix}. Use .txt or .docx")


def tokenize_words(text: str) -> list[str]:
    """Split text into words, punctuation, and whitespace for diffing."""
    return WORD_PATTERN.findall(text)


def diff_words(original: str, revised: str, *, substantive_only: bool = False) -> list[Token]:
    original_tokens = tokenize_words(original)
    revised_tokens = tokenize_words(revised)
    original_keys = [substantive_key(token) for token in original_tokens] if substantive_only else original_tokens
    revised_keys = [substantive_key(token) for token in revised_tokens] if substantive_only else revised_tokens
    matcher = SequenceMatcher(a=original_keys, b=revised_keys, autojunk=False)
    output: list[Token] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            output.extend(Token(token, "equal") for token in original_tokens[i1:i2])
        elif tag == "delete":
            output.extend(Token(token, "delete") for token in original_tokens[i1:i2])
        elif tag == "insert":
            output.extend(Token(token, "insert") for token in revised_tokens[j1:j2])
        elif tag == "replace":
            original_chunk = original_tokens[i1:i2]
            revised_chunk = revised_tokens[j1:j2]
            if substantive_only and tokens_equivalent_for_strict(original_chunk, revised_chunk):
                output.extend(Token(token, "equal") for token in revised_chunk)
            else:
                output.extend(Token(token, "delete") for token in original_chunk)
                output.extend(Token(token, "insert") for token in revised_chunk)

    return output


def _normalize_token(token: str) -> str:
    return token.casefold()


def _run_style(run) -> dict[str, object]:
    return {
        "bold": bool(run.bold),
        "italic": bool(run.italic),
        "underline": run.underline,
        "font_name": run.font.name,
        "font_size": run.font.size,
    }


def _tokenize_paragraph_with_style(paragraph) -> list[StyledToken]:
    text = "".join(run.text for run in paragraph.runs)
    if not text:
        return []

    style_by_char: list[dict[str, object]] = []
    for run in paragraph.runs:
        run_style = _run_style(run)
        if not run.text:
            continue
        style_by_char.extend(run_style for _ in run.text)

    tokens: list[StyledToken] = []
    for match in WORD_PATTERN.finditer(text):
        start = match.start()
        token_text = match.group(0)
        style = style_by_char[start] if start < len(style_by_char) else {}
        tokens.append(
            StyledToken(
                text=token_text,
                normalized=_normalize_token(token_text),
                style=style,
            )
        )
    return tokens


def _append_run_with_style(paragraph, text: str, style: dict[str, object], kind: str) -> None:
    run = paragraph.add_run(text)
    run.bold = style.get("bold", False)
    run.italic = style.get("italic", False)
    run.underline = style.get("underline", False)
    run.font.name = style.get("font_name")
    run.font.size = style.get("font_size")

    if kind == "insert":
        run.font.color.rgb = RGBColor(0, 71, 255)
        run.font.underline = WD_UNDERLINE.DOUBLE
        _set_underline_color(run, "0047FF")
    elif kind == "delete":
        run.font.color.rgb = RGBColor(192, 0, 0)
        run.font.strike = True


def _paragraph_compare_key(text: str, *, substantive_only: bool) -> str:
    if not substantive_only:
        return text.casefold()
    keys = [substantive_key(token) for token in tokenize_words(text)]
    return " ".join(key for key in keys if key.strip())


def _append_plain_paragraph(report: list[RedlineParagraph], text: str, kind: str) -> None:
    report.append(RedlineParagraph(tokens=[Token(text, kind)]))


def _compare_changed_block(
    original_block: Sequence[str],
    revised_block: Sequence[str],
    *,
    substantive_only: bool,
) -> list[RedlineParagraph]:
    block_matcher = SequenceMatcher(
        a=[_paragraph_compare_key(paragraph, substantive_only=substantive_only) for paragraph in original_block],
        b=[_paragraph_compare_key(paragraph, substantive_only=substantive_only) for paragraph in revised_block],
        autojunk=False,
    )
    redline: list[RedlineParagraph] = []

    for block_tag, a1, a2, b1, b2 in block_matcher.get_opcodes():
        if block_tag == "equal":
            for paragraph in revised_block[b1:b2]:
                _append_plain_paragraph(redline, paragraph, "equal")
            continue

        if block_tag == "delete":
            for paragraph in original_block[a1:a2]:
                _append_plain_paragraph(redline, paragraph, "delete")
            continue

        if block_tag == "insert":
            for paragraph in revised_block[b1:b2]:
                _append_plain_paragraph(redline, paragraph, "insert")
            continue

        nested_count = max(a2 - a1, b2 - b1)
        for nested_idx in range(nested_count):
            original_text = original_block[a1 + nested_idx] if a1 + nested_idx < a2 else ""
            revised_text = revised_block[b1 + nested_idx] if b1 + nested_idx < b2 else ""
            redline.append(
                RedlineParagraph(
                    tokens=diff_words(
                        original_text,
                        revised_text,
                        substantive_only=substantive_only,
                    )
                )
            )

    return redline


def compare_paragraphs_with_options(
    original_paragraphs: Sequence[str],
    revised_paragraphs: Sequence[str],
    *,
    substantive_only: bool = False,
) -> list[RedlineParagraph]:
    matcher = SequenceMatcher(
        a=[_paragraph_compare_key(paragraph, substantive_only=substantive_only) for paragraph in original_paragraphs],
        b=[_paragraph_compare_key(paragraph, substantive_only=substantive_only) for paragraph in revised_paragraphs],
        autojunk=False,
    )
    redline: list[RedlineParagraph] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for paragraph in revised_paragraphs[j1:j2]:
                _append_plain_paragraph(redline, paragraph, "equal")
            continue

        if tag == "delete":
            for paragraph in original_paragraphs[i1:i2]:
                _append_plain_paragraph(redline, paragraph, "delete")
            continue

        if tag == "insert":
            for paragraph in revised_paragraphs[j1:j2]:
                _append_plain_paragraph(redline, paragraph, "insert")
            continue

        redline.extend(
            _compare_changed_block(
                original_paragraphs[i1:i2],
                revised_paragraphs[j1:j2],
                substantive_only=substantive_only,
            )
        )

    return redline


def _compare_paragraphs(
    original_paragraphs: Sequence[str],
    revised_paragraphs: Sequence[str],
    *,
    substantive_only: bool = False,
) -> list[RedlineParagraph]:
    return compare_paragraphs_with_options(
        original_paragraphs,
        revised_paragraphs,
        substantive_only=substantive_only,
    )


def compare_paragraphs(
    original_paragraphs: Sequence[str],
    revised_paragraphs: Sequence[str],
) -> list[RedlineParagraph]:
    return compare_paragraphs_with_options(
        original_paragraphs,
        revised_paragraphs,
        substantive_only=False,
    )


def compare_paragraphs_strict(
    original_paragraphs: Sequence[str],
    revised_paragraphs: Sequence[str],
) -> list[RedlineParagraph]:
    return compare_paragraphs_with_options(
        original_paragraphs,
        revised_paragraphs,
        substantive_only=True,
    )


def _paragraph_style_name(paragraph) -> str | None:
    if paragraph is None or paragraph.style is None:
        return None
    return paragraph.style.name


def write_docx_blackline_with_formatting(
    original_path: Path,
    revised_path: Path,
    output_path: Path,
    *,
    substantive_only: bool = False,
) -> None:
    _require_docx()
    original_doc = Document(original_path)
    revised_doc = Document(revised_path)
    output_doc = Document()

    output_doc.add_heading("Blackline Report", level=1)
    output_doc.add_paragraph(f"{original_path.name} -> {revised_path.name}")

    original_paragraphs = list(original_doc.paragraphs)
    revised_paragraphs = list(revised_doc.paragraphs)
    paragraph_matcher = SequenceMatcher(
        a=[_paragraph_compare_key(p.text, substantive_only=substantive_only) for p in original_paragraphs],
        b=[_paragraph_compare_key(p.text, substantive_only=substantive_only) for p in revised_paragraphs],
        autojunk=False,
    )

    for tag, i1, i2, j1, j2 in paragraph_matcher.get_opcodes():
        if tag == "equal":
            for para in revised_paragraphs[j1:j2]:
                out = output_doc.add_paragraph(style=_paragraph_style_name(para))
                for run in para.runs:
                    _append_run_with_style(out, run.text, _run_style(run), "equal")
            continue

        if tag == "insert":
            for para in revised_paragraphs[j1:j2]:
                out = output_doc.add_paragraph(style=_paragraph_style_name(para))
                for token in _tokenize_paragraph_with_style(para):
                    _append_run_with_style(out, token.text, token.style, "insert")
            continue

        if tag == "delete":
            for para in original_paragraphs[i1:i2]:
                out = output_doc.add_paragraph(style=_paragraph_style_name(para))
                for token in _tokenize_paragraph_with_style(para):
                    _append_run_with_style(out, token.text, token.style, "delete")
            continue

        count = max(i2 - i1, j2 - j1)
        for idx in range(count):
            original_para = original_paragraphs[i1 + idx] if i1 + idx < i2 else None
            revised_para = revised_paragraphs[j1 + idx] if j1 + idx < j2 else None
            out = output_doc.add_paragraph(
                style=_paragraph_style_name(revised_para) or _paragraph_style_name(original_para)
            )

            original_tokens = _tokenize_paragraph_with_style(original_para) if original_para else []
            revised_tokens = _tokenize_paragraph_with_style(revised_para) if revised_para else []
            word_matcher = SequenceMatcher(
                a=[
                    substantive_key(token.text) if substantive_only else token.normalized
                    for token in original_tokens
                ],
                b=[
                    substantive_key(token.text) if substantive_only else token.normalized
                    for token in revised_tokens
                ],
                autojunk=False,
            )

            for word_tag, a1, a2, b1, b2 in word_matcher.get_opcodes():
                if word_tag == "equal":
                    for token in revised_tokens[b1:b2]:
                        _append_run_with_style(out, token.text, token.style, "equal")
                elif word_tag == "insert":
                    for token in revised_tokens[b1:b2]:
                        _append_run_with_style(out, token.text, token.style, "insert")
                elif word_tag == "delete":
                    for token in original_tokens[a1:a2]:
                        _append_run_with_style(out, token.text, token.style, "delete")
                elif word_tag == "replace":
                    original_chunk = original_tokens[a1:a2]
                    revised_chunk = revised_tokens[b1:b2]
                    if substantive_only and tokens_equivalent_for_strict(
                        [token.text for token in original_chunk],
                        [token.text for token in revised_chunk],
                    ):
                        for token in revised_chunk:
                            _append_run_with_style(out, token.text, token.style, "equal")
                    else:
                        for token in original_chunk:
                            _append_run_with_style(out, token.text, token.style, "delete")
                        for token in revised_chunk:
                            _append_run_with_style(out, token.text, token.style, "insert")

    output_doc.save(output_path)


def _render_html_tokens(tokens: Iterable[Token]) -> str:
    chunks: list[str] = []
    for token in tokens:
        escaped = html.escape(token.text)
        if token.kind == "equal":
            chunks.append(escaped)
        elif token.kind == "insert":
            chunks.append(f'<span class="ins">{escaped}</span>')
        elif token.kind == "delete":
            chunks.append(f'<span class="del">{escaped}</span>')
    return "".join(chunks)


def write_html_report(report: Sequence[RedlineParagraph], output_path: Path, source_a: str, source_b: str) -> None:
    body = "\n".join(f"<p>{_render_html_tokens(paragraph.tokens)}</p>" for paragraph in report)

    html_content = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <title>Blackline Report</title>
  <style>
    body {{ font-family: "Times New Roman", Georgia, serif; margin: 2rem auto; max-width: 8.5in; line-height: 1.5; color: #111; }}
    .ins {{
      color: #0b3fae;
      text-decoration-line: underline;
      text-decoration-style: double;
      text-decoration-color: #0b3fae;
    }}
    .del {{
      color: #c00000;
      text-decoration-line: line-through;
      text-decoration-style: solid;
      text-decoration-color: #c00000;
    }}
    h1, h2 {{ margin: .25rem 0; }}
    p {{ margin: 0 0 0.8rem; }}
  </style>
</head>
<body>
  <h1>Blackline Report</h1>
  <h2>{html.escape(source_a)} ⟶ {html.escape(source_b)}</h2>
  {body}
</body>
</html>
"""
    output_path.write_text(html_content, encoding="utf-8")


def write_docx_report(report: Sequence[RedlineParagraph], output_path: Path, source_a: str, source_b: str) -> None:
    _require_docx()
    doc = Document()
    doc.add_heading("Blackline Report", level=1)
    doc.add_paragraph(f"{source_a} -> {source_b}")

    for paragraph in report:
        out = doc.add_paragraph()
        for token in paragraph.tokens:
            run = out.add_run(token.text)
            if token.kind == "insert":
                run.font.color.rgb = RGBColor(0, 71, 255)
                run.font.underline = WD_UNDERLINE.DOUBLE
                _set_underline_color(run, "0047FF")
            elif token.kind == "delete":
                run.font.color.rgb = RGBColor(192, 0, 0)
                run.font.strike = True

    doc.save(output_path)


def _pdf_markup(tokens: Iterable[Token]) -> str:
    chunks: list[str] = []
    for token in tokens:
        escaped = html.escape(token.text).replace("\n", "<br/>")
        if token.kind == "equal":
            chunks.append(escaped)
        elif token.kind == "insert":
            chunks.append(f'<font color="#0047FF"><u>{escaped}</u></font>')
        elif token.kind == "delete":
            chunks.append(f'<font color="#C00000"><strike>{escaped}</strike></font>')
    return "".join(chunks)


def _set_underline_color(run, color_hex: str) -> None:
    r_pr = run._r.get_or_add_rPr()
    underline = r_pr.find(qn("w:u"))
    if underline is None:
        underline = OxmlElement("w:u")
        r_pr.append(underline)
    underline.set(qn("w:val"), "double")
    underline.set(qn("w:color"), color_hex)


def write_pdf_report(report: Sequence[RedlineParagraph], output_path: Path, source_a: str, source_b: str) -> None:
    _require_reportlab()
    doc = SimpleDocTemplate(str(output_path), pagesize=LETTER)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Blackline Report", styles["Title"]),
        Paragraph(f"{html.escape(source_a)} -> {html.escape(source_b)}", styles["Normal"]),
        Spacer(1, 12),
    ]

    for idx, paragraph in enumerate(report, start=1):
        story.append(Paragraph(f"<b>Paragraph {idx}</b>", styles["Heading4"]))
        style = styles["BodyText"].clone("BodyTextRedline")
        style.textColor = colors.black
        story.append(Paragraph(_pdf_markup(paragraph.tokens), style))
        story.append(Spacer(1, 10))

    doc.build(story)


def _require_docx() -> None:
    if Document is None or WD_UNDERLINE is None or RGBColor is None or OxmlElement is None or qn is None:
        raise ModuleNotFoundError(
            "python-docx is required for DOCX input/output. Install dependencies with: pip install -e ."
        )


def _require_reportlab() -> None:
    if (
        colors is None
        or LETTER is None
        or getSampleStyleSheet is None
        or Paragraph is None
        or SimpleDocTemplate is None
        or Spacer is None
    ):
        raise ModuleNotFoundError(
            "reportlab is required for PDF output. Install dependencies with: pip install -e ."
        )
