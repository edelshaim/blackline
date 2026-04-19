from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Sequence, Iterable
from dataclasses import replace

from .models import Token, CompareOptions

WORD_PATTERN = re.compile(r"\w+|[^\w\s]+|\s+")
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


def tokenize_words(text: str) -> list[str]:
    return WORD_PATTERN.findall(text)


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
        for token in tokenize_words(normalized_text)
    ]
    if options.ignore_whitespace:
        normalized_tokens = [token for token in normalized_tokens if not token.isspace()]
    return " ".join(token for token in normalized_tokens if token.strip()) or ""


def substantive_key(text: str, options: CompareOptions | None = None) -> str:
    from .utils import options_for_profile
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
    from .utils import options_for_profile
    legal_options = options_for_profile("legal")
    original_non_ws = [token for token in original_tokens if not token.isspace()]
    revised_non_ws = [token for token in revised_tokens if not token.isspace()]
    if len(original_non_ws) != len(revised_non_ws):
        return False
    return all(
        normalize_token(original_token, legal_options) == normalize_token(revised_token, legal_options)
        for original_token, revised_token in zip(original_non_ws, revised_non_ws)
    )


def _simple_tokens(text: str, kind: str) -> list[Token]:
    if not text:
        return [Token("", kind)]
    return [Token(token, kind) for token in tokenize_words(text)]


def diff_words(
    original: str,
    revised: str,
    *,
    substantive_only: bool = False,
    options: CompareOptions | None = None,
) -> list[Token]:
    from .utils import _resolve_options
    resolved = _resolve_options(options=options, substantive_only=substantive_only)
    original_tokens = tokenize_words(original)
    revised_tokens = tokenize_words(revised)
    original_keys = [normalize_token(token, resolved) for token in original_tokens]
    revised_keys = [normalize_token(token, resolved) for token in revised_tokens]
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
            if tokens_equivalent_for_strict(original_chunk, revised_chunk) and resolved.profile_name == "legal":
                output.extend(Token(token, "equal") for token in revised_chunk)
            else:
                output.extend(Token(token, "delete") for token in original_chunk)
                output.extend(Token(token, "insert") for token in revised_chunk)

    return output
