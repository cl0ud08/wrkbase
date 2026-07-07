"""fix tenant_isolation policy: treat '' same as NULL for app.current_org_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-13

"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # Bug found while building the auth slice: the *first* SET LOCAL of a
    # custom (never-before-referenced) GUC on a given physical connection,
    # once its transaction ends without an explicit COMMIT/ROLLBACK (e.g. a
    # read-only request that never calls session.commit()), resets to an
    # empty string '' on that connection — not to true NULL like every
    # subsequent reset does. `''::uuid` raises a cast error instead of
    # comparing as "no match", which turned a request-ending edge case into
    # a 500 instead of the intended default-deny. NULLIF(..., '') collapses
    # both "never set" (NULL) and "reset to empty" ('') to the same NULL
    # before the cast, so both cases behave identically: zero rows, no error.
    bind.execute(text("DROP POLICY IF EXISTS tenant_isolation ON users"))
    bind.execute(
        text(
            """
            CREATE POLICY tenant_isolation ON users
            USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(text("DROP POLICY IF EXISTS tenant_isolation ON users"))
    bind.execute(
        text(
            """
            CREATE POLICY tenant_isolation ON users
            USING (org_id = current_setting('app.current_org_id', true)::uuid)
            WITH CHECK (org_id = current_setting('app.current_org_id', true)::uuid)
            """
        )
    )
