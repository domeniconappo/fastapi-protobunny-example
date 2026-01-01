import logging
from contextlib import asynccontextmanager

import betterproto
import chatlib as cl  # this is the module generated with `protobunny generate`
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from protobunny import asyncio as pb
from redis import asyncio as redis

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")

# Initialize Redis client for storage for specific app features
# (e.g. keeping track of last 20 messages to show for the connected user)
r_store = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
HISTORY_LIMIT = 20
HISTORY_KEY = "chat_history"
PRESENCE_KEY = "active_users"


@asynccontextmanager
async def lifespan(_: FastAPI):
    """
    This manages the app-level protobunny subscriptions
    """
    # Connect to the message broker
    await reset_onlinelist()
    await pb.connect()

    # Listen for Messages
    async def on_chat(msg: cl.chat.ChatMessage):
        await manager.broadcast_smart(msg)

    # Listen for Presence
    async def on_status(msg: cl.chat.UserStatus):
        await save_to_presence_statuses(msg)
        await manager.broadcast_status(msg)

    # Subscribe the two callbacks to the relevant topics
    await pb.subscribe(cl.chat.ChatMessage, on_chat)
    await pb.subscribe(cl.chat.UserStatus, on_status)

    yield
    # Shutdown
    await pb.disconnect()


app = FastAPI(lifespan=lifespan)


class ConnectionManager:
    """Keep track of active WebSocket connections and publish messages to them."""

    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        # If the user is already connected, close the old ghost socket
        if client_id in self.active_connections:
            try:
                await self.active_connections[client_id].close()
            except:
                pass
        await websocket.accept()
        self.active_connections[client_id] = websocket

    def disconnect(self, client_id: str, websocket: WebSocket):
        # Only remove if this is the ACTUAL websocket stored
        # (Prevents a late disconnect from a ghost killing a new session)
        if self.active_connections.get(client_id) == websocket:
            self.active_connections.pop(client_id, None)
            return True
        return False

    async def broadcast_smart(self, message: cl.chat.ChatMessage):
        log.info("Sending message: %s", message)
        if message.to_user:
            message.content = f"__private__ {message.content.strip()}"
            log.info("Sending private message: %s", message.content.strip(""))
            # Private: Send only to the recipient (if they are on THIS server)
            # and the sender (for their own UI)
            targets = [
                t
                for t in (message.to_user, message.sender)
                if t in self.active_connections
            ]
            for client_id in targets:
                log.info("Sending to: %s", client_id)
                await self.active_connections[client_id].send_text(
                    message.to_json(casing=betterproto.Casing.SNAKE)
                )
        else:
            # Global: Send to everyone
            for ws in self.active_connections.values():
                await ws.send_text(
                    data=message.to_json(casing=betterproto.Casing.SNAKE)
                )

    async def broadcast_status(self, message: cl.chat.UserStatus):
        data = message.to_json(casing=betterproto.Casing.SNAKE)
        log.debug(f"Broadcasting status update: {data}")
        for ws in self.active_connections.values():
            await ws.send_text(data)


manager = ConnectionManager()


@app.get("/", response_class=HTMLResponse)
async def get_chat_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    # Main entrypoint for new connections
    # Here the user starts a chat session
    await manager.connect(websocket, client_id)

    # Send History and online list to the new user ---
    await send_history(websocket)
    await send_presence_statuses(websocket)

    # Broadcast that a new user is online
    user_status = cl.chat.UserStatus(client_id=client_id, online=True)
    await pb.publish(user_status)
    await save_to_presence_statuses(user_status)

    try:
        while True:
            # Receive message from client
            data = await websocket.receive_json()

            # ChatMessage (could be global or private if to_user is set)
            chat_msg = cl.chat.ChatMessage(
                sender=client_id,
                content=data["content"],
                to_user=data.get("to_user", ""),
            )

            await save_to_history(chat_msg)

            # Publish the message
            await pb.publish(chat_msg)
            # Send an ArchiveMessage to the archiver service
            await pb.publish(cl.tasks.ArchiveMessage(message=chat_msg))

    except WebSocketDisconnect:
        manager.disconnect(client_id, websocket)
        disconnect_status = cl.chat.UserStatus(client_id=client_id, online=False)
        await pb.publish(disconnect_status)
        await save_to_presence_statuses(disconnect_status)


async def save_to_history(chat_msg: cl.chat.ChatMessage):
    # Keep track of last 20 messages in a global list so the user sees the last messages when it connects
    if not chat_msg.to_user:
        msg_json = chat_msg.to_json(casing=betterproto.Casing.SNAKE)
        await r_store.lpush(HISTORY_KEY, msg_json)
        await r_store.ltrim(HISTORY_KEY, 0, HISTORY_LIMIT - 1)


async def send_history(websocket: WebSocket):
    # Retrieve last 20 messages from the "global_history" list for the newly connected user
    history = await r_store.lrange(HISTORY_KEY, 0, HISTORY_LIMIT - 1)
    for msg_json in reversed(history):
        await websocket.send_text(msg_json)


async def save_to_presence_statuses(message: cl.chat.UserStatus):
    # Keep track of last 20 messages in a global list so the user sees the last messages when it connects
    msg_json = message.to_json(casing=betterproto.Casing.SNAKE)
    log.warning("Saving presence status: %s", msg_json)
    if message.online:
        await r_store.lpush(PRESENCE_KEY, msg_json)
    else:
        await r_store.lrem(PRESENCE_KEY, 0, msg_json)


async def send_presence_statuses(websocket: WebSocket):
    # Retrieve present users
    log.warning("Sending presence statuses:")
    active_users = await r_store.lrange(PRESENCE_KEY, 0, -1)
    for msg_json in reversed(active_users):
        log.warning("Sending presence statuses %s", msg_json)
        await websocket.send_text(msg_json)


async def reset_onlinelist():
    await r_store.delete(PRESENCE_KEY)


async def reset_history():
    await r_store.delete(HISTORY_KEY)
