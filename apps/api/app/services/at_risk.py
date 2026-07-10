"""At-risk ticket detection: a small, explicit, human-curated rule-based
scoring pass over tickets in a project's active sprint -- the same
"small explicit library, not an open-ended thing" discipline as
app/services/appsec_triggers.py, applied to a different kind of input
(structured ticket/sprint fields instead of free-text keyword matching).

--- What's honestly available to score against ------------------------

The same data audit already done for the sprint-retro slice applies
again here, unchanged: no state-change/activity-log table exists
anywhere in this codebase (checked directly against every model in
app/db/models.py, not assumed), and Ticket has no started_at or
per-workflow-transition timestamp -- only created_at, updated_at,
triaged_at, and appsec_reviewed_at exist, and none of the last two are
about board/sprint movement.

The roadmap for this slice assumed a "stale, no updates" signal built
on Ticket.updated_at. That column's DB trigger (migrations 0004/0005,
tickets_set_updated_at) is unconditional -- BEFORE UPDATE, no WHEN
clause -- confirmed by reading the trigger SQL directly, not assumed
from its name. It fires on ANY write to a tickets row, including the
triage worker setting triage_status/priority/labels, the embed worker
setting embedding, and the appsec worker setting appsec_review_status/
appsec_comment. So updated_at does NOT mean "a human last touched this
ticket at this time" -- it means "something last touched this row at
this time," human or otherwise.

That sounds disqualifying, but tracing through *when* those async jobs
actually fire changes the picture: TriageJob, EmbedJob, and AppSecJob
are all published synchronously, only from create_ticket or
update_ticket (see app/api/tickets.py), and only ever as an immediate
reaction to a human action -- never on a schedule, never independently,
never a second time later on their own. So the gap between "a human
edited this ticket" and "the resulting worker write lands" is bounded
by ordinary job-processing latency (seconds, occasionally longer under
load), never days. At the day-granularity this feature actually cares
about (is a ticket in an active sprint stale by *days*, not by
minutes), that latency is noise. updated_at is therefore used here as a
real, if slightly imprecise, "time since any activity, human-initiated
or its immediate automated follow-on" signal -- not silently trusted as
if the trigger were scoped to human edits only, and not discarded
either, since discarding it would throw away real signal over a
corruption window that, traced through, turns out not to matter at the
timescale this feature operates on. Reworking the trigger itself (a
WHEN clause excluding AI-pipeline-owned columns) was considered and
rejected: it would change updated_at's semantics for the whole app, a
strictly bigger and riskier change than this feature needs, for a
precision improvement that wouldn't change any day-granularity result.

--- The three signals, and why each is honest given the above ---------

1. UNOWNED_AND_NOT_STARTED: assignee_id IS NULL and the ticket is still
   in the project's *earliest* workflow column (lowest `order` --
   the same "no explicit is_first/is_done flag, `order` is the
   heuristic stand-in" reasoning app/api/sprints.py's own
   _terminal_workflow_state_id already uses for the opposite end, not a
   new invented heuristic). Nobody has claimed it, nothing has moved.

2. STALE: no write to the ticket (see the updated_at discussion above)
   in at least STALE_DAYS_THRESHOLD days.

3. LOW_RUNWAY: the ticket is not yet in the project's terminal column,
   and the sprint has at most LOW_RUNWAY_DAYS_THRESHOLD days left
   before Sprint.end_date (zero or negative counts -- a sprint can run
   past its end_date while still ACTIVE, since nothing auto-completes
   it; that's an even stronger signal, not an edge case to exclude).

A ticket already in the terminal column is never at risk, full stop,
regardless of the three signals above -- it's done.

--- Why 2-of-3, not any single signal --------------------------------

AT_RISK_SCORE_THRESHOLD requires at least two of the three checks to
fire. Any one of these signals alone is weak and noisy on its own:
UNOWNED_AND_NOT_STARTED is completely normal on day one of a sprint;
LOW_RUNWAY is true for nearly every unfinished ticket in a sprint's
final days regardless of whether anything is actually wrong with any
one of them; STALE alone can just mean a ticket is genuinely fine and
simply hasn't needed touching. Requiring two independent signals to
co-occur is what turns three individually weak, gameable heuristics
into a real signal worth surfacing -- the same reason AppSec's flag is
additive across matched categories rather than firing loudly on the
weakest possible match.

--- Why this is a rule-based pass, not an LLM call, and why that's an
    even easier call than it was for AppSec ---------------------------

AppSec's keyword gate decided WHETHER a ticket needed review at all;
the LLM's job, once triggered, was to read genuinely unstructured free
text (the ticket's own title/description) and produce guidance that
requires actual language understanding to synthesize -- which checklist
items apply to *this* ticket's specific feature, phrased around its own
specific details. That's real synthesis work a template cannot do,
because the input is free text with no fixed shape.

At-risk detection has no such free text to interpret anywhere in its
pipeline. Every input to a risk assessment is already a clean,
structured fact this module computed itself: assignee_id is/isn't NULL,
a workflow_state_id equality check, an integer day count, a boolean.
The only thing a "narrate this in plain language" LLM step could
possibly add is turning "unowned_and_not_started=True,
days_since_update=5, days_remaining=2" into a sentence -- which a
plain f-string already does, exactly, deterministically, and instantly,
with zero risk of the model paraphrasing a number wrong or dropping a
fact under latency pressure. There is no free-text synthesis step here
for an LLM to be good at; the entire value proposition that justified
AppSec's LLM call (turning unstructured text into tailored judgment)
simply isn't present. Layered on top of the cost argument that already
applied to AppSec (most tickets aren't at risk, an LLM call on all of
them would be wasted spend) is a stronger, separate argument specific
to this feature: even for the tickets that DO trip the threshold, a
templated reason is not just cheaper than an LLM-narrated one, it is
*more reliable* for a glanceable, trust-critical board signal read
during sprint crunch -- a manager needs "2 days left" to always say
exactly 2, never a model's paraphrase of it. So this module makes zero
LLM calls, for any ticket, ever -- not "only for some," the reasoning
doesn't support even that.

--- Why there's no fifth queue, no scheduler, and no new schema -------

Every prior AI feature in this app (triage, embedding, AppSec review,
sprint retro) needed async machinery because each did something
genuinely expensive and worth caching: a real network call to an LLM
provider, with real latency and real failure modes, whose result then
needed to be stored so it wasn't repeated on every read. Risk scoring
has no such expensive step -- it's a handful of field comparisons and
one or two cheap, already-established-pattern queries (the project's
workflow-state order bounds, the project's one active sprint), on data
that's already being fetched for the exact same request regardless.
There is nothing here worth caching, and nothing here worth queuing:
computing a risk assessment costs microseconds, so computing it fresh
on every read (the same "cheap, derived, never persisted" pattern
Sprint.total_points and Sprint.points_planned already established) is
strictly better than any alternative that would need new
infrastructure to keep a stored value in sync. In particular, a
scheduled/periodic rescore -- the one option this app has zero existing
infrastructure for (no cron container, no APScheduler, no scheduled
GitHub Actions workflow anywhere in this repo, confirmed by searching,
not assumed absent) -- would introduce a real staleness window of its
own (a cached score going out of date between runs, worst exactly when
a sprint is ending fast and things change quickly) to solve a problem
that on-demand computation doesn't have in the first place. Building
that machinery here would be exactly the kind of "because the pattern
exists" over-engineering this app has avoided everywhere else. Nothing
in this module is stored on a Ticket or a Sprint row; every field it
produces is bolted onto the ORM object at read time and never survives
past that response, the same runtime-attribute pattern total_points
already uses.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Sprint, SprintStatus, Ticket, WorkflowState

# Tunable, documented constants -- same "small, explicit, adjustable"
# shape as SIMILARITY_THRESHOLD (ticket_duplicates.py) and the keyword
# lists in appsec_triggers.py, not magic numbers buried in logic.
STALE_DAYS_THRESHOLD = 3
LOW_RUNWAY_DAYS_THRESHOLD = 2
AT_RISK_SCORE_THRESHOLD = 2


@dataclass(frozen=True)
class RiskAssessment:
    at_risk: bool
    reasons: list[str]


def _unowned_and_not_started_reason(ticket: Ticket, earliest_state_id: uuid.UUID | None) -> str | None:
    if earliest_state_id is None:
        return None
    if ticket.assignee_id is None and ticket.workflow_state_id == earliest_state_id:
        return "Unassigned and not yet started"
    return None


def _stale_reason(ticket: Ticket, now: datetime) -> str | None:
    days_since_update = (now - ticket.updated_at).days
    if days_since_update >= STALE_DAYS_THRESHOLD:
        plural = "s" if days_since_update != 1 else ""
        return f"No activity in {days_since_update} day{plural}"
    return None


def _low_runway_reason(days_remaining: int) -> str | None:
    if days_remaining > LOW_RUNWAY_DAYS_THRESHOLD:
        return None
    if days_remaining < 0:
        overdue = abs(days_remaining)
        plural = "s" if overdue != 1 else ""
        return f"Sprint end date has passed ({overdue} day{plural} overdue) and this ticket isn't done"
    plural = "s" if days_remaining != 1 else ""
    return f"Only {days_remaining} day{plural} left in the sprint and not yet done"


def assess_ticket_risk(
    ticket: Ticket,
    *,
    sprint_end_date: date,
    today: date,
    now: datetime,
    earliest_state_id: uuid.UUID | None,
    terminal_state_id: uuid.UUID | None,
) -> RiskAssessment:
    """Pure -- no I/O, no ORM writes, entirely deterministic given its
    inputs. Callers (app/api/tickets.py, app/api/sprints.py) are
    responsible for confirming `ticket` actually belongs to an ACTIVE
    sprint before calling this; a ticket outside an active sprint has no
    meaningful risk assessment at all (see this module's own docstring)
    and should never reach this function in the first place.
    """
    if terminal_state_id is not None and ticket.workflow_state_id == terminal_state_id:
        return RiskAssessment(at_risk=False, reasons=[])

    days_remaining = (sprint_end_date - today).days
    reasons = [
        reason
        for reason in (
            _unowned_and_not_started_reason(ticket, earliest_state_id),
            _stale_reason(ticket, now),
            _low_runway_reason(days_remaining),
        )
        if reason is not None
    ]
    return RiskAssessment(at_risk=len(reasons) >= AT_RISK_SCORE_THRESHOLD, reasons=reasons)


async def workflow_state_bounds(
    db: AsyncSession, project_id: uuid.UUID
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """(earliest_state_id, terminal_state_id) by `order` -- the same
    heuristic already used by app/api/sprints.py's own
    _terminal_workflow_state_id (no explicit is_first/is_done flag
    exists on WorkflowState), extended here to also need the earliest
    column as a "hasn't been started" stand-in. One query for both
    bounds rather than two, since a project's workflow states are a
    handful of rows, not worth a second round trip to avoid.
    """
    result = await db.execute(
        select(WorkflowState.id)
        .where(WorkflowState.project_id == project_id)
        .order_by(WorkflowState.order)
    )
    ids = [row[0] for row in result.all()]
    if not ids:
        return None, None
    return ids[0], ids[-1]


async def find_active_sprint(db: AsyncSession, project_id: uuid.UUID) -> Sprint | None:
    # At most one row can ever come back -- uq_sprints_one_active_per_project
    # (migration 0012) enforces this at the DB level.
    result = await db.execute(
        select(Sprint).where(Sprint.project_id == project_id, Sprint.status == SprintStatus.ACTIVE)
    )
    return result.scalar_one_or_none()
