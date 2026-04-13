from __future__ import annotations

<<<<<<< ours
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Sequence

NUMBERING_TOKEN_PATTERN = re.compile(r"^(?:\(?[0-9ivxlcdm]+\)?[.)]?|[a-z][.)])$", re.IGNORECASE)
LEADING_NUMBERING_PATTERN = re.compile(
    r"""^\s*
    (?:
        (?:section|article|paragraph|clause|tab)\s+
    )?
    (?:
        \(?[0-9ivxlcdm]+\)?(?:\.[0-9ivxlcdm]+)*(?:[.)])?
        |
        [A-Za-z](?:[.)])
    )
    (?=\s+)
    """,
    re.IGNORECASE | re.VERBOSE,
)
DEFINED_TERM_PATTERN = re.compile(r"[\"'“”‘’]([^\"'“”‘’]{2,80})[\"'“”‘’]")


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


def _normalize_smart_punctuation(text: str) -> str:
    return (
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )


def _normalize_defined_terms(text: str) -> str:
    return DEFINED_TERM_PATTERN.sub(lambda match: match.group(1), text)


def normalize_token(text: str, options: CompareOptions) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    if options.ignore_smart_punctuation:
        normalized = _normalize_smart_punctuation(normalized)
    if options.normalize_defined_terms:
        normalized = _normalize_defined_terms(normalized)
    if options.ignore_case:
        normalized = normalized.casefold()
    if options.ignore_whitespace and normalized.isspace():
        return " "
    if options.ignore_numbering and NUMBERING_TOKEN_PATTERN.fullmatch(normalized.strip()):
        return "<num>"
    if options.ignore_punctuation and normalized and all(not char.isalnum() for char in normalized):
        if normalized.isspace():
            return " " if options.ignore_whitespace else normalized
        return "<punct>"
    return normalized


def normalize_text(text: str, options: CompareOptions) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    if options.ignore_smart_punctuation:
        normalized = _normalize_smart_punctuation(normalized)
    if options.normalize_defined_terms:
        normalized = _normalize_defined_terms(normalized)
    if options.ignore_numbering:
        normalized = LEADING_NUMBERING_PATTERN.sub("", normalized, count=1)
    if options.ignore_case:
        normalized = normalized.casefold()
    return normalized


def paragraph_compare_key(text: str, options: CompareOptions) -> str:
    normalized_text = normalize_text(text, options)
    normalized_tokens = [
        normalize_token(token, options)
        for token in re.findall(r"\w+|[^\w\s]+|\s+", normalized_text)
    ]
    if options.ignore_whitespace:
        normalized_tokens = [token for token in normalized_tokens if not token.isspace()]
    return " ".join(token for token in normalized_tokens if token.strip()) or ""


def substantive_key(text: str, options: CompareOptions | None = None) -> str:
    base = options or options_for_profile("legal")
    substantive_options = replace(
        base,
        ignore_case=True,
        ignore_whitespace=True,
        ignore_smart_punctuation=True,
        ignore_numbering=True,
        normalize_defined_terms=True,
    )
    return paragraph_compare_key(text, substantive_options)


def block_alignment_key(text: str, options: CompareOptions) -> str:
    if not options.prefer_substantive_alignment:
        return paragraph_compare_key(text, options)
    return substantive_key(text, options)


def tokens_equivalent_for_strict(original_tokens: Sequence[str], revised_tokens: Sequence[str]) -> bool:
    legal_options = options_for_profile("legal")
=======
import unicodedata
from typing import Sequence


def substantive_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = (
        normalized.replace("’", "'")
        .replace("‘", "'")
        .replace("–", "-")
        .replace("—", "-")
    )
    alnum_only = "".join(char for char in normalized if char.isalnum())
    return alnum_only or normalized


def tokens_equivalent_for_strict(original_tokens: Sequence[str], revised_tokens: Sequence[str]) -> bool:
>>>>>>> theirs
    original_non_ws = [token for token in original_tokens if not token.isspace()]
    revised_non_ws = [token for token in revised_tokens if not token.isspace()]
    if len(original_non_ws) != len(revised_non_ws):
        return False
    return all(
<<<<<<< ours
        normalize_token(original_token, legal_options) == normalize_token(revised_token, legal_options)
        for original_token, revised_token in zip(original_non_ws, revised_non_ws)
    )
=======
        substantive_key(original_token) == substantive_key(revised_token)
        for original_token, revised_token in zip(original_non_ws, revised_non_ws)
    )

>>>>>>> theirs
