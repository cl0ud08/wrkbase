"""Label-list validation shared between llm_triage.TriageResult and
ticket_parse.ParsedTicketCandidate -- both ask a model for the same kind
of "0 to a few short, lowercase, free-text labels" field and both need to
distrust it the same way, so the cleaning rule lives once, not twice.
"""

_MAX_LABELS = 5
_MAX_LABEL_LENGTH = 30


def clean_labels(value: list[str]) -> list[str]:
    cleaned = [label.strip().lower() for label in value if label.strip()]
    if len(cleaned) > _MAX_LABELS:
        raise ValueError(f"too many labels ({len(cleaned)} > {_MAX_LABELS})")
    for label in cleaned:
        if len(label) > _MAX_LABEL_LENGTH:
            raise ValueError(f"label {label!r} exceeds {_MAX_LABEL_LENGTH} characters")
    return cleaned
