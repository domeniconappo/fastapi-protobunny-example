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
