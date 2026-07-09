"""Consumes ticket-triage jobs off RabbitMQ, calling a real LLM (Groq
first, Gemini as fallback — see app/services/llm_triage.py) to classify
each ticket.

Run: python -m worker.main (see docker-compose.yml's worker service —
same image and codebase as the api service, just a different command).

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

import aio_pika
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Ticket, TriageStatus
from app.db.session import AsyncSessionLocal, set_tenant_context
from app.services.llm_triage import TriageFailed, triage_ticket
from app.services.queue import TRIAGE_QUEUE_NAME, TriageJob
from app.services.ticket_events import SYSTEM_ACTOR_ID, publish_ticket_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker.triage")


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


async def main() -> None:
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        # See module docstring's "two workers running at once" section —
        # without this, one worker can starve every other one instead of
        # the queue's work being shared fairly.
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(TRIAGE_QUEUE_NAME, durable=True)

        logger.info("worker started, consuming %r", TRIAGE_QUEUE_NAME)

        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                try:
                    job = TriageJob.model_validate_json(message.body)
                except Exception:
                    logger.exception("unparseable message, rejecting without requeue")
                    await message.nack(requeue=False)
                    continue

                try:
                    await _process_job(job)
                except LookupError:
                    logger.warning("poison message for ticket=%s, rejecting without requeue", job.ticket_id)
                    await message.nack(requeue=False)
                except Exception:
                    logger.exception("transient failure processing ticket=%s, requeueing", job.ticket_id)
                    await message.nack(requeue=True)
                else:
                    await message.ack()


if __name__ == "__main__":
    asyncio.run(main())
