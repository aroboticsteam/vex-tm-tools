"""Button pages API (issue #11): a stream-deck-style set of pages of
pressable buttons. Each button stores a YAML action chain (same format as
scripts/automations, so it can call any integration service, script,
delay, ...) which is executed by the leader's `AutomationEngine` on press.

Pressing needs `buttons:press`; defining what a button does needs
`buttons:manage` (equivalent in power to editing a script, so grant it
accordingly).
"""
from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.audit import log_action
from backend.core.dependencies import CurrentPrincipal, get_db, require_permission
from backend.models.button_page import Button, ButtonPage
from backend.modules.automation.engine import validate_script_yaml
from backend.schemas.button_page import (
    ButtonCreate,
    ButtonRead,
    ButtonUpdate,
    PageCreate,
    PageRead,
    PageUpdate,
    PageWithButtons,
    PressResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/button-pages", tags=["button-pages"])


def _check_yaml(action_yaml: str) -> None:
    valid, errors = validate_script_yaml(action_yaml)
    if not valid:
        raise HTTPException(status_code=400, detail={"errors": errors})


async def _page_or_404(db: AsyncSession, page_id: UUID) -> ButtonPage:
    page = await db.get(ButtonPage, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


async def _button_or_404(db: AsyncSession, button_id: UUID) -> Button:
    button = await db.get(Button, button_id)
    if button is None:
        raise HTTPException(status_code=404, detail="Button not found")
    return button


# ── pages ────────────────────────────────────────────────────────────


@router.get("", response_model=list[PageWithButtons], dependencies=[Depends(require_permission("buttons:read"))])
async def list_pages(db: Annotated[AsyncSession, Depends(get_db)]) -> list[PageWithButtons]:
    pages = (await db.execute(select(ButtonPage).order_by(ButtonPage.position, ButtonPage.name))).scalars().all()
    buttons = (await db.execute(select(Button).order_by(Button.position, Button.label))).scalars().all()
    by_page: dict[UUID, list[Button]] = {}
    for b in buttons:
        by_page.setdefault(b.page_id, []).append(b)
    return [
        PageWithButtons(
            **PageRead.model_validate(p).model_dump(),
            buttons=[ButtonRead.model_validate(b) for b in by_page.get(p.id, [])],
        )
        for p in pages
    ]


@router.post("", response_model=PageRead, status_code=201)
async def create_page(
    body: PageCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("buttons:manage"))],
) -> PageRead:
    page = ButtonPage(**body.model_dump())
    db.add(page)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Page {body.name!r} already exists")
    await db.refresh(page)
    await log_action(db, principal.subject, "create", "button_page", str(page.id), changes=body.model_dump())
    return PageRead.model_validate(page)


@router.put("/{page_id}", response_model=PageRead)
async def update_page(
    page_id: UUID,
    body: PageUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("buttons:manage"))],
) -> PageRead:
    page = await _page_or_404(db, page_id)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(page, key, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A page with that name already exists")
    await db.refresh(page)
    await log_action(db, principal.subject, "update", "button_page", str(page_id), changes=changes)
    return PageRead.model_validate(page)


@router.delete("/{page_id}", status_code=204, response_model=None)
async def delete_page(
    page_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("buttons:manage"))],
) -> Response:
    page = await _page_or_404(db, page_id)
    await db.delete(page)
    await db.commit()
    await log_action(db, principal.subject, "delete", "button_page", str(page_id))
    return Response(status_code=204)


# ── buttons ──────────────────────────────────────────────────────────


@router.post("/{page_id}/buttons", response_model=ButtonRead, status_code=201)
async def create_button(
    page_id: UUID,
    body: ButtonCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("buttons:manage"))],
) -> ButtonRead:
    await _page_or_404(db, page_id)
    _check_yaml(body.action_yaml)
    button = Button(page_id=page_id, **body.model_dump())
    db.add(button)
    await db.commit()
    await db.refresh(button)
    await log_action(db, principal.subject, "create", "button", str(button.id), changes={"label": body.label})
    return ButtonRead.model_validate(button)


@router.put("/buttons/{button_id}", response_model=ButtonRead)
async def update_button(
    button_id: UUID,
    body: ButtonUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("buttons:manage"))],
) -> ButtonRead:
    button = await _button_or_404(db, button_id)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if "action_yaml" in changes:
        _check_yaml(changes["action_yaml"])
    for key, value in changes.items():
        setattr(button, key, value)
    await db.commit()
    await db.refresh(button)
    await log_action(db, principal.subject, "update", "button", str(button_id), changes={"fields": sorted(changes)})
    return ButtonRead.model_validate(button)


@router.delete("/buttons/{button_id}", status_code=204, response_model=None)
async def delete_button(
    button_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("buttons:manage"))],
) -> Response:
    button = await _button_or_404(db, button_id)
    await db.delete(button)
    await db.commit()
    await log_action(db, principal.subject, "delete", "button", str(button_id))
    return Response(status_code=204)


# ── press ────────────────────────────────────────────────────────────


async def _proxy_press_to_leader(request: Request, leader: Any, button_id: UUID) -> Response | None:
    """Passive-node forwarding (plan §3.1): the engine only runs on the
    leader, so forward the press there with the caller's credentials."""
    address = await leader.get_leader_address_async()
    if not address:
        return None
    headers = {k: v for k in ("authorization", "cookie") if (v := request.headers.get(k))}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"http://{address}/api/v1/button-pages/buttons/{button_id}/press", headers=headers)
    except httpx.HTTPError:
        logger.exception("Failed to proxy button press %s to leader at %s", button_id, address)
        return None
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"content-type": resp.headers.get("content-type", "application/json")},
    )


@router.post("/buttons/{button_id}/press", response_model=PressResult)
async def press_button(
    button_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[CurrentPrincipal, Depends(require_permission("buttons:press"))],
) -> Response | PressResult:
    button = await _button_or_404(db, button_id)
    engine = getattr(request.app.state, "automation_engine", None)
    if engine is None:
        leader = getattr(request.app.state, "leader", None)
        if leader is not None and not leader.is_leader():
            proxied = await _proxy_press_to_leader(request, leader, button_id)
            if proxied is not None:
                return proxied
        raise HTTPException(status_code=503, detail="Automation engine is not currently running")
    try:
        actions = yaml.safe_load(button.action_yaml) or []
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Stored action YAML is invalid: {exc}") from exc
    if not isinstance(actions, list):
        actions = [actions]
    result = await engine.run_actions(actions, button.id, {"button": button.label, "page_id": str(button.page_id)})
    await log_action(
        db, principal.subject, "press", "button", str(button_id), changes={"label": button.label, "status": result["status"]}
    )
    return PressResult(**result)
