from __future__ import annotations

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
    original_non_ws = [token for token in original_tokens if not token.isspace()]
    revised_non_ws = [token for token in revised_tokens if not token.isspace()]
    if len(original_non_ws) != len(revised_non_ws):
        return False
    return all(
        substantive_key(original_token) == substantive_key(revised_token)
        for original_token, revised_token in zip(original_non_ws, revised_non_ws)
    )

