"""Finite, route-bound Cursor presentation aliases."""

REPORTED_MODEL_ALIASES = {
    ("gpt-5.6-sol-high", "GPT-5.6 Sol 272K High"): frozenset(
        {"GPT-5.6 Sol 1M High"}
    ),
}


def approved_reported_models(selection: str, canonical: str) -> frozenset[str]:
    vendor_aliases = (
        frozenset({f"Claude {canonical}"})
        if selection.startswith("claude-") and canonical
        else frozenset()
    )
    return frozenset((selection, canonical)) | vendor_aliases | REPORTED_MODEL_ALIASES.get(
        (selection, canonical), frozenset()
    )
