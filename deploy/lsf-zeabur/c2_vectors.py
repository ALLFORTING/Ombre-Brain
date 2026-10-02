"""C2-only deterministic provider inputs; never supplies business search results."""
import re
import math

def c2_input(text):
    return bool(re.search(r"\bC2(?:[- ]|PAGE\b|STATE\b|HISTORY\b|FEEL\b|SESSION\b|EMERGE\b|LETTER\b)", text))

def vector(text):
    if not c2_input(text):
        return [1.0, 0.0, 0.0, 0.0]
    # Separate C2 vectors from legacy e0. Page corpus deliberately shares e1.
    if "C2PAGE" in text or "C2-PAGE" in text:
        return [0.0, 1.0, 0.0, 0.0]
    if "C2STATE" in text or "C2HISTORY" in text:
        return [0.0, 0.0, 1.0, 0.0]
    if "C2-LEX-05" in text:
        return [0.0, 0.0, 0.6, 0.8]
    if "C2-LEX-06" in text:
        return [0.0, 0.0, 1.0, 0.0]
    return [0.0, 0.0, 0.0, 1.0]

def valid_unit(value):
    return len(value) == 4 and all(math.isfinite(v) for v in value) and abs(sum(v*v for v in value)-1) < 1e-9
