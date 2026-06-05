from __future__ import annotations
import asyncio
import json
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI()

# Connected WebSocket clients
_clients: list[WebSocket] = []
_clients_lock = asyncio.Lock()


class Event(BaseModel):
    event: str
    node: str
    ts: float = 0.0
    model_config = {"extra": "allow"}


@app.post("/event")
async def receive_event(event: Event) -> dict:
    data = event.model_dump()
    await _broadcast(data)
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    async with _clients_lock:
        _clients.append(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive, ignore client messages
    except WebSocketDisconnect:
        async with _clients_lock:
            _clients.remove(ws)


async def _broadcast(data: dict[str, Any]) -> None:
    async with _clients_lock:
        dead = []
        for ws in _clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _clients.remove(ws)


app.mount("/", StaticFiles(directory="marsnet/dashboard/static",
                           html=True), name="static")
