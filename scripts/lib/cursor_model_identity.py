"""Finite, route-bound Cursor presentation aliases."""

REPORTED_MODEL_ALIASES = {
    ("claude-sonnet-5-thinking-high", "Sonnet 5 300K High"): frozenset(
        {"Claude Sonnet 5 300K High"}
    ),
    ("gpt-5.6-sol-high", "GPT-5.6 Sol 272K High"): frozenset(
        {"GPT-5.6 Sol 1M High"}
    ),
}


def approved_reported_models(selection: str, canonical: str) -> frozenset[str]:
    return frozenset((selection, canonical)) | REPORTED_MODEL_ALIASES.get(
        (selection, canonical), frozenset()
    )
