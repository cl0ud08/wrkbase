"""Ticket-triage, ticket-embedding, AppSec-review, and sprint-retro job
publishing. worker/main.py is the consumer for all four — see its module
docstring for the ack/nack and tenant-context reasoning, and for why each
is its own queue rather than one job type doing everything.
"""

import uuid

import aio_pika
from pydantic import BaseModel

from app.core.config import settings

TRIAGE_QUEUE_NAME = "ticket_triage"
EMBED_QUEUE_NAME = "ticket_embedding"
APPSEC_QUEUE_NAME = "ticket_appsec_review"
SPRINT_RETRO_QUEUE_NAME = "sprint_retro"

_connection: aio_pika.abc.AbstractRobustConnection | None = None
_channel: aio_pika.abc.AbstractChannel | None = None


class TriageJob(BaseModel):
    """The message contract published on ticket creation and consumed by
    worker/main.py. Deliberately minimal — just enough for the worker to
    look the ticket up itself (under its own freshly-established tenant
    context, not by trusting this payload's org_id blindly; see
    worker/main.py) and to hand to an LLM. Not the full ticket: anything
    else the worker needs, it re-reads from the database at process
    time, which is also naturally always current even if the ticket was
    edited again between publish and consume.
    """

    ticket_id: uuid.UUID
    org_id: uuid.UUID
    title: str
    description: str | None = None


class EmbedJob(BaseModel):
    """Published on ticket creation, and again whenever a PATCH changes
    title or description (see app/api/tickets.py) — regenerating a
    ticket's embedding is the exact same job as generating it the first
    time, just triggered a second time. Deliberately does NOT carry
    title/description the way TriageJob does: the embed worker always
    re-reads them fresh from the ticket row at process time (see
    worker/main.py's _process_embed_job), specifically so this job is
    safe to fire on every edit without any ordering assumption. If a
    ticket is edited twice in quick succession, two EmbedJobs land on
    the queue, but whichever one actually runs last reads the ticket's
    *current* title/description and produces the correct final
    embedding regardless of which job that was — an EmbedJob is a
    trigger ("something about this ticket may have changed, re-embed
    it"), not a snapshot of what to embed.
    """

    ticket_id: uuid.UUID
    org_id: uuid.UUID


class AppSecJob(BaseModel):
    """Published only when app/services/appsec_triggers.match_triggers()
    already matched at least one category, synchronously, in the API
    request itself (see app/api/tickets.py) — unlike TriageJob and
    EmbedJob, which publish unconditionally for every ticket because
    every ticket genuinely needs a priority and an embedding. Most
    tickets have zero security surface, so most tickets never publish
    an AppSecJob at all; the queue's traffic is naturally correlated
    with "tickets that actually matched a trigger," not with ticket
    volume itself.

    Same "trigger, not snapshot" shape as EmbedJob and for the same
    reason: carries only ticket_id/org_id, not the matched categories or
    the ticket text. The worker re-runs match_triggers() itself against
    the ticket's *current* title/description at process time — both to
    stay correct if the ticket was edited again between publish and
    consume, and as the same defense-in-depth instinct that already
    governs a job's claimed org_id: this app never trusts a job payload
    for something it can cheaply re-derive from the real row instead.
    """

    ticket_id: uuid.UUID
    org_id: uuid.UUID


class SprintRetroJob(BaseModel):
    """Published once, synchronously, by complete_sprint the instant a
    sprint transitions to COMPLETED (see app/api/sprints.py), and again
    by the manual regenerate endpoint. Unlike TriageJob/EmbedJob/
    AppSecJob, this is scoped to a Sprint, not a Ticket — a genuinely
    different trigger entity and a different consumer, which is why it
    gets its own queue rather than being folded into an existing one
    (see worker/main.py's module docstring for the full "fourth
    independent queue, not a variation on an existing one" reasoning).

    Deliberately minimal — sprint_id/org_id only, the same "trigger, not
    snapshot" shape as EmbedJob/AppSecJob — but for a narrower reason
    than either: the worker does NOT re-derive which tickets were
    returned to the backlog from live ticket state, because by the time
    this job is consumed that information no longer exists there at all
    (complete_sprint's bulk UPDATE already nulled sprint_id for every
    returned ticket — see app/services/sprint_retro_context.py's own
    docstring for why). What makes re-reading fresh from the DB safe
    here anyway is that complete_sprint persists the one fact that can't
    be reconstructed later — Sprint.retro_returned_snapshot — onto the
    sprint row itself, in the same transaction that completes the
    sprint, before this job is even published. The worker re-reads the
    Sprint row fresh (never trusts a payload-carried snapshot, same
    defense-in-depth instinct as everywhere else in this app), and that
    row already has everything app/services/sprint_retro_context.py
    needs.
    """

    sprint_id: uuid.UUID
    org_id: uuid.UUID


async def _get_channel() -> aio_pika.abc.AbstractChannel:
    """Lazily connects on first publish, then reuses the same connection
    for this process's remaining life — the same "just works when
    imported" simplicity as app/core/redis.py's module-level redis_client,
    adapted for aio_pika's connect being async (unlike Redis.from_url, it
    can't run at bare module-import time). connect_robust reconnects
    automatically on a dropped connection rather than staying dead.
    """
    global _connection, _channel
    if _channel is None or _channel.is_closed:
        _connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        _channel = await _connection.channel()
        # Durable: each queue definition itself survives a RabbitMQ
        # restart. Declared here too, not just in worker/main.py, so a
        # publish still works correctly even if it's the very first thing
        # to touch a queue (whichever side starts first shouldn't
        # matter) — declaring an already-existing durable queue with the
        # same arguments is a safe no-op, not an error.
        await _channel.declare_queue(TRIAGE_QUEUE_NAME, durable=True)
        await _channel.declare_queue(EMBED_QUEUE_NAME, durable=True)
        await _channel.declare_queue(APPSEC_QUEUE_NAME, durable=True)
        await _channel.declare_queue(SPRINT_RETRO_QUEUE_NAME, durable=True)
    return _channel


async def _publish(job: BaseModel, *, routing_key: str) -> None:
    channel = await _get_channel()
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=job.model_dump_json().encode(),
            # PERSISTENT, paired with each queue's own durable=True
            # above: a durable queue holding non-persistent messages
            # still loses everything sitting in it across a broker
            # restart — both halves are needed together, not either
            # alone.
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
        ),
        # The default exchange is a direct exchange where routing_key ==
        # queue name — the standard idiom for "publish straight to one
        # named queue" when there's no need for an exchange's fan-out/
        # topic-routing flexibility, which neither job type needs.
        routing_key=routing_key,
    )


async def publish_triage_job(job: TriageJob) -> None:
    await _publish(job, routing_key=TRIAGE_QUEUE_NAME)


async def publish_embed_job(job: EmbedJob) -> None:
    await _publish(job, routing_key=EMBED_QUEUE_NAME)


async def publish_appsec_job(job: AppSecJob) -> None:
    await _publish(job, routing_key=APPSEC_QUEUE_NAME)


async def publish_sprint_retro_job(job: SprintRetroJob) -> None:
    await _publish(job, routing_key=SPRINT_RETRO_QUEUE_NAME)
