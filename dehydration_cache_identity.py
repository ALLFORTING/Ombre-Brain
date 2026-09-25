"""Stable identity for the current, versioned dehydration cache."""

import hashlib


DEHYDRATE_PROMPT_VERSION = "r123-v1"


def dehydration_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
