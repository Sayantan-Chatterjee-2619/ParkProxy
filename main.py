"""ParkProxy - Private Audio Calling Application

A zero-dependency, privacy-focused WebRTC audio call app that allows two people
to communicate over a web browser using a unique room URL without revealing phone
numbers, IP addresses, or personal data.

Signaling is handled via WebSockets. Each room is limited to a maximum of 2
active WebSocket connections.

Run locally:
    python -m venv venv
    venv\\Scripts\\activate        # Windows
    # or source venv/bin/activate  # macOS/Linux
    pip install -r requirements.txt
    uvicorn main:app --reload --host 0.0.0.0 --port 8000

Deploy to Render:
    1. Push this code to a GitHub repository.
    2. Create a new Web Service on Render connected to the repo.
    3. Set the build command:   pip install -r requirements.txt
    4. Set the start command:    uvicorn main:app --host 0.0.0.0 --port $PORT
    5. Add a runtime environment variable if needed (PORT is set by Render).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("parkproxy")

# ---------------------------------------------------------------------------
# FastAPI app & Jinja2 templates
# ---------------------------------------------------------------------------
app = FastAPI(
    title="ParkProxy - Private Audio Calling",
    description="Privacy-focused WebRTC audio calls via unique room URLs.",
    version="1.0.0",
)

templates = Jinja2Templates(directory="templates")

# ---------------------------------------------------------------------------
# Room management
#
# Each room stores:
#   connections: dict[websocket_id, WebSocket]  -- max 2 entries
#   created_at: float
# ---------------------------------------------------------------------------
class Room:
    __slots__ = ("room_id", "connections", "created_at")

    def __init__(self, room_id: str):
        self.room_id = room_id
        self.connections: Dict[str, WebSocket] = {}
        self.created_at = asyncio.get_event_loop().time()

    @property
    def is_full(self) -> bool:
        return len(self.connections) >= 2

    @property
    def peer_ids(self):
        return list(self.connections.keys())


rooms: Dict[str, Room] = {}
rooms_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Time-limited TURN credentials (coturn auth-secret mode)
#
# coturn turnserver.conf must include:
#   use-auth-secret=TURN_SECRET_HERE
#   realm=yourdomain.com
#
# The backend issues short-lived credentials so the static password never
# appears in frontend code. Credentials expire after TURN_TTL_SECONDS.
# ---------------------------------------------------------------------------
TURN_SECRET = os.environ.get("TURN_SECRET", "changeme-turn-secret")
TURN_REALM = os.environ.get("TURN_REALM", "localhost")
TURN_TTL_SECONDS = int(os.environ.get("TURN_TTL_SECONDS", "86400"))


def generate_turn_credentials() -> dict:
    """Generate time-limited TURN username/password for coturn auth-secret."""
    expiry = int(time.time()) + TURN_TTL_SECONDS
    tmp_username = f"{expiry}:turnuser"
    mac = hmac.new(TURN_SECRET.encode(), tmp_username.encode(), hashlib.sha1)
    tmp_password = base64.b64encode(mac.digest()).decode()
    return {
        "username": tmp_username,
        "credential": tmp_password,
        "realm": TURN_REALM,
        "ttl_seconds": TURN_TTL_SECONDS,
    }


async def get_or_create_room(room_id: str) -> Room:
    async with rooms_lock:
        if room_id not in rooms:
            rooms[room_id] = Room(room_id)
        return rooms[room_id]


async def add_connection(room: Room, conn_id: str, websocket: WebSocket) -> bool:
    """Add a connection to a room. Returns True if accepted, False if full."""
    async with rooms_lock:
        if room.is_full:
            return False
        room.connections[conn_id] = websocket
        return True


async def remove_connection(room: Room, conn_id: str):
    async with rooms_lock:
        room.connections.pop(conn_id, None)
        if not room.connections:
            # Clean up empty rooms to avoid memory growth
            rooms.pop(room.room_id, None)


async def broadcast_to_peer(room: Room, sender_id: str, message: dict):
    """Send a signaling message to the *other* peer in the room."""
    for conn_id, websocket in room.connections.items():
        if conn_id != sender_id:
            try:
                await websocket.send_text(json.dumps(message))
            except Exception as exc:
                logger.warning("Failed to send to peer %s: %s", conn_id, exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, summary="Home page")
async def home(request: Request):
    """Render the call page. A unique room ID is generated client-side or
    passed via the ?room= query parameter."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/turn-credentials", summary="Get time-limited TURN credentials")
async def turn_credentials():
    """Return short-lived TURN credentials for coturn auth-secret mode."""
    return generate_turn_credentials()


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    """WebSocket signaling endpoint.

    Message protocol (JSON):
        {"type": "offer",   "sdp": <SessionDescription>}
        {"type": "answer",  "sdp": <SessionDescription>}
        {"type": "candidate", "candidate": <RTCIceCandidateInit>}
        {"type": "bye"}  -- call termination
    """
    await websocket.accept()
    conn_id = str(uuid.uuid4())

    room = await get_or_create_room(room_id)

    # Enforce max 2 connections per room
    accepted = await add_connection(room, conn_id, websocket)
    if not accepted:
        logger.warning("Room %s is full; rejecting connection %s", room_id, conn_id)
        await websocket.send_text(
            json.dumps({"type": "error", "message": "Room Full"})
        )
        await websocket.close(code=1008, reason="Room Full")
        return

    logger.info(
        "Client %s joined room %s (total: %d)", conn_id, room_id, len(room.connections)
    )

    # Assign a WebRTC role to coordinate the offer/answer handshake and avoid
    # glare (both peers sending offers simultaneously).
    role = "offerer" if len(room.connections) == 1 else "answerer"
    try:
        await websocket.send_text(
            json.dumps({"type": "role", "role": role})
        )
    except Exception as exc:
        logger.warning("Failed to send role to %s: %s", conn_id, exc)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from %s", conn_id)
                continue

            msg_type = message.get("type")

            if msg_type in ("offer", "answer", "candidate"):
                # Relay signaling message to the peer
                await broadcast_to_peer(room, conn_id, message)
            elif msg_type == "bye":
                # Notify peer of call termination, then close
                await broadcast_to_peer(
                    room, conn_id, {"type": "bye", "by": conn_id}
                )
                break
            else:
                logger.warning("Unknown message type %s from %s", msg_type, conn_id)

    except WebSocketDisconnect:
        logger.info("Client %s disconnected from room %s", conn_id, room_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error for client %s: %s", conn_id, exc)
    finally:
        await remove_connection(room, conn_id)
        logger.info(
            "Client %s left room %s (remaining: %d)",
            conn_id,
            room_id,
            len(room.connections),
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
