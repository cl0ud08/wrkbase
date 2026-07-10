"""Semantic duplicate detection: given draft ticket text, find existing
tickets in the same project whose stored embedding is similar enough to
be worth a human's attention. See app/api/tickets.py's check_duplicates
endpoint for how this is surfaced, and app/services/ticket_embedding.py
for how the embeddings being compared here are generated and stored.

--- Why this is synchronous, not another async job --------------------

Same "is there a waiting caller" test already established for ticket
parsing (see app/api/tickets.py's parse_ticket docstring): a user is
reviewing a draft ticket right now, deciding whether to actually create
it, and a possible-duplicate warning only means anything if it's part
of that decision. Surfacing it after the fact, once the ticket already
exists, would be too late for anyone to "reconsider" — the exact
framing point 5 of this slice's brief used. Generating a *new* ticket's
own embedding is a different, already-async concern
(worker/main.py's _process_embed_job) — this function never writes
anything at all; it only reads other tickets' already-stored embeddings
and generates one throwaway vector for the draft text to compare
against them, discarded the moment the request finishes.

--- Scoping: the same composite-FK-equivalent discipline as every
    other query in this app, applied to a query shape that's genuinely
    new here ---------------------------------------------------------

Every prior query in this app scoping "which tickets" has been a plain
equality filter (project_id = :x, sprint_id = :y). This is the first
one that orders by a *distance*, not an equality — worth checking
explicitly, not just assumed to inherit the same guarantees for free.
It doesn't change anything about the safety story, though: RLS already
restricts every row this session can see to the caller's own org (set
once per request by get_current_auth, same as every endpoint in this
file), and `project_id = :project_id` is the same explicit,
defense-in-depth filter _get_ticket_or_404 and friends already apply on
top of RLS rather than trusting it alone — a project id from a
*different* project in the *same* org must still never leak in here,
which RLS's org-only scoping wouldn't catch by itself. `deleted_at IS
NULL` matches every other ticket read in this file. None of that is
specific to vector search; ORDER BY embedding <=> :query_vector is just
what decides the order and cutoff *within* that already-correctly
-scoped row set, no differently in principle than ORDER BY created_at
elsewhere. Proven directly, not just reasoned about — see
scripts/verify_duplicate_detection.py's cross-org and cross-project
checks.

--- The threshold -------------------------------------------------

Not guessed: tuned empirically against real Gemini embeddings of 13
real ticket-text pairs — see scripts/tune_duplicate_threshold.py for
the full run. Three clusters emerged: genuinely similar pairs (same
bug/feature, reworded) scored 0.8351-0.9185; genuinely unrelated pairs
scored 0.4632-0.5207 -- a huge, unambiguous gap between the two. A
third cluster, pairs in the *same category but a different specific
issue* ("password reset email never arrives" vs. "verification email
never arrives"; "export to CSV" vs. "export to PDF"), scored across a
wide 0.7921-0.9300 range that genuinely overlaps both of the other two
-- confirming that no fixed threshold on topic-level semantic
similarity can perfectly distinguish "the same underlying request,
reworded" from "a closely related but genuinely different one." That's
not a bug in this approach, it's what point 5 of this feature's own
brief already planned for: a non-blocking warning, not a hard gate, so
an occasional flag on a merely-related (not actually duplicate) ticket
costs a human one glance, not a blocked action. 0.83 sits just below
the observed similar-pairs floor (0.8351) -- catching every tested
genuine duplicate -- and enormously above the unrelated-pairs ceiling
(0.5207, a >0.3 margin) -- excluding every tested unrelated pair --
while landing inside the ambiguous same-category cluster's own real
spread (0.7921-0.9300), which is exactly where a threshold *should*
land given that cluster genuinely straddles both the "similar" and
"different" scores observed elsewhere.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket
from app.services.ticket_embedding import generate_embedding

# A cosine *similarity* (1 - pgvector's <=> distance), not the raw
# distance itself -- 1.0 is identical direction, 0.0 is orthogonal.
# See this module's own docstring and scripts/tune_duplicate_threshold.py.
SIMILARITY_THRESHOLD = 0.83

# A courtesy cap on how many possible duplicates get returned, not a
# correctness boundary -- the SQL WHERE clause below already does the
# real filtering (similarity >= SIMILARITY_THRESHOLD); this just keeps
# the response small if an unusually large number of tickets happen to
# clear that bar.
_MAX_CANDIDATES = 5


@dataclass
class DuplicateMatch:
    ticket: Ticket
    similarity: float


async def find_possible_duplicates(
    db: AsyncSession, *, project_id: uuid.UUID, title: str, description: str | None
) -> list[DuplicateMatch]:
    """Raises ticket_embedding.EmbeddingFailed if the draft text itself
    can't be embedded (both Gemini attempts exhausted) -- a real
    provider failure, left for the caller to translate into a 503, same
    "infra failure vs. an honest negative result" distinction already
    established for ticket parsing. An empty *result* list (the call
    succeeded, nothing scored above threshold) is not an error.
    """
    query_vector = await generate_embedding(title, description)

    distance = Ticket.embedding.cosine_distance(query_vector)
    similarity = (1 - distance).label("similarity")

    result = await db.execute(
        select(Ticket, similarity)
        .where(
            Ticket.project_id == project_id,
            Ticket.deleted_at.is_(None),
            Ticket.embedding.is_not(None),
            distance <= (1 - SIMILARITY_THRESHOLD),
        )
        .order_by(distance)
        .limit(_MAX_CANDIDATES)
    )
    return [DuplicateMatch(ticket=ticket, similarity=float(sim)) for ticket, sim in result.all()]
