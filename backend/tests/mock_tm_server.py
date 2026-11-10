"""Standalone mock VEX TM server for wire-level integration testing.

Implements just enough of the real VEX TM API surface (plan §5.3 / Wave 2a
item 3) to genuinely exercise the async `vex_tm` integration client
end-to-end:

- ``POST /oauth2/token`` — OAuth2 client-credentials grant, returns a fake
  bearer token.
- ``GET /api/divisions``, ``/api/matches/{div_id}``,
  ``/api/rankings/{div_id}/{round}``, ``/api/skills``, ``/api/teams``,
  ``/api/event`` — canned REST data. Every REST call is checked against a
  server-side recomputation of the HMAC ``StringToSign`` (plan §3.7) so a
  bad signature is genuinely rejected with 401, not rubber-stamped.
- ``WS /api/fieldsets/{id}`` — validates the same HMAC-signed headers
  during the handshake, then can be driven by tests via `push_event()` to
  send fake events (``fieldMatchAssigned``, ``matchStarted``, ...) and
  records every command the client sends back via `received_commands`.

Run standalone with: ``python -m backend.tests.mock_tm_server``
(listens on 127.0.0.1:18181 by default; override with ``MOCK_TM_PORT``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

DEFAULT_CLIENT_ID = "test-client-id"
DEFAULT_CLIENT_SECRET = "test-client-secret"
DEFAULT_API_KEY = "test-api-key-for-hmac-signing"
DEFAULT_TOKEN = "mock-bearer-token-abc123"


def _expected_signature(api_key: str, uri_path_and_query: str, token: str, host: str, date: str) -> str:
    string_to_sign = f"GET\n{uri_path_and_query}\ntoken:{token}\nhost:{host}\nx-tm-date:{date}\n"
    return hmac.new(api_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def _team(number: str, name: str, div_id: int) -> dict[str, Any]:
    """A `/api/teams` row in the documented shape."""
    return {
        "number": number,
        "name": name,
        "shortName": "",
        "school": f"{name} Robotics",
        "sponsors": "",
        "city": "Redmond",
        "state": "Washington",
        "country": "United States",
        "ageGroup": "HIGH_SCHOOL",
        "divId": div_id,
        "checkedIn": False,
    }


def create_app(
    client_id: str = DEFAULT_CLIENT_ID,
    client_secret: str = DEFAULT_CLIENT_SECRET,
    api_key: str = DEFAULT_API_KEY,
    token: str = DEFAULT_TOKEN,
) -> FastAPI:
    app = FastAPI(title="Mock VEX TM Server")
    app.state.connections = {}  # fieldset_id -> list[WebSocket]
    app.state.received_commands = []  # list[dict] sent by the client over WS

    def _verify_rest_signature(request: Request) -> str:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        presented_token = auth_header[len("Bearer ") :]
        if presented_token != token:
            raise HTTPException(status_code=401, detail="Invalid token")

        date = request.headers.get("x-tm-date")
        signature = request.headers.get("x-tm-signature")
        if not date or not signature:
            raise HTTPException(status_code=401, detail="Missing signature headers")

        host = request.headers.get("host", "")
        uri_path_and_query = request.url.path
        if request.url.query:
            uri_path_and_query += f"?{request.url.query}"

        expected = _expected_signature(api_key, uri_path_and_query, presented_token, host, date)
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Bad signature")
        return presented_token

    @app.post("/oauth2/token")
    async def oauth_token(request: Request) -> dict[str, Any]:
        auth = request.headers.get("authorization", "")
        got_id: str | None
        got_secret: str | None
        if auth.startswith("Basic "):
            decoded = base64.b64decode(auth[len("Basic ") :]).decode("utf-8")
            got_id, _, got_secret = decoded.partition(":")
        else:
            form = await request.form()
            got_id = form.get("client_id")
            got_secret = form.get("client_secret")
        if got_id != client_id or got_secret != client_secret:
            raise HTTPException(status_code=401, detail="invalid_client")
        return {"access_token": token, "token_type": "bearer", "expires_in": 3600}

    # Shapes follow docs/modules/VEX_API_DOCS.md exactly (two divisions so
    # multi-division handling is exercised).
    divisions_data = [{"name": "Science", "id": 1}, {"name": "Technology", "id": 2}]
    teams_data = [
        _team("1234A", "Team A", 1),
        _team("4321C", "Team C", 1),
        _team("5678B", "Team B", 2),
        _team("8765D", "Team D", 2),
    ]
    div_matches = {1: ("1234A", "4321C"), 2: ("5678B", "8765D")}

    @app.get("/api/divisions")
    async def divisions(request: Request) -> dict[str, Any]:
        _verify_rest_signature(request)
        return {"divisions": divisions_data}

    @app.get("/api/matches/{div_id}")
    async def matches(div_id: int, request: Request) -> dict[str, Any]:
        _verify_rest_signature(request)
        red, blue = div_matches.get(div_id, ("", ""))
        return {
            "matches": [
                {
                    "finalScore": [0, 0],
                    "matchInfo": {
                        "timeScheduled": 1688306400,
                        "state": "UNPLAYED",
                        "alliances": [{"teams": [{"number": red}]}, {"teams": [{"number": blue}]}],
                        "matchTuple": {"session": 0, "division": div_id, "round": "QUAL", "instance": 1, "match": 1},
                    },
                    "winningAlliance": 0,
                }
            ]
        }

    @app.get("/api/rankings/{div_id}/{round}")
    async def rankings(div_id: int, round: str, request: Request) -> dict[str, Any]:
        _verify_rest_signature(request)
        rows = []
        for rank, number in enumerate(div_matches.get(div_id, ()), start=1):
            rows.append(
                {
                    "rank": rank,
                    "tied": False,
                    "alliance": {"name": "", "teams": [{"number": number}]},
                    "wins": 3 if rank == 1 else 0,
                    "losses": 0 if rank == 1 else 3,
                    "ties": 0,
                    "wp": 6,
                    "ap": 10,
                    "sp": 100,
                    "avgPoints": 50.0,
                    "totalPoints": 150,
                    "highScore": 60,
                    "numMatches": 3,
                    "minNumMatches": True,
                }
            )
        return {"rankings": rows}

    @app.get("/api/skills")
    async def skills(request: Request) -> dict[str, Any]:
        _verify_rest_signature(request)
        return {
            "skillsRankings": [
                {
                    "rank": 1,
                    "tie": False,
                    "number": "1234A",
                    "totalScore": 100,
                    "progHighScore": 40,
                    "progAttempts": 2,
                    "driverHighScore": 60,
                    "driverAttempts": 2,
                }
            ]
        }

    @app.get("/api/teams")
    @app.get("/api/teams/{div_id}")
    async def teams(request: Request, div_id: int | None = None) -> dict[str, Any]:
        _verify_rest_signature(request)
        return {"teams": [t for t in teams_data if div_id is None or t["divId"] == div_id]}

    @app.get("/api/event")
    async def event(request: Request) -> dict[str, Any]:
        _verify_rest_signature(request)
        return {"event": {"name": "Mock Event", "code": "RE-MOCK-24-1234"}}

    @app.websocket("/api/fieldsets/{fieldset_id}")
    async def fieldset_ws(websocket: WebSocket, fieldset_id: int) -> None:
        auth_header = websocket.headers.get("authorization", "")
        date = websocket.headers.get("x-tm-date")
        signature = websocket.headers.get("x-tm-signature")
        host = websocket.headers.get("host", "")
        presented_token = auth_header[len("Bearer ") :] if auth_header.startswith("Bearer ") else None
        uri_path = f"/api/fieldsets/{fieldset_id}"

        valid = False
        if presented_token == token and date and signature:
            expected = _expected_signature(api_key, uri_path, presented_token, host, date)
            valid = hmac.compare_digest(expected, signature)

        if not valid:
            await websocket.close(code=4401)
            return

        await websocket.accept()
        app.state.connections.setdefault(fieldset_id, []).append(websocket)
        try:
            while True:
                data = await websocket.receive_json()
                app.state.received_commands.append(data)
        except WebSocketDisconnect:
            pass
        finally:
            conns = app.state.connections.get(fieldset_id, [])
            if websocket in conns:
                conns.remove(websocket)

    return app


async def push_event(app: FastAPI, fieldset_id: int, payload: dict[str, Any]) -> int:
    """Test helper: push a fake TM event to every WS client connected to
    ``fieldset_id``. Returns the number of clients it was sent to."""
    sent = 0
    for ws in list(app.state.connections.get(fieldset_id, [])):
        await ws.send_json(payload)
        sent += 1
    return sent


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MOCK_TM_PORT", "18181"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port)
