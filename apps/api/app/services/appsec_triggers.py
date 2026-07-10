"""The AI Security Champion's trigger library — a small, explicit,
human-curated set of security-relevant categories, kept as data, not as
an open-ended "ask an LLM if this ticket is security-relevant" prompt.

That's a deliberate scoping decision, not a limitation accepted for lack
of a better idea. An open-ended prompt ("does this ticket have security
implications?") would call an LLM on every single ticket to answer a
question with genuinely unbounded scope and no fixed vocabulary to
audit — two different reviewers (or the same model on two different
days) could reasonably disagree about what counts, and there would be
no fixed list a security team could look at and say "these are the
categories this system watches for, and here's what it checks for
each." A small, explicit category list is the opposite of that: cheap
to run (no LLM call at all for the gate itself — see match_triggers
below), fully deterministic (the same ticket text always produces the
same match), and auditable in the way that matters for a security
process specifically — someone can read this file and know exactly
what will and won't get flagged, and extend it by adding a keyword or a
whole new category, not by hoping a prompt phrasing tweak generalizes
correctly.

See app/services/appsec_review.py for what happens once a category
matches — a real LLM call, but only then, and only to write a tailored
comment applying the matched category's own checklist to what the
ticket actually says, not to decide whether the ticket is relevant in
the first place.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TriggerCategory:
    key: str
    label: str
    # Substrings matched case-insensitively against "title\n\ndescription".
    # Deliberately plain substrings, not regex or word-boundary matching:
    # a false positive here just means one extra (cheap) LLM call is spent
    # writing a security note a human can skim and dismiss in seconds; a
    # false negative means a real security-relevant ticket goes completely
    # unflagged. That asymmetry is why the keyword lists below lean
    # slightly wide rather than slightly precise.
    keywords: tuple[str, ...]
    checklist: tuple[str, ...]


TRIGGER_CATEGORIES: tuple[TriggerCategory, ...] = (
    TriggerCategory(
        key="file_upload",
        label="File upload",
        keywords=(
            "upload", "attachment", "attach a file", "import file", "import csv",
            "csv import", "image upload", "profile picture", "avatar", "file input",
        ),
        checklist=(
            "Validate file type by inspecting actual content (magic bytes), "
            "not just the file extension or the client-supplied MIME type.",
            "Scan uploaded files for malicious payloads before they become "
            "accessible to any other user or process.",
            "Restrict execution permissions on the upload directory — an "
            "uploaded file should never be executable.",
            "Check for path traversal in the filename (reject `../`, absolute "
            "paths, null bytes, and other characters that could escape the "
            "intended storage location).",
            "Enforce a maximum file size to prevent resource-exhaustion via "
            "oversized uploads.",
            "Serve uploaded files with a safe, fixed Content-Type and "
            "Content-Disposition rather than trusting whatever the uploader "
            "claimed, and store them outside the application's own webroot.",
        ),
    ),
    TriggerCategory(
        key="auth_permission",
        label="Auth / permission changes",
        keywords=(
            "login", "logout", "authentication", "auth flow", "session",
            "password", "2fa", "mfa", "sso", "oauth", "access token",
            "refresh token", "permission check", "role check", "access control",
            "rbac",
        ),
        checklist=(
            "Confirm the change doesn't weaken an existing authorization "
            "check — explicitly test the negative case (a request without "
            "proper credentials or role) still gets rejected.",
            "If this touches session or token handling, confirm tokens are "
            "actually invalidated server-side on logout or password change, "
            "not just discarded client-side.",
            "Check for a privilege-escalation path: can a lower-privileged "
            "role now reach a code path it previously couldn't?",
            "If this touches a credential-guessing surface (login, password "
            "reset, token validation), confirm rate limiting still applies.",
            "Confirm tenant/RLS scoping still holds for any new or modified "
            "query this change touches.",
        ),
    ),
    TriggerCategory(
        key="payment_pii",
        label="Payment / PII handling",
        keywords=(
            "payment", "billing", "credit card", "invoice", "subscription",
            "stripe", "pii", "personal data", "social security", "date of birth",
            "home address", "phone number", "gdpr", "export user data",
        ),
        checklist=(
            "Confirm no sensitive value (card numbers, government IDs, full "
            "payment details) is ever written to logs.",
            "Confirm this data is encrypted at rest and in transit if it "
            "wasn't already.",
            "Check retention: is there a defined deletion or expiry policy "
            "for this data, or does it persist indefinitely by default?",
            "Confirm access to this data is itself tenant-scoped and "
            "role-gated, not broadly readable by anyone authenticated.",
            "If a third-party payment processor is involved, confirm this "
            "app never handles raw card data directly (stay out of PCI scope).",
        ),
    ),
    TriggerCategory(
        key="external_api",
        label="External API calls",
        keywords=(
            "integrate", "integration", "webhook", "third-party api",
            "external api", "call out to", "api key", "external service",
        ),
        checklist=(
            "Confirm the external service's credentials are stored as real "
            "secrets, never hardcoded or logged in plaintext.",
            "Validate and sanitize any data received back from the external "
            "service before trusting or storing it.",
            "Set a timeout and a bounded retry budget on the outbound call — "
            "an unbounded call to a third party is a real availability risk "
            "for this app, the same class of guardrail already required for "
            "every LLM call this app makes.",
            "Confirm this app isn't over-sharing its own data with the "
            "external service — send only what the integration actually needs.",
            "If this is an inbound webhook, verify its signature or other "
            "authenticity proof before acting on the payload.",
        ),
    ),
    TriggerCategory(
        key="admin_permission",
        label="Admin permission changes",
        keywords=(
            "admin role", "make admin", "grant admin", "superuser", "privilege",
            "escalation", "elevate", "admin access", "impersonate", "admin only",
        ),
        checklist=(
            "Confirm only an existing admin can grant admin — there must be "
            "no path for a member to self-elevate.",
            "Log every admin-role change (who granted it, to whom, when) for "
            "audit purposes.",
            "If this touches role removal, confirm a last-admin-removal guard "
            "(or equivalent) still holds.",
            "Double check this doesn't accidentally expose an org-wide "
            "action behind what's actually only a project-scoped permission "
            "check.",
            "Consider whether an action this sensitive should require "
            "re-authentication (step-up auth), not just an existing session.",
        ),
    ),
)

_CATEGORIES_BY_KEY = {c.key: c for c in TRIGGER_CATEGORIES}


def category_by_key(key: str) -> TriggerCategory:
    return _CATEGORIES_BY_KEY[key]


def match_triggers(title: str, description: str | None) -> list[TriggerCategory]:
    """Pure, synchronous, deterministic — no I/O, no LLM call. Cheap enough
    to run inline in the request handler (see app/api/tickets.py) so that
    the ~majority of tickets with zero security surface never publish a
    job at all, rather than publishing unconditionally and paying an LLM
    call (or even just a queue round trip) to find out there was nothing
    to say.
    """
    text = f"{title}\n\n{description}" if description else title
    text = text.lower()
    return [category for category in TRIGGER_CATEGORIES if any(kw in text for kw in category.keywords)]
