from __future__ import annotations

import html
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence

from docx import Document
from docx.enum.text import WD_UNDERLINE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import RGBColor
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

WORD_PATTERN = re.compile(r"\w+|[^\w\s]+|\s+")


@dataclass(slots=True)
class Token:
    text: str
    kind: str  # "equal", "insert", "delete"


@dataclass(slots=True)
class RedlineParagraph:
    tokens: list[Token]


def load_text(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        text = path.read_text(encoding="utf-8")
        return text.splitlines()
    if suffix == ".docx":
        doc = Document(path)
        return [paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()]
    raise ValueError(f"Unsupported file type: {path.suffix}. Use .txt or .docx")


def tokenize_words(text: str) -> list[str]:
    return WORD_PATTERN.findall(text)


def diff_words(original: str, revised: str) -> list[Token]:
    original_tokens = tokenize_words(original)
    revised_tokens = tokenize_words(revised)
    matcher = SequenceMatcher(a=original_tokens, b=revised_tokens)
    output: list[Token] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            output.extend(Token(token, "equal") for token in original_tokens[i1:i2])
        elif tag == "delete":
            output.extend(Token(token, "delete") for token in original_tokens[i1:i2])
        elif tag == "insert":
            output.extend(Token(token, "insert") for token in revised_tokens[j1:j2])
        elif tag == "replace":
            output.extend(Token(token, "delete") for token in original_tokens[i1:i2])
            output.extend(Token(token, "insert") for token in revised_tokens[j1:j2])

    return output


def compare_paragraphs(original_paragraphs: Sequence[str], revised_paragraphs: Sequence[str]) -> list[RedlineParagraph]:
    matcher = SequenceMatcher(a=original_paragraphs, b=revised_paragraphs)
    redline: list[RedlineParagraph] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for paragraph in original_paragraphs[i1:i2]:
                redline.append(RedlineParagraph(tokens=[Token(paragraph, "equal")]))
            continue

        if tag == "delete":
            for paragraph in original_paragraphs[i1:i2]:
                redline.append(
                    RedlineParagraph(tokens=[Token(paragraph, "delete")])
                )
            continue

        if tag == "insert":
            for paragraph in revised_paragraphs[j1:j2]:
                redline.append(
                    RedlineParagraph(tokens=[Token(paragraph, "insert")])
                )
            continue

        # replace
        count = max(i2 - i1, j2 - j1)
        for idx in range(count):
            original_text = original_paragraphs[i1 + idx] if i1 + idx < i2 else ""
            revised_text = revised_paragraphs[j1 + idx] if j1 + idx < j2 else ""
            redline.append(RedlineParagraph(tokens=diff_words(original_text, revised_text)))

    return redline


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
    section_links = "\n".join(
        f'<li><a href="#para-{idx}">Paragraph {idx + 1}</a></li>'
        for idx in range(len(report))
    )
    body = "\n".join(
        f'<section id="para-{idx}"><h3>Paragraph {idx + 1}</h3><p>{_render_html_tokens(paragraph.tokens)}</p></section>'
        for idx, paragraph in enumerate(report)
    )

    html_content = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <title>Blackline Report</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; line-height: 1.5; }}
    .ins {{
      color: #0047ff;
      font-weight: 600;
      text-decoration-line: underline;
      text-decoration-style: double;
      text-decoration-color: #0047ff;
    }}
    .del {{
      color: #c00000;
      text-decoration-line: line-through underline;
      text-decoration-style: solid, double;
      text-decoration-color: #c00000, #0047ff;
    }}
    nav {{ position: sticky; top: 0; background: #fff; border-bottom: 1px solid #ddd; padding-bottom: .5rem; margin-bottom: 1rem; }}
    section {{ border-bottom: 1px solid #eee; padding: .75rem 0; }}
    h1, h2, h3 {{ margin: .4rem 0; }}
    ul {{ columns: 3; padding-left: 1rem; }}
  </style>
</head>
<body>
  <h1>Blackline Report</h1>
  <h2>{html.escape(source_a)} ⟶ {html.escape(source_b)}</h2>
  <nav>
    <strong>Jump to paragraph</strong>
    <ul>{section_links}</ul>
  </nav>
  {body}
</body>
</html>
"""
    output_path.write_text(html_content, encoding="utf-8")


def write_docx_report(report: Sequence[RedlineParagraph], output_path: Path, source_a: str, source_b: str) -> None:
    doc = Document()
    doc.add_heading("Blackline Report", level=1)
    doc.add_paragraph(f"{source_a} -> {source_b}")

    for paragraph in report:
        p = doc.add_paragraph()
        for token in paragraph.tokens:
            run = p.add_run(token.text)
            if token.kind == "insert":
                run.font.color.rgb = RGBColor(0, 71, 255)
                run.bold = True
                run.font.underline = WD_UNDERLINE.DOUBLE
                _set_underline_color(run, "0047FF")
            elif token.kind == "delete":
                run.font.color.rgb = RGBColor(192, 0, 0)
                run.font.strike = True
                run.font.underline = WD_UNDERLINE.DOUBLE
                _set_underline_color(run, "0047FF")

    doc.save(output_path)


def _pdf_markup(tokens: Iterable[Token]) -> str:
    chunks: list[str] = []
    for token in tokens:
        escaped = html.escape(token.text).replace("\n", "<br/>")
        if token.kind == "equal":
            chunks.append(escaped)
        elif token.kind == "insert":
            chunks.append(f'<font color="#0047FF"><u><b>{escaped}</b></u></font>')
        elif token.kind == "delete":
            chunks.append(f'<font color="#C00000"><strike>{escaped}</strike></font><font color="#0047FF"><u>{escaped}</u></font>')
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
