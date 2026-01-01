# Building a Real-time Chat App with FastAPI & Protobunny
This guide demonstrates how to build a simple but scalable, type-safe chat application. We will use FastAPI for the web layer, Redis as the message broker, and Protobunny to handle all the internal communication logic.

## Project Architecture
Our application consists of three main parts:

- The Library (`chatlib`): Protobuf definitions for Messages, User Status, and Background Tasks.

- The Web Server: A FastAPI app handling WebSockets and real-time Pub/Sub.

- The Worker: A background consumer that archives messages to a database using the tasks pattern.

```text
chat-app/
└── messages/            # Protobuf definitions
├── chatlib/             # Generated betterproto code
├── static/              # CSS/JS for the frontend
├── templates/           # HTML templates
├── main.py              # FastAPI server (Pub/Sub logic)
└── worker.py            # Archive service (Consumer logic)
```

---

## Prerequisites
- Python 3.10+
- redis instance reachable

Note: This example uses redis as a message broker but Protobunny can be used with any other broker.

For the chat app, we will use the redis backend.

If you want to use a different broker:

- replace the redis specific app code dependency with the one for your broker available (rabbitmq, mosquitto, redis)  
- change the `backend` in the `[tool.protobunny]` section in pyproject.toml file.

You can also use `backend=python` to use a local in-process broker for testing purposes. 

## Step 0: Install Protobunny and setup

Add `protobunny[redis]>=0.1.2a1` as a dependency to your project.

You can install it using pip, poetry, or uv:

```shell
pip install protobunny[redis]
```

This is an example of a `pyproject.toml` file:
```toml
[project]
name = "fastapiprotobunny"
version = "0.1.0"
description = "A FastAPI chat app that uses protobunny with redis"
requires-python = ">=3.10,<3.14"
dependencies = [
    "fastapi>=0.126.0",
    "uvicorn[standard]>=0.38.0",
    "protobunny[redis] >=0.1.2a1",
    "aiosqlite>=0.22.1",
]
[tool.protobunny]
messages-directory = "messages"
messages-prefix = "acme"
generated-package-name = "chatlib"
generated-package-root = "./chat-app/"
backend = "redis"
mode = "async"
force-required-fields = true
```

Variables can be configured also in a `protobunny.ini` file or environment variables.
See the official documentation for more details.


## Step 1: Define your Messages
First, we define our communication contract using Protobuf. 
Notice the tasks package: Protobunny uses this naming convention to automatically route these 
messages to a persistent worker queue instead of a broadcast exchange.

```protobuf
// messages/chat.proto
syntax = "proto3";
package chat;

message ChatMessage {
  string sender = 1;
  string content = 2;
  string to_user = 3; // Empty for global chat
}

message UserStatus {
  string client_id = 1;
  bool online = 2;
}
```

```protobuf
// messages/tasks.proto
syntax = "proto3";

package tasks;

import "chat.proto";


message ArchiveMessage {
  chat.ChatMessage message = 1;
}
```
----
## Step 2: Generate the python classes

```shell
protobunny generate
```

You will find the generated code in `chat-app/chatlib` directory.

---
## Step 3: Create the Web Server

The web server acts as a bridge. It converts incoming WebSocket JSON into type-safe Protobuf objects and publishes them via Protobunny. 
It also subscribes to global updates to keep all connected clients in sync.

Notice the following features:
- Unified Interface: The same pb.publish() call handles both real-time broadcasts and background tasks.
- Automatic Serialization: No manual JSON-to-Protobuf mapping.

