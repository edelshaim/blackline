from __future__ import annotations

from typing import Any, Sequence
from .models import CompareOptions

INSERT_HEX = "0B3FAE"
DELETE_HEX = "C00000"
MOVE_HEX = "4B5F7A"
ACCENT_HEX = "E6ECF4"
BORDER_HEX = "D0D8E6"
TEXT_HEX = "111827"
MUTED_HEX = "5B6474"
SURFACE_HEX = "F7F9FC"
TRACK_AUTHOR = "blackline-tool"
SPECIAL_WORD_CONTENT_TAGS = {
    "w:commentRangeStart",
    "w:commentRangeEnd",
    "w:commentReference",
    "w:bookmarkStart",
    "w:bookmarkEnd",
    "w:fldSimple",
    "w:fldChar",
    "w:instrText",
    "w:hyperlink",
}
ANCHOR_ONLY_TAGS = {
    "w:commentRangeStart",
    "w:commentRangeEnd",
    "w:bookmarkStart",
    "w:bookmarkEnd",
}

def options_for_profile(profile_name: str) -> CompareOptions:
    normalized = profile_name.strip().lower()
    if normalized == "default":
        return CompareOptions(profile_name="default")
    if normalized == "legal":
        return CompareOptions(
            profile_name="legal",
            ignore_case=True,
            ignore_smart_punctuation=True,
            normalize_defined_terms=True,
            prefer_substantive_alignment=True,
        )
    if normalized in {"contract", "contracts"}:
        return CompareOptions(
            profile_name="contract",
            ignore_case=True,
            ignore_smart_punctuation=True,
            ignore_numbering=True,
            normalize_defined_terms=True,
            prefer_substantive_alignment=True,
        )
    if normalized == "litigation":
        return CompareOptions(
            profile_name="litigation",
            ignore_case=True,
            ignore_whitespace=True,
            ignore_smart_punctuation=True,
            ignore_numbering=True,
            normalize_defined_terms=True,
            prefer_substantive_alignment=True,
        )
    if normalized == "factum":
        return CompareOptions(
            profile_name="factum",
            ignore_case=True,
            ignore_whitespace=True,
            ignore_smart_punctuation=True,
            ignore_numbering=True,
            normalize_defined_terms=True,
            prefer_substantive_alignment=True,
        )
    if normalized == "presentation":
        return CompareOptions(
            profile_name="presentation",
            ignore_case=True,
            ignore_whitespace=True,
            ignore_smart_punctuation=True,
            ignore_punctuation=True,
            prefer_substantive_alignment=True,
        )
    raise ValueError(f"Unsupported comparison profile: {profile_name}")


def _resolve_options(
    *,
    options: CompareOptions | None = None,
    substantive_only: bool = False,
) -> CompareOptions:
    if options is not None:
        return options
    if substantive_only:
        return options_for_profile("legal")
    return options_for_profile("default")


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[idx:idx + 2], 16) for idx in (0, 2, 4))


def active_rule_labels(options: CompareOptions) -> list[str]:
    labels: list[str] = []
    if options.ignore_case:
        labels.append("ignore case")
    if options.ignore_whitespace:
        labels.append("ignore whitespace-only edits")
    if options.ignore_smart_punctuation:
        labels.append("normalize smart punctuation")
    if options.ignore_punctuation:
        labels.append("ignore punctuation-only edits")
    if options.ignore_numbering:
        labels.append("ignore numbering token changes")
    if options.normalize_defined_terms:
        labels.append("normalize defined terms")
    if options.prefer_substantive_alignment:
        labels.append("prefer substantive alignment")
    return labels


def report_profile_summary(options: CompareOptions) -> str:
    labels = active_rule_labels(options)
    rules = ", ".join(labels) if labels else "no normalization rules"
    move_text = "move detection on" if options.detect_moves else "move detection off"
    return f"Profile: {options.profile_name} ({rules}; {move_text})"


def _section_kind_label(kind: str) -> str:
    if kind == "equal":
        return "No Change"
    if kind == "insert":
        return "Insertion"
    if kind == "delete":
        return "Deletion"
    if kind == "replace":
        return "Replacement"
    if kind == "move":
        return "Move"
    return kind.title()


def _section_location_kind(container: str) -> str:
    if container == "body":
        return "body"
    if container.startswith("textbox:"):
        return "textbox"
    if container.startswith("footnote:"):
        return "footnote"
    if container.startswith("endnote:"):
        return "endnote"
    if "header" in container.casefold():
        return "header"
    if "footer" in container.casefold():
        return "footer"
    return "body"


def _pdf_rgb(hex_color: str):
    from reportlab.lib.colors import Color
    r, g, b = _hex_to_rgb(hex_color)
    return Color(r / 255, g / 255, b / 255)
