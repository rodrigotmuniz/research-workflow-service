"""Keyset ("cursor") pagination over ``(created_at, id)``, newest first.

Unlike OFFSET, a cursor stays correct when new rows are inserted between page
requests and stays fast on large tables (it uses the composite index).
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

from sqlalchemy import Select, and_, or_
from sqlalchemy.orm import Session

from rws.models import Flow, Run


class InvalidCursor(ValueError):
    pass


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created, row_id = json.loads(raw)
        return datetime.fromisoformat(created), uuid.UUID(row_id)
    except (ValueError, TypeError) as exc:
        raise InvalidCursor("invalid cursor") from exc


def paginate[M: (Flow, Run)](
    session: Session, stmt: Select[M], model: type[M], limit: int, cursor: str | None
) -> tuple[list[M], str | None]:
    """Return one page of rows and the cursor for the next page (None at the end)."""
    if cursor:
        created, row_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                model.created_at < created,
                and_(model.created_at == created, model.id < row_id),
            )
        )
    stmt = stmt.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)
    rows = list(session.scalars(stmt))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id)
    return rows, next_cursor
