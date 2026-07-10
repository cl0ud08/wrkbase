"""Phase 3, slice 4: semantic duplicate detection. The first real use of
pgvector since it was locked into the Postgres image in Phase 0 (Slice 1's
own docstring on why pgvector/pgvector:pg16 was chosen from day one, so
this moment wouldn't mean migrating real data onto a different base
image) — the extension itself was never actually enabled until now.

tickets.embedding is a nullable pgvector `vector(768)`, populated
asynchronously after creation (and regenerated after an edit that
changes title/description — see app/services/queue.py's EmbedJob and
worker/main.py) via Gemini's gemini-embedding-001, the only real
embedding model available to this app (Groq's client exposes an
`embeddings` attribute, inherited from the OpenAI-compatible SDK base
it's built on, but there is no actual embedding model on Groq's real
model list — confirmed with a real 404, not assumed). 768, not the
model's own 3072-dimension default: pgvector's ANN index types (HNSW,
IVFFlat) have a hard 2000-dimension ceiling, so a 3072-dim column could
never be indexed at all, only ever sequentially scanned. 768 is one of
Gemini's own documented Matryoshka-trained reduced sizes (alongside
1536), not an arbitrary truncation, and stays comfortably under that
ceiling if this project's ticket volume ever grows enough to justify an
index — see app/services/ticket_embedding.py for the full empirical
reasoning.

No index created here: at this project's real scale (a portfolio app's
handful of demo projects, not millions of tickets), a plain sequential
scan with pgvector's `<=>` cosine-distance operator is fast enough, and
building an IVFFlat/HNSW index well ahead of having representative data
to train it on would be premature — the standard pgvector guidance is to
build these indexes *after* the table has real data, not speculatively
on an empty one. 768 dimensions leaves that upgrade path open without
needing a second migration to shrink the column first, if that day ever
comes.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("tickets", sa.Column("embedding", Vector(768), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "embedding")
    # The extension itself is deliberately left enabled on downgrade —
    # dropping it is only safe if nothing else in this database could
    # possibly depend on the `vector` type, which this migration has no
    # way to guarantee stays true forever. Enabling an unused extension
    # is harmless; dropping one something else still needs is not.