The code is available in this [chat-app example on github](https://github.com/domeniconappo/fastapi-protobunny-example.git) example.

The following snippet shows the startup logic.
It uses the `lifespan` feature of FastAPI to manage the protobunny subscriptions.

The app subscribes to two topics: `chat.ChatMessage` and `chat.UserStatus`. One for all messages sent and one to keep track of the online users.
The callbacks are async functions that handle the incoming messages and will use the in process broker to send updates to the connected clients.


```python
# main.py
import logging

import betterproto
import chatlib as cl  # this is the module generated with `protobunny generate`
from protobunny import asyncio as pb
from redis import asyncio as redis

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

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
```

The `manager` object is used to send messages to connected websocket clients.

```python
# main.py
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
```

These are the two main routes. 
One simply renders the chat page and the other handles the WebSocket incoming messages and publishes them to redis via protobunny.
It also sends an ArchiveMessage to the background worker to store the message in a database.


```python
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
            # Send an ArchiveMessage to the archiver service if running
            await pb.publish(cl.tasks.ArchiveMessage(message=chat_msg))

    except WebSocketDisconnect:
        manager.disconnect(client_id, websocket)
        disconnect_status = cl.chat.UserStatus(client_id=client_id, online=False)
        await pb.publish(disconnect_status)
        await save_to_presence_statuses(disconnect_status)

```

Some helper functions to send history and presence updates to the connected clients.

```python
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

```
---

## Step 4: The worker

You can run workers as separate processes to compete for the same tasks queue and share load.
This is handy for expensive tasks.
In protobunny, all protobuf messages defined under a package `tasks` will be routed to shared queues.

For the asynchronous task of archiving messages, we will use the `cl.tasks.ArchiveMessage` message.

```python
# worker.py
import logging

import aiosqlite
import chatlib as cl
from protobunny import asyncio as pb

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# Simple DB setup for the example
async def save_to_db(sender, content):
    async with aiosqlite.connect("chat_history.db") as conn:
        # cursor = conn.cursor()
        log.info("Saving message to DB...")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS messages (sender TEXT, content TEXT)"
        )
        await conn.execute("INSERT INTO messages VALUES (?, ?)", (sender, content))
        await conn.commit()
        await conn.close()


async def archive_callback(task: cl.tasks.ArchiveMessage):
    print(f"Archiving message from {task.message.sender}...")
    await save_to_db(task.message.sender, task.message.content)


async def main():
    await pb.connect()
    await pb.subscribe(cl.tasks.ArchiveMessage, archive_callback)


if __name__ == "__main__":
    log.info("Archiver Service Started. Waiting for tasks...")
    pb.run_forever(main)
```

## Step 5: A bit of frontend

The example frontend is a simple HTML page with a simple chat form and some JS to also handle private chats.
It's very rudimentary but it works. 
The purpose is to show how to integrate the websocket connection with the rest of the app.

```html
<!DOCTYPE html>
<html>
    <head>
        <title>Protobunny Chat</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light">
<div class="container-fluid py-5">
    <div class="row">
        <div class="col-9">
            <h2 id="chatHeader">Global Chat</h2>
            <div id="messages" class="border bg-white p-3 mb-3" style="height: 400px; overflow-y: scroll;"></div>
            <div class="input-group">
                <input type="text" id="messageText" class="form-control" placeholder="Type..." >
                <button class="btn btn-primary" onclick="sendMessage()">Send</button>
            </div>
        </div>

        <div class="col-3">
            <h5>Online Users</h5>
            <div id="userList" class="list-group">
                <button class="list-group-item list-group-item-action active" onclick="startPrivateChat('global')">
                    🌎 Global Chat
                </button>
                <hr>
            </div>
        </div>
    </div>
</div>

<script>
    // Check if we have a saved ID, otherwise create one
    let current_user = localStorage.getItem('chat_username');

    if (!current_user) {
        current_user = "user_" + Math.floor(Math.random() * 10000);
        localStorage.setItem('chat_username', current_user);
    }
    {#const current_user = "User_" + Math.floor(Math.random() * 1000);#}
    document.getElementById('chatHeader').textContent = `Hallo there ${current_user}!`;
    let activePrivateChat = null; // null means Global

    const ws = new WebSocket(`ws://localhost:8000/ws/${current_user}`);
    const node = document.getElementById("messageText");
    node.addEventListener("keyup", ({key}) => {
        if (key === "Enter") {
            sendMessage();
        }
    });
    // --- Global State ---
    const messageCache = {
        "global": [], // Store global messages here
        // "User_123": [ {sender, content}, ... ]
    };

    ws.onmessage = function(event) {
        const data = JSON.parse(event.data);

        // Handle Presence Update
        if (data.client_id) {
            updateUserList(data);
            return;
        }

        // Handle Chat Message
        displayMessage(data);
    };

    function updateUserList(data) {
        console.log("Presence Update");

        const list = document.getElementById('userList');
        let el = document.getElementById(`user-${data.client_id}`);
        console.log(el);
        console.log(current_user);

        if (data.online && !el) {
            // create user element in the right panel
            console.log(data.client_id);
            el = document.createElement('button');
            el.id = `user-${data.client_id}`;
            el.className = "list-group-item list-group-item-action";
            el.innerText = data.client_id;
            el.ondblclick = () => startPrivateChat(data.client_id);
            list.appendChild(el);
        } else if (!data.online && el) {
            el.remove();
        }
    }

    function sendMessage() {
        const input = document.getElementById("messageText");
        const msg = {
            content: input.value,
            to_user: activePrivateChat || "", // If null, it's global chat
            sender: current_user,
        };
        ws.send(JSON.stringify(msg));
        input.value = '';
    }

    function displayMessage(data) {
        const sender = data.sender;
        const toUser = data.to_user || "";
        const content = data.content;

        const chatPartner = (toUser === "") ? "global" : (sender === current_user ? toUser : sender);
        const isGlobalMsg = (toUser === "");
        if (!messageCache[chatPartner]) {
            messageCache[chatPartner] = [];
        }
        messageCache[chatPartner].push({sender, content});
        if (isGlobalMsg && activePrivateChat === null) {
            // We are in Global view, show global message
            renderSingleMessage(sender, content);
        }
        else if (!isGlobalMsg) {
            if (chatPartner === activePrivateChat) {
                // We are in the correct Private view, show it
                renderSingleMessage(sender, content);
            } else if (sender !== current_user) {
                // It's a private message for a window that isn't open
                notifyNewPrivateMessage(sender);
            }
        }
    }

    function startPrivateChat(userId) {
        activePrivateChat = userId;

        // Clear notification UI
        const userBtn = document.getElementById(`user-${userId}`);
        if (userBtn) {
            userBtn.classList.remove("bg-warning");
            userBtn.innerText = userId;
        }

        document.getElementById('chatHeader').innerText = (userId === "global") ? `${current_user} Global Chat` : `Private Chat with ${userId}`;

        // RE-RENDER everything from the cache for this specific user
        renderEntireCache(userId);
    }

    function renderEntireCache(chatId) {
        const container = document.getElementById('messages');
        container.innerHTML = ''; // Clear current screen

        const messages = messageCache[chatId] || [];
        messages.forEach(msg => {
            renderSingleMessage(msg.sender, msg.content);
        });
    }

    function renderSingleMessage(sender, content) {
        const container = document.getElementById('messages');
        const div = document.createElement('div');
        div.innerHTML = `<b>${sender}:</b> ${content}`;
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    }

    function notifyNewPrivateMessage(sender, content) {
        const userBtn = document.getElementById(`user-${sender}`);
        if (userBtn) {
            userBtn.classList.add("bg-warning"); // Highlight the user in the list
            userBtn.innerHTML = `${sender} (New Message!)`;
        }

    }

</script>
</body>
</html>

```

## Run the demo
Start Redis with docker or use an existing instance: 

`docker run -p 6379:6379 redis`

Start the Worker to store (public!) messages to a local sqlite db: `python worker.py`

Enter the `chat-app` directory and start the Web Server: `uvicorn main:app --reload`

Open in Browser: Visit http://localhost:8000 in two different tabs to start chatting!

You can open the redis-cli to see the messages stored in the redis db:

```shell
docker exec -it redis redis-cli
```

To see what's happening in the background in Redis:
```shell
redis-cli MONITOR
```
