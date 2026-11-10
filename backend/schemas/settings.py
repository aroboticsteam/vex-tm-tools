"""Pydantic schemas mirroring `backend.models.settings` (plan §8)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RolePermissionBase(BaseModel):
    authentik_group: str = Field(..., max_length=100)
    permission: str = Field(..., max_length=80)


class RolePermissionRead(RolePermissionBase):
    model_config = ConfigDict(from_attributes=True)


class ApiKeyCreate(BaseModel):
    name: str = Field(..., max_length=100)
    permissions: list[str]


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    permissions: list[str]
    created_by: str | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    revoked: bool


class ApiKeyCreateResponse(ApiKeyRead):
    """Returned only once, at creation time, with the raw key (plan §B.7)."""

    raw_key: str


class SystemSettingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    value: dict[str, Any]
    updated_by: str | None = None
    updated_at: datetime


class SystemSettingUpdate(BaseModel):
    value: dict[str, Any]


class CompetitionSettingValue(BaseModel):
    """`system_settings['competition']`: which VEX program the event runs."""

    model_config = ConfigDict(extra="forbid")

    program: Literal["V5RC", "VIQRC"]
