from __future__ import annotations

import re
from copy import deepcopy
from collections import Counter, defaultdict
from pathlib import Path
from typing import Sequence
from difflib import SequenceMatcher

from .models import (
    Token, DocumentBlock, RedlineSection, ReportSummary, RedlineReport, CompareOptions, RedlineParagraph
)
from .diff import (
    tokenize_words, diff_words, block_alignment_key, 
    _simple_tokens, paragraph_compare_key
)
from .utils import (
    _resolve_options, _section_kind_label, _section_location_kind
)
from .docx_engine import (
    _paragraph_layout_signature, _layout_change_facets, 
    _require_docx_loading, _build_docx_containers, _flatten_word_containers,
    _flatten_word_blocks, Document
)


def _tokens_for_original(combined_tokens: Sequence[Token]) -> list[Token]:
    tokens: list[Token] = []
    for token in combined_tokens:
        if token.kind == "insert":
            continue
        kind = "delete" if token.kind == "delete" else "equal"
        tokens.append(Token(token.text, kind))
    return tokens


def _tokens_for_revised(combined_tokens: Sequence[Token]) -> list[Token]:
    tokens: list[Token] = []
    for token in combined_tokens:
        if token.kind == "delete":
            continue
        kind = "insert" if token.kind == "insert" else "equal"
        tokens.append(Token(token.text, kind))
    return tokens


def _format_change_facets(
    original_block: DocumentBlock | None,
    revised_block: DocumentBlock | None,
) -> list[str]:
    if original_block is None or revised_block is None:
        return []

    facets: list[str] = []
    if (original_block.style_name or "") != (revised_block.style_name or ""):
        facets.extend(["formatting", "style"])
    if original_block.alignment != revised_block.alignment:
        facets.extend(["formatting", "alignment"])

    layout_facets = _layout_change_facets(original_block.layout, revised_block.layout)
    if layout_facets:
        facets.append("formatting")
        facets.extend(layout_facets)

    return facets


