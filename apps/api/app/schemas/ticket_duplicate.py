import uuid

from pydantic import BaseModel, Field


class DuplicateCheckRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None


class DuplicateCandidate(BaseModel):
    ticket_id: uuid.UUID
    ticket_number: int
    title: str
    # 1.0 = identical direction, 0.0 = orthogonal -- see
    # app/services/ticket_duplicates.py for the threshold this was
    # already filtered against before ever reaching this response.
    similarity: float


class DuplicateCheckResponse(BaseModel):
    matches: list[DuplicateCandidate]
