from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Sequence, Iterable


@dataclass(slots=True)
class CompareOptions:
    profile_name: str = "default"
    ignore_case: bool = False
    ignore_whitespace: bool = False
    ignore_smart_punctuation: bool = False
    ignore_punctuation: bool = False
    ignore_numbering: bool = False
    normalize_defined_terms: bool = False
    prefer_substantive_alignment: bool = False
    detect_moves: bool = True


@dataclass(slots=True)
class Token:
    text: str
    kind: str  # "equal", "insert", "delete"


@dataclass(slots=True)
class RedlineParagraph:
    tokens: list[Token]


@dataclass(slots=True)
class DocumentBlock:
    label: str
    text: str
    kind: str
    style_name: str | None = None
    alignment: int | None = None
    layout: dict[str, int | float | bool | None] = field(default_factory=dict)
    container: str = "body"
    path: str | None = None


@dataclass(slots=True)
class RedlineSection:
    index: int
    label: str
    kind: str
    block_kind: str
    original_text: str
    revised_text: str
    combined_tokens: list[Token]
    original_tokens: list[Token]
    revised_tokens: list[Token]
    container: str = "body"
    location_kind: str = "body"
    change_facets: list[str] = field(default_factory=list)
    format_change_facets: list[str] = field(default_factory=list)
    style_name: str | None = None
    alignment: int | None = None
    original_label: str | None = None
    revised_label: str | None = None
    move_from_label: str | None = None
    move_to_label: str | None = None
    kind_label: str = ""

    @property
    def is_changed(self) -> bool:
        return self.kind != "equal"


@dataclass(slots=True)
class ReportSummary:
    total_sections: int
    changed_sections: int
    unchanged_sections: int
    inserted_sections: int
    deleted_sections: int
    replaced_sections: int
    moved_sections: int


@dataclass(slots=True)
class RedlineReport:
    source_a: str
    source_b: str
    options: CompareOptions
    sections: list[RedlineSection]
    document_sections: list[RedlineSection]
    summary: ReportSummary
    structure_kinds: list[str] = field(default_factory=list)

    @property
    def changed_sections(self) -> list[RedlineSection]:
        return [section for section in self.sections if section.is_changed]
