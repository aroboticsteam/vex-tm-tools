"""Pydantic schemas mirroring `backend.models.overlay` (plan §8)."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OverlayInstanceBase(BaseModel):
    entity_id: str = Field(..., max_length=100)
    display_name: str = Field(..., max_length=150)
    field_set_id: int
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True


class OverlayInstanceCreate(OverlayInstanceBase):
    pass


class OverlayInstanceUpdate(BaseModel):
    display_name: str | None = None
    field_set_id: int | None = None
    tags: list[str] | None = None
    enabled: bool | None = None


class OverlayInstanceRead(OverlayInstanceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class ShowTeamRequest(BaseModel):
    """`team: null` clears the spotlight and returns to the match view."""

    team: str | None = Field(default=None, max_length=20, pattern=r"^[A-Za-z0-9]+$")