def _punctuation_or_whitespace_only(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return all(not ch.isalnum() for ch in stripped)


def _strip_punctuation(text: str) -> str:
    return re.sub(r"[^\w\s]+", "", text)


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _text_equivalent_with_options(
    original_text: str,
    revised_text: str,
    *,
    ignore_case: bool = False,
    ignore_whitespace: bool = False,
    ignore_smart_punctuation: bool = False,
    ignore_punctuation: bool = False,
    ignore_numbering: bool = False,
) -> bool:
    options = CompareOptions(
        profile_name="default",
        ignore_case=ignore_case,
        ignore_whitespace=ignore_whitespace,
        ignore_smart_punctuation=ignore_smart_punctuation,
        ignore_punctuation=ignore_punctuation,
        ignore_numbering=ignore_numbering,
        detect_moves=False,
    )
    return paragraph_compare_key(original_text, options) == paragraph_compare_key(revised_text, options)


def _numbering_only_text(text: str) -> bool:
    from .diff import NUMBERING_TOKEN_PATTERN
    tokens = [token.strip() for token in tokenize_words(text) if token.strip()]
    if not tokens:
        return False
    alnum_tokens = [token for token in tokens if any(ch.isalnum() for ch in token)]
    if not alnum_tokens:
        return False
    return all(bool(NUMBERING_TOKEN_PATTERN.fullmatch(token)) for token in alnum_tokens)


def _text_change_facets(section: RedlineSection) -> list[str]:
    if not section.is_changed:
        return []
    if section.kind == "replace" and section.original_text == section.revised_text:
        return []

    if section.kind in {"insert", "delete", "move"}:
        delta_text = section.revised_text if section.kind in {"insert", "move"} else section.original_text
        if not delta_text.strip():
            return ["whitespace"]
        if _punctuation_or_whitespace_only(delta_text):
            return ["punctuation"]
        if _numbering_only_text(delta_text):
            return ["numbering"]
        return ["content"]

    facets: list[str] = []
    if _text_equivalent_with_options(section.original_text, section.revised_text, ignore_whitespace=True):
        facets.append("whitespace")
    if _text_equivalent_with_options(section.original_text, section.revised_text, ignore_case=True):
        facets.append("capitalization")
    if _collapse_whitespace(_strip_punctuation(section.original_text)) == _collapse_whitespace(
        _strip_punctuation(section.revised_text)
    ):
        facets.append("punctuation")
    if _text_equivalent_with_options(section.original_text, section.revised_text, ignore_numbering=True):
        facets.append("numbering")
    if not facets:
        facets.append("content")
    return facets


def _section_change_facets(section: RedlineSection) -> list[str]:
    facets = _text_change_facets(section)
    facets.extend(section.format_change_facets)
    if section.block_kind == "table_row":
        facets.append("table")
    if section.location_kind != "body":
        facets.append(section.location_kind)
    seen: set[str] = set()
    unique_facets: list[str] = []
    for facet in facets:
        if facet in seen:
            continue
        seen.add(facet)
        unique_facets.append(facet)
    return unique_facets


def _make_section(
    kind: str,
    *,
    original_block: DocumentBlock | None,
    revised_block: DocumentBlock | None,
    options: CompareOptions,
) -> RedlineSection:
    original_text = original_block.text if original_block else ""
    revised_text = revised_block.text if revised_block else ""
    label = revised_block.label if revised_block else original_block.label
    block_kind = revised_block.kind if revised_block else original_block.kind
    style_name = revised_block.style_name if revised_block and revised_block.style_name else (
        original_block.style_name if original_block else None
    )
    alignment = revised_block.alignment if revised_block and revised_block.alignment is not None else (
        original_block.alignment if original_block else None
    )
    container = revised_block.container if revised_block else (original_block.container if original_block else "body")
    format_change_facets = _format_change_facets(original_block, revised_block)

    if kind == "equal":
        combined_tokens = _simple_tokens(revised_text, "equal")
        original_tokens = _simple_tokens(original_text, "equal")
        revised_tokens = _simple_tokens(revised_text, "equal")
    elif kind == "insert":
        combined_tokens = _simple_tokens(revised_text, "insert")
        original_tokens = []
        revised_tokens = _simple_tokens(revised_text, "insert")
    elif kind == "delete":
        combined_tokens = _simple_tokens(original_text, "delete")
        original_tokens = _simple_tokens(original_text, "delete")
        revised_tokens = []
    else:
        combined_tokens = diff_words(original_text, revised_text, options=options)
        if not any(token.kind != "equal" for token in combined_tokens):
            combined_tokens = _simple_tokens(revised_text, "equal")
            original_tokens = _simple_tokens(original_text, "equal")
            revised_tokens = _simple_tokens(revised_text, "equal")
            kind = "equal"
        else:
            original_tokens = _tokens_for_original(combined_tokens)
            revised_tokens = _tokens_for_revised(combined_tokens)

    if kind == "equal" and format_change_facets:
        kind = "replace"

    return RedlineSection(
        index=0,
        label=label,
        kind=kind,
        block_kind=block_kind,
        container=container,
        original_text=original_text,
        revised_text=revised_text,
        combined_tokens=combined_tokens,
        original_tokens=original_tokens,
        revised_tokens=revised_tokens,
        format_change_facets=format_change_facets,
        style_name=style_name,
        alignment=alignment,
        original_label=original_block.label if original_block else None,
        revised_label=revised_block.label if revised_block else None,
        kind_label=_section_kind_label(kind),
    )


def _block_compare_key(block: DocumentBlock, options: CompareOptions) -> str:
    return block_alignment_key(block.text, options)


def _text_compare_key(text: str, options: CompareOptions) -> str:
    return block_alignment_key(text, options)


def _compare_changed_block(
    original_block: Sequence[DocumentBlock],
    revised_block: Sequence[DocumentBlock],
    *,
    options: CompareOptions,
) -> list[RedlineSection]:
    block_matcher = SequenceMatcher(
        a=[_block_compare_key(block, options) for block in original_block],
        b=[_block_compare_key(block, options) for block in revised_block],
        autojunk=False,
    )
    sections: list[RedlineSection] = []

    for block_tag, a1, a2, b1, b2 in block_matcher.get_opcodes():
        if block_tag == "equal":
            for original_item, revised_item in zip(original_block[a1:a2], revised_block[b1:b2]):
                sections.append(
                    _make_section(
                        "equal",
                        original_block=original_item,
                        revised_block=revised_item,
                        options=options,
                    )
                )
            continue

        if block_tag == "delete":
            for block in original_block[a1:a2]:
                sections.append(_make_section("delete", original_block=block, revised_block=None, options=options))
            continue

        if block_tag == "insert":
            for block in revised_block[b1:b2]:
                sections.append(_make_section("insert", original_block=None, revised_block=block, options=options))
            continue

        nested_count = max(a2 - a1, b2 - b1)
        for nested_idx in range(nested_count):
            original_item = original_block[a1 + nested_idx] if a1 + nested_idx < a2 else None
            revised_item = revised_block[b1 + nested_idx] if b1 + nested_idx < b2 else None
            if original_item and revised_item:
                sections.append(
                    _make_section(
                        "replace",
                        original_block=original_item,
                        revised_block=revised_item,
                        options=options,
                    )
                )
            elif original_item:
                sections.append(
                    _make_section("delete", original_block=original_item, revised_block=None, options=options)
                )
            elif revised_item:
                sections.append(
                    _make_section("insert", original_block=None, revised_block=revised_item, options=options)
                )

    return sections


def _reindex_sections(sections: list[RedlineSection]) -> list[RedlineSection]:
    for idx, section in enumerate(sections, start=1):
        section.index = idx
        section.kind_label = _section_kind_label(section.kind)
        section.location_kind = _section_location_kind(section.container)
        section.change_facets = _section_change_facets(section)
    return sections


def _apply_move_detection(sections: list[RedlineSection], options: CompareOptions) -> list[RedlineSection]:
    from collections import deque
    if not options.detect_moves:
        return _reindex_sections(sections)

    delete_candidates: dict[str, deque[int]] = defaultdict(deque)
    for idx, section in enumerate(sections):
        if section.kind == "delete":
            delete_candidates[_text_compare_key(section.original_text, options)].append(idx)

    hidden_indices: set[int] = set()
    for idx, section in enumerate(sections):
        if section.kind != "insert":
            continue
        compare_key = _text_compare_key(section.revised_text, options)
        candidates = delete_candidates.get(compare_key)
        if not candidates:
            continue
        while candidates and candidates[0] in hidden_indices:
            candidates.popleft()
        if not candidates:
            continue

        delete_idx = candidates.popleft()
        deleted = sections[delete_idx]
        hidden_indices.add(delete_idx)
        section.kind = "move"
        section.kind_label = _section_kind_label("move")
        section.original_text = deleted.original_text
        section.original_label = deleted.original_label or deleted.label
        section.move_from_label = deleted.original_label or deleted.label
        section.move_to_label = section.revised_label or section.label
        section.combined_tokens = _simple_tokens(section.revised_text, "equal")
        section.original_tokens = _simple_tokens(section.original_text, "equal")
        section.revised_tokens = _simple_tokens(section.revised_text, "equal")

    visible = [section for idx, section in enumerate(sections) if idx not in hidden_indices]
    return _reindex_sections(visible)


def _summarize_sections(sections: Sequence[RedlineSection]) -> ReportSummary:
    counts = Counter(section.kind for section in sections)
    total = len(sections)
    unchanged = counts.get("equal", 0)
    changed = total - unchanged
    return ReportSummary(
        total_sections=total,
        changed_sections=changed,
        unchanged_sections=unchanged,
        inserted_sections=counts.get("insert", 0),
        deleted_sections=counts.get("delete", 0),
        replaced_sections=counts.get("replace", 0),
        moved_sections=counts.get("move", 0),
    )


def build_report_from_blocks(
    original_blocks: Sequence[DocumentBlock],
    revised_blocks: Sequence[DocumentBlock],
    *,
    source_a: str,
    source_b: str,
    options: CompareOptions | None = None,
) -> RedlineReport:
    resolved = _resolve_options(options=options)
    matcher = SequenceMatcher(
        a=[_block_compare_key(block, resolved) for block in original_blocks],
        b=[_block_compare_key(block, resolved) for block in revised_blocks],
        autojunk=False,
    )
    sections: list[RedlineSection] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for original_block, revised_block in zip(original_blocks[i1:i2], revised_blocks[j1:j2]):
                sections.append(
                    _make_section(
                        "equal",
                        original_block=original_block,
                        revised_block=revised_block,
                        options=resolved,
                    )
                )
            continue

        if tag == "delete":
            for block in original_blocks[i1:i2]:
                sections.append(_make_section("delete", original_block=block, revised_block=None, options=resolved))
            continue

        if tag == "insert":
            for block in revised_blocks[j1:j2]:
                sections.append(_make_section("insert", original_block=None, revised_block=block, options=resolved))
            continue

        sections.extend(
            _compare_changed_block(
                original_blocks[i1:i2],
                revised_blocks[j1:j2],
                options=resolved,
            )
        )

    document_sections = _reindex_sections(deepcopy(sections))
    sections = _apply_move_detection(sections, resolved)
    return RedlineReport(
        source_a=source_a,
        source_b=source_b,
        options=resolved,
        sections=sections,
        document_sections=document_sections,
        summary=_summarize_sections(sections),
        structure_kinds=sorted({block.kind for block in (*original_blocks, *revised_blocks)}),
    )


def _blocks_from_lines(lines: Sequence[str]) -> list[DocumentBlock]:
    return [
        DocumentBlock(
            label=f"Paragraph {idx}",
            text=line,
            kind="paragraph",
            style_name="Normal",
        )
        for idx, line in enumerate(lines, start=1)
    ]


def compare_paragraphs_with_options(
    original_paragraphs: Sequence[str],
    revised_paragraphs: Sequence[str],
    *,
    substantive_only: bool = False,
    options: CompareOptions | None = None,
) -> list[RedlineParagraph]:
    base_options = _resolve_options(options=options, substantive_only=substantive_only)
    legacy_options = CompareOptions(
        profile_name=base_options.profile_name,
        ignore_case=base_options.ignore_case,
        ignore_whitespace=base_options.ignore_whitespace,
        ignore_smart_punctuation=base_options.ignore_smart_punctuation,
        ignore_punctuation=base_options.ignore_punctuation,
        ignore_numbering=base_options.ignore_numbering,
        detect_moves=False,
    )
    report = build_report_from_blocks(
        _blocks_from_lines(original_paragraphs),
        _blocks_from_lines(revised_paragraphs),
        source_a="original",
        source_b="revised",
        options=legacy_options,
    )
    return [RedlineParagraph(tokens=list(section.combined_tokens)) for section in report.sections]


def compare_paragraphs(
    original_paragraphs: Sequence[str],
    revised_paragraphs: Sequence[str],
) -> list[RedlineParagraph]:
    return compare_paragraphs_with_options(original_paragraphs, revised_paragraphs)


def compare_paragraphs_strict(
    original_paragraphs: Sequence[str],
    revised_paragraphs: Sequence[str],
) -> list[RedlineParagraph]:
    return compare_paragraphs_with_options(
        original_paragraphs, revised_paragraphs, substantive_only=True
    )


def load_document_blocks(path: Path) -> list[DocumentBlock]:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return _blocks_from_lines(path.read_text(encoding="utf-8").splitlines())
    if suffix != ".docx":
        raise ValueError(f"Unsupported file type: {path.suffix}. Use .txt or .docx")

    _require_docx_loading()
    doc = Document(path)
    if not hasattr(doc, "element"):
        return [
            DocumentBlock(
                label=f"Paragraph {idx}",
                text=paragraph.text,
                kind="paragraph",
                style_name=getattr(getattr(paragraph, "style", None), "name", None),
                alignment=getattr(paragraph, "alignment", None),
                layout=_paragraph_layout_signature(paragraph),
            )
            for idx, paragraph in enumerate(doc.paragraphs, start=1)
        ]
    containers, _ = _build_docx_containers(doc)
    return _flatten_word_containers(containers)


def load_text(path: Path) -> list[str]:
    return [block.text for block in load_document_blocks(path)]


def _is_inert_non_body_section(section: RedlineSection, container_kind: str) -> bool:
    if container_kind == "body":
        return False
    if section.kind != "equal":
        return False
    if section.block_kind not in {"paragraph", "section_break"}:
        return False
    return not section.original_text.strip() and not section.revised_text.strip()


def _build_report_from_container_sets(
    original_containers: Sequence[Any],
    revised_containers: Sequence[Any],
    *,
    source_a: str,
    source_b: str,
    options: CompareOptions,
) -> RedlineReport:
    original_by_kind: dict[str, list[Any]] = defaultdict(list)
    revised_by_kind: dict[str, list[Any]] = defaultdict(list)
    for container in original_containers:
        original_by_kind[container.kind].append(container)
    for container in revised_containers:
        revised_by_kind[container.kind].append(container)

    merged_sections: list[RedlineSection] = []
    merged_document_sections: list[RedlineSection] = []
    structure_kinds: set[str] = set()
    container_order: list[str] = []
    for container in [*original_containers, *revised_containers]:
        if container.kind not in container_order:
            container_order.append(container.kind)

    for kind in container_order:
        original_group = original_by_kind.get(kind, [])
        revised_group = revised_by_kind.get(kind, [])
        for idx in range(max(len(original_group), len(revised_group))):
            original_blocks = _flatten_word_blocks(original_group[idx].blocks) if idx < len(original_group) else []
            revised_blocks = _flatten_word_blocks(revised_group[idx].blocks) if idx < len(revised_group) else []
            partial = build_report_from_blocks(
                original_blocks,
                revised_blocks,
                source_a=source_a,
                source_b=source_b,
                options=options,
            )
            merged_sections.extend(
                deepcopy(section)
                for section in partial.sections
                if not _is_inert_non_body_section(section, kind)
            )
            merged_document_sections.extend(
                deepcopy(section)
                for section in partial.document_sections
                if not _is_inert_non_body_section(section, kind)
            )
            structure_kinds.update(partial.structure_kinds)

    merged_sections = _reindex_sections(merged_sections)
    merged_document_sections = _reindex_sections(merged_document_sections)
    return RedlineReport(
        source_a=source_a,
        source_b=source_b,
        options=options,
        sections=merged_sections,
        document_sections=merged_document_sections,
        summary=_summarize_sections(merged_sections),
        structure_kinds=sorted(structure_kinds),
    )


def generate_report(
    original_path: Path,
    revised_path: Path,
    *,
    options: CompareOptions | None = None,
) -> RedlineReport:
    resolved = _resolve_options(options=options)
    if original_path.suffix.lower() == ".docx" and revised_path.suffix.lower() == ".docx":
        _require_docx_loading()
        original_containers, _ = _build_docx_containers(Document(original_path))
        revised_containers, _ = _build_docx_containers(Document(revised_path))
        return _build_report_from_container_sets(
            original_containers,
            revised_containers,
            source_a=original_path.name,
            source_b=revised_path.name,
            options=resolved,
        )

    original_blocks = load_document_blocks(original_path)
    revised_blocks = load_document_blocks(revised_path)
    return build_report_from_blocks(
        original_blocks,
        revised_blocks,
        source_a=original_path.name,
        source_b=revised_path.name,
        options=resolved,
    )
