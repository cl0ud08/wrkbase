import re


def derive_ticket_prefix(org_name: str) -> str:
    """First 4 alphanumeric characters of an org name, uppercased — the
    prefix half of a ticket's displayed key (PREFIX-NUMBER, e.g. WRK-142).
    Same rule migration 0011 used to backfill existing orgs. No collision
    avoidance across orgs: a ticket key only ever needs to be unique
    within its own org's UI, which the per-org sequence (see
    app/api/tickets.py's _next_ticket_number) already guarantees.
    """
    alnum = re.sub(r"[^a-zA-Z0-9]", "", org_name)
    return alnum[:4].upper() or "ORG"
