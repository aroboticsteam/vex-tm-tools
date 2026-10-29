"""Pydantic schemas for button pages (issue #11)."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"


class ButtonBase(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    color: str = Field(default="#374151", pattern=COLOR_PATTERN)
    position: int = Field(default=0, ge=0, le=1000)
    action_yaml: str = Field(..., min_length=1)


class ButtonCreate(ButtonBase):
    pass


class ButtonUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    position: int | None = Field(default=None, ge=0, le=1000)
    action_yaml: str | None = Field(default=None, min_length=1)


class ButtonRead(ButtonBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    page_id: UUID


class PageBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    position: int = Field(default=0, ge=0, le=1000)
    columns: int = Field(default=4, ge=1, le=12)


class PageCreate(PageBase):
    pass


class PageUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    position: int | None = Field(default=None, ge=0, le=1000)
    columns: int | None = Field(default=None, ge=1, le=12)


class PageRead(PageBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class PageWithButtons(PageRead):
    buttons: list[ButtonRead] = []


class PressResult(BaseModel):
    status: str
    error: str | None = None
    failed_action_index: int | None = None
    actions_executed: int = 0
    action_results: list[dict[str, Any]] = []
