"""Consumes ticket-triage jobs off RabbitMQ, calling a real LLM (Groq
first, Gemini as fallback — see app/services/llm_triage.py) to classify
each ticket; ticket-embedding jobs, calling Gemini's embedding endpoint
(see app/services/ticket_embedding.py) to generate and store a ticket's
semantic-duplicate-detection vector; and AppSec-review jobs, calling an
LLM (see app/services/appsec_review.py) to write a tailored security
review comment for tickets a cheap, deterministic keyword gate has
already flagged.

Run: python -m worker.main (see docker-compose.yml's worker service —
same image and codebase as the api service, just a different command).

--- Three queues, three jobs, not one job doing everything -------------

Triage, embedding, and AppSec review are all async for the same reason:
nobody is waiting on any of their results at ticket-creation time (see
app/api/tickets.py's create_ticket, which publishes whichever jobs apply
and returns immediately). That answers *whether* each one is a
background job; it doesn't answer whether they should share a job with
each other — a genuinely different question, decided by thinking
through what combining them would actually cost, the same way it was
for triage vs. embedding, and reapplied here rather than assumed to
transfer automatically.

If AppSec review were folded into _process_job (triage) or
_process_embed_job, the same coupling problem would recur: AppSec's own
LLM call succeeding or failing has nothing to do with whether triage or
embedding succeeded — different prompt, independent failure mode, no
shared cause. A combined job would force one ack/nack decision to cover
outcomes that don't share fate: nack-and-requeue on an AppSec hiccup
would redundantly re-run an already-succeeded triage or embedding call,
silently re-paying for LLM work nothing asked to redo. Three independent
messages (TriageJob, EmbedJob, AppSecJob, one queue each) sidestep this
exactly as before: each gets its own ack/nack lifecycle, its own retry
budget, its own terminal-failure handling, and none of their outcomes
have any bearing on each other's.

AppSec review does have one genuinely new property triage and embedding
don't share, worth stating rather than glossing over: it's the first of
the three that's *conditionally* published. TriageJob and EmbedJob go
out for every single ticket, because every ticket genuinely needs a
priority and an embedding. AppSecJob only goes out when
app/services/appsec_triggers.match_triggers() already matched a
category, synchronously, in the API request itself, before anything is
published — most tickets have zero security surface and never publish
one at all. That changes this queue's *traffic volume* relative to the
other two; it does not change the separate-queue conclusion, since the
coupling argument above is about what happens once a job exists, not
about how often one gets created.

All three still run in this one worker process, on three concurrent
consumer loops sharing one AMQP connection but separate channels (each
with its own prefetch_count=1 QoS, so one queue's traffic can never
starve another's fair share). There's no deployment-topology reason to
duplicate the container/image/Compose service just because the message
*types* are independent — the independence that actually matters here
is at the message level, not the process level, and duplicating
infrastructure to express a distinction that doesn't need it would be
the same kind of premature-abstraction mistake this app avoids
elsewhere.

--- Ack/nack: what the library actually defaults to, and why that's not
    what's used here --------------------------------------------------

aio_pika's queue consumption requires an explicit ack/nack/reject per
message unless a consumer opts into `no_ack=True` — confirmed by reading
the library's own behavior, not assumed. An unacked message is not gone:
RabbitMQ keeps it, undelivered to any other consumer, until either this
process acks it, nacks/rejects it, or the connection carrying it drops.
That default (manual ack, nothing auto-ack'd) is used here deliberately,
not merely inherited:

  - **Worker crashes mid-job** (killed, OOM, network partition) before
    ack: the connection drops, and RabbitMQ automatically requeues every
    message that was unacked on that connection — no code here has to run
    for this to happen, it's the broker's own behavior on a dropped
    connection. The job gets redelivered to another consumer (or this one
    again, once restarted). No silent data loss.
  - **A caught infrastructure exception** during processing (this process
    survives, but something failed) explicitly nacks with `requeue=True`
    below — same outcome, deliberately, rather than swallowing the error
    and acking anyway (which would silently drop a job that never
    actually ran).
  - **Acking before the DB write**, or `no_ack=True`, would have been the
    wrong default to accept: either loses the job forever on a crash
    between "received" and "processed," for a use case where losing a
    triage job silently is worse than occasionally reprocessing one.

The tradeoff this accepts: **at-least-once, not exactly-once** delivery.
If this process crashes after committing the DB update but before the
ack frame actually reaches the broker (a narrow window), the job gets
redelivered and reprocessed — now that a real LLM call sits behind this,
that means occasionally paying for a duplicate call, a real but accepted
cost, not a correctness bug. Exactly-once would need an idempotency key
and a dedup check — worth building if that cost ever actually matters,
not speculatively here.

--- A failed LLM call is a handled outcome, not an infrastructure
    failure — and never nack-and-requeue's problem to solve -----------

This is the one place this slice changes the ack/nack picture above:
`_process_job` catches `TriageFailed` (both providers exhausted their
retry budgets — see llm_triage.py) *inside itself*, writes a real
`triage_status = 'failed'` + `triage_error` to the ticket, commits,
publishes the live update, and returns normally. From the outer loop's
point of view that's success — the message gets ack'd, same as an
ordinary completed triage. Deliberately not raised up to the generic
`except Exception: nack(requeue=True)` handler below: doing that would
mean every redelivery pays for a fresh 2-attempt-per-provider budget
against a job that has already been shown, at least once, not to
succeed — an unbounded, silently expensive retry loop dressed up as
"reliability." `nack(requeue=True)` stays reserved for genuine
infrastructure failures unrelated to the LLM call itself (a DB hiccup
writing the result, a network blip) — the same class of transient
failure it was always meant for.

--- Two workers running at once: does the same job ever get processed
    twice? ---------------------------------------------------------------

No, and not by luck: RabbitMQ delivers each message in a queue to exactly
one consumer (competing consumers), round-robin across however many are
subscribed — this is the standard, documented behavior for multiple
consumers on one queue, not something this app has to implement itself.
What *is* deliberately configured here is `prefetch_count=1` (QoS) — the
default with no QoS set is for the broker to push every ready message to
whichever consumer connects first, which would starve a second worker
that starts even a moment later instead of distributing work fairly.
prefetch_count=1 means each worker only ever holds one unacked message at
a time, so multiple workers actually share the load instead of the first
one greedily draining the whole queue.

--- Tenant context for a process that outlives any single org --------

Every prior version of this question in this app had a natural boundary
to hang tenant context on: one HTTP request (get_current_auth, once, for
that request), one WebSocket connection (once, at handshake, bounded by
the same staleness ceiling as everything else). A worker has neither —
it's a loop that processes an unbounded sequence of jobs, for however
many different orgs happen to publish one, over a process lifetime that
could span days. There is no "current org" for this process in any
ambient sense; there's only ever the org named by whichever job is being
handled *right now*.

So tenant context here is set fresh, per job, from that job's own
payload — never held across jobs, never assumed from whatever the
previous job happened to be. A brand-new, short-lived AsyncSession is
opened per job (same "session scoped to exactly the work that needs it"
shape as app/api/ws.py's project-access check), and the very first thing
that happens on it is set_tenant_context(session, job.org_id).

That still leaves a real question: is a job's own claimed org_id trusted
blindly? No — the same defense-in-depth instinct behind FORCE ROW LEVEL
SECURITY and every composite FK in this app applies here too. The job
also carries ticket_id, and the very first query this worker runs is
"look up this ticket, under this org's tenant context" — an RLS-scoped
read, not a raw-by-id lookup. If the job is malformed, stale (the ticket
was deleted after publish but before consume), or — hypothetically — the
publisher had a bug and the (ticket_id, org_id) pair doesn't actually
match, that lookup returns nothing, indistinguishable from "doesn't
exist," the exact same ambiguity RLS already produces for every HTTP
endpoint in this app. That's treated as a poison message (logged,
rejected without requeue below), not retried forever: only a genuinely
transient failure — a DB hiccup, a network blip — deserves a requeue.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypeVar

import aio_pika
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AppSecReviewStatus, Ticket, TriageStatus
from app.db.session import AsyncSessionLocal, set_tenant_context
from app.services.appsec_review import AppSecReviewFailed, generate_appsec_review
from app.services.appsec_triggers import match_triggers
from app.services.llm_triage import TriageFailed, triage_ticket
from app.services.queue import (
    APPSEC_QUEUE_NAME,
    EMBED_QUEUE_NAME,
    TRIAGE_QUEUE_NAME,
    AppSecJob,
    EmbedJob,
    TriageJob,
)
from app.services.ticket_embedding import EmbeddingFailed, generate_embedding
from app.services.ticket_events import SYSTEM_ACTOR_ID, publish_ticket_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker.triage")

TJob = TypeVar("TJob", bound=BaseModel)


async def _process_job(job: TriageJob) -> None:
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, job.org_id)
        result = await session.execute(select(Ticket).where(Ticket.id == job.ticket_id))
        ticket = result.scalar_one_or_none()
        if ticket is None:
            # See module docstring: a poison message, not a transient
            # failure -- the caller of _process_job rejects without
            # requeue for this case specifically.
            raise LookupError(
                f"ticket {job.ticket_id} not found under org {job.org_id} — "
                "already deleted, or a malformed/stale job"
            )

        try:
            triage_result, provider = await triage_ticket(job.title, job.description)
        except TriageFailed as exc:
            # A handled, terminal outcome -- see module docstring's
            # "failed LLM call" section for why this returns normally
            # (ack'd) instead of propagating to the nack(requeue=True)
            # path below.
            ticket.triage_status = TriageStatus.FAILED
            ticket.triage_error = str(exc)
            await session.commit()
            await publish_ticket_update(
                project_id=ticket.project_id,
                ticket_id=ticket.id,
                changes={"triage_status": ticket.triage_status, "triage_error": ticket.triage_error},
                updated_by=SYSTEM_ACTOR_ID,
            )
            logger.error("triage failed for ticket=%s: %s", ticket.id, exc)
            return

        ticket.triage_status = TriageStatus.TRIAGED
        ticket.priority = triage_result.priority
        ticket.labels = triage_result.labels
        ticket.triage_reasoning = triage_result.reasoning
        ticket.triaged_at = datetime.now(timezone.utc)
        await session.commit()

        # Published only after the commit above actually succeeded — a
        # crash between commit and publish just means a late live update
        # (the row itself is already correct; the next GET /tickets
        # reflects it regardless), never a live update for a change that
        # didn't really happen.
        await publish_ticket_update(
            project_id=ticket.project_id,
            ticket_id=ticket.id,
            changes={
                "triage_status": ticket.triage_status,
                "priority": ticket.priority,
                "labels": ticket.labels,
                "triage_reasoning": ticket.triage_reasoning,
                "triaged_at": ticket.triaged_at,
            },
            updated_by=SYSTEM_ACTOR_ID,
        )
        logger.info(
            "triaged ticket=%s org=%s via=%s priority=%s",
            ticket.id,
            job.org_id,
            provider,
            ticket.priority.value,
        )


async def _process_embed_job(job: EmbedJob) -> None:
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, job.org_id)
        result = await session.execute(select(Ticket).where(Ticket.id == job.ticket_id))
        ticket = result.scalar_one_or_none()
        if ticket is None:
            # Same poison-message reasoning as _process_job above.
            raise LookupError(
                f"ticket {job.ticket_id} not found under org {job.org_id} — "
                "already deleted, or a malformed/stale job"
            )

        try:
            # Always re-read title/description fresh from the row just
            # looked up above, never carried on the job payload — see
            # EmbedJob's own docstring in queue.py for why that's the
            # actual point: this job is safe to fire on every edit with
            # no ordering assumption, because whichever EmbedJob for
            # this ticket happens to run last always embeds whatever the
            # ticket's title/description actually are *right now*.
            embedding = await generate_embedding(ticket.title, ticket.description)
        except EmbeddingFailed as exc:
            # A handled, terminal outcome, same shape as TriageFailed
            # above — but unlike a failed triage, there's no visible
            # ticket-level state to set here. An unembedded ticket isn't
            # stuck or broken the way a triage_status=failed ticket is
            # meant to be noticed and possibly retried by a human; it
            # simply can never surface as, or be matched against, a
            # possible duplicate until some future edit successfully
            # re-triggers this job. Logged for operational visibility,
            # not surfaced on the ticket itself.
            logger.error("embedding failed for ticket=%s: %s", ticket.id, exc)
            return

        ticket.embedding = embedding
        await session.commit()
        logger.info("embedded ticket=%s org=%s", ticket.id, job.org_id)


async def _process_appsec_job(job: AppSecJob) -> None:
    async with AsyncSessionLocal() as session:
        await set_tenant_context(session, job.org_id)
        result = await session.execute(select(Ticket).where(Ticket.id == job.ticket_id))
        ticket = result.scalar_one_or_none()
        if ticket is None:
            # Same poison-message reasoning as _process_job above.
            raise LookupError(
                f"ticket {job.ticket_id} not found under org {job.org_id} — "
                "already deleted, or a malformed/stale job"
            )

        # Re-run the keyword gate against the ticket's *current*
        # title/description, never trusting AppSecJob's payload to say
        # what matched — it doesn't even carry that (see AppSecJob's own
        # docstring). If the ticket was edited between publish and
        # consume such that nothing matches anymore, there's nothing to
        # review; a previously-set flag is never silently cleared here
        # either (see app/api/tickets.py's update_ticket for why a flag
        # is additive-only), this just means this particular job has
        # nothing new to add.
        categories = match_triggers(ticket.title, ticket.description)
        if not categories:
            logger.info("appsec job for ticket=%s found no current trigger match, skipping", ticket.id)
            return

        try:
            review, provider = await generate_appsec_review(ticket.title, ticket.description, categories)
        except AppSecReviewFailed as exc:
            # A handled, terminal outcome, same shape as TriageFailed —
            # the ticket stays flagged (appsec_review_status/categories
            # were already set synchronously at creation/edit time; see
            # app/api/tickets.py) since the security concern itself is
            # real and independent of whether the AI could produce
            # tailored prose. Only the guidance is missing, visibly.
            ticket.appsec_review_status = AppSecReviewStatus.FAILED
            ticket.appsec_review_error = str(exc)
            ticket.appsec_reviewed_at = datetime.now(timezone.utc)
            await session.commit()
            await publish_ticket_update(
                project_id=ticket.project_id,
                ticket_id=ticket.id,
                changes={
                    "appsec_review_status": ticket.appsec_review_status,
                    "appsec_review_error": ticket.appsec_review_error,
                },
                updated_by=SYSTEM_ACTOR_ID,
            )
            logger.error("appsec review failed for ticket=%s: %s", ticket.id, exc)
            return

        ticket.appsec_review_status = AppSecReviewStatus.COMPLETED
        ticket.appsec_comment = review.comment
        ticket.appsec_reviewed_at = datetime.now(timezone.utc)
        await session.commit()
        await publish_ticket_update(
            project_id=ticket.project_id,
            ticket_id=ticket.id,
            changes={
                "appsec_review_status": ticket.appsec_review_status,
                "appsec_comment": ticket.appsec_comment,
                "appsec_reviewed_at": ticket.appsec_reviewed_at,
            },
            updated_by=SYSTEM_ACTOR_ID,
        )
        logger.info(
            "appsec-reviewed ticket=%s org=%s via=%s categories=%s",
            ticket.id,
            job.org_id,
            provider,
            [c.key for c in categories],
        )


async def _consume(
    queue: aio_pika.abc.AbstractQueue,
    job_model: type[TJob],
    process: Callable[[TJob], Awaitable[None]],
) -> None:
    """Generic consume loop shared by all three queues — the ack/nack
    shape (unparseable -> reject no requeue, LookupError -> poison
    message, reject no requeue, anything else -> transient, requeue,
    success -> ack) is identical across triage, embedding, and AppSec
    review jobs alike; only the job model and the processing function
    differ. See main() for the three call sites, each on its own
    channel.
    """
    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            try:
                job = job_model.model_validate_json(message.body)
            except Exception:
                logger.exception("unparseable message on %r, rejecting without requeue", queue.name)
                await message.nack(requeue=False)
                continue

            try:
                await process(job)
            except LookupError:
                logger.warning(
                    "poison message for ticket=%s on %r, rejecting without requeue",
                    job.ticket_id,
                    queue.name,
                )
                await message.nack(requeue=False)
            except Exception:
                logger.exception(
                    "transient failure processing ticket=%s on %r, requeueing", job.ticket_id, queue.name
                )
                await message.nack(requeue=True)
            else:
                await message.ack()


async def main() -> None:
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    async with connection:
        # Separate channels, not just separate queues on one shared
        # channel — see module docstring's "three queues, three jobs"
        # section for why: each channel's own prefetch_count=1 QoS
        # applies independently, so a burst of traffic on any one queue
        # (or a slow LLM call) can never starve another's fair share of
        # this worker's attention.
        triage_channel = await connection.channel()
        embed_channel = await connection.channel()
        appsec_channel = await connection.channel()
        await triage_channel.set_qos(prefetch_count=1)
        await embed_channel.set_qos(prefetch_count=1)
        await appsec_channel.set_qos(prefetch_count=1)
        triage_queue = await triage_channel.declare_queue(TRIAGE_QUEUE_NAME, durable=True)
        embed_queue = await embed_channel.declare_queue(EMBED_QUEUE_NAME, durable=True)
        appsec_queue = await appsec_channel.declare_queue(APPSEC_QUEUE_NAME, durable=True)

        logger.info(
            "worker started, consuming %r, %r, and %r", TRIAGE_QUEUE_NAME, EMBED_QUEUE_NAME, APPSEC_QUEUE_NAME
        )

        await asyncio.gather(
            _consume(triage_queue, TriageJob, _process_job),
            _consume(embed_queue, EmbedJob, _process_embed_job),
            _consume(appsec_queue, AppSecJob, _process_appsec_job),
        )


if __name__ == "__main__":
    asyncio.run(main())
