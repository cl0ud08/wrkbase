"""Ticket-triage job publishing. worker/main.py is the consumer — see its
module docstring for the ack/nack and tenant-context reasoning.
"""

import uuid

import aio_pika
from pydantic import BaseModel

from app.core.config import settings

TRIAGE_QUEUE_NAME = "ticket_triage"

_connection: aio_pika.abc.AbstractRobustConnection | None = None
_channel: aio_pika.abc.AbstractChannel | None = None


class TriageJob(BaseModel):
    """The message contract published on ticket creation and consumed by
    worker/main.py. Deliberately minimal — just enough for the worker to
    look the ticket up itself (under its own freshly-established tenant
    context, not by trusting this payload's org_id blindly; see
    worker/main.py) and to hand to an LLM in the next slice. Not the full
    ticket: anything else the worker needs, it re-reads from the database
    at process time, which is also naturally always current even if the
    ticket was edited again between publish and consume.
    """

    ticket_id: uuid.UUID
    org_id: uuid.UUID
    title: str
    description: str | None = None


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
        # Durable: the queue definition itself survives a RabbitMQ
        # restart. Declared here too, not just in worker/main.py, so a
        # publish still works correctly even if it's the very first thing
        # to touch this queue (whichever side starts first shouldn't
        # matter) — declaring an already-existing durable queue with the
        # same arguments is a safe no-op, not an error.
        await _channel.declare_queue(TRIAGE_QUEUE_NAME, durable=True)
    return _channel


async def publish_triage_job(job: TriageJob) -> None:
    channel = await _get_channel()
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=job.model_dump_json().encode(),
            # PERSISTENT, paired with the queue's own durable=True above:
            # a durable queue holding non-persistent messages still loses
            # everything sitting in it across a broker restart — both
            # halves are needed together, not either alone.
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
        ),
        # The default exchange is a direct exchange where routing_key ==
        # queue name — the standard idiom for "publish straight to one
        # named queue" when there's no need for an exchange's fan-out/
        # topic-routing flexibility, which this single-queue, single-job-
        # type slice doesn't need yet.
        routing_key=TRIAGE_QUEUE_NAME,
    )
