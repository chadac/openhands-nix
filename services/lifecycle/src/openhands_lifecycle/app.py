"""OpenHands Lifecycle Manager — background daemon for sandbox lifecycle management."""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from openhands_common import db, messaging

from .cleanup import cleanup_idle_conversations, cleanup_orphaned_sandboxes, cleanup_old_pvcs
from .config import db_settings, mq_settings, settings
from .resume import handle_conversation_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)

IDLE_CLEANUP_INTERVAL = 60.0
ORPHAN_CLEANUP_INTERVAL = 300.0

_cleanup_task: asyncio.Task | None = None


async def _cleanup_loop() -> None:
    """Main cleanup loop — runs idle and orphan cleanup on intervals."""
    idle_counter = 0.0
    orphan_counter = 0.0
    poll_interval = 20.0

    while True:
        await asyncio.sleep(poll_interval)

        idle_counter += poll_interval
        if idle_counter >= IDLE_CLEANUP_INTERVAL:
            idle_counter = 0.0
            await cleanup_idle_conversations()

        orphan_counter += poll_interval
        if orphan_counter >= ORPHAN_CLEANUP_INTERVAL:
            orphan_counter = 0.0
            await cleanup_orphaned_sandboxes()
            await cleanup_old_pvcs()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task
    await db.init_db(db_settings)
    await messaging.connect(mq_settings)
    await messaging.subscribe(
        "lifecycle.conversation-messages",
        ["conversation.message"],
        handle_conversation_message,
    )
    _cleanup_task = asyncio.create_task(_cleanup_loop())
    logger.info("Lifecycle manager started")
    yield
    if _cleanup_task:
        _cleanup_task.cancel()
    await messaging.disconnect()
    await db.close_db()


app = FastAPI(title="OpenHands Lifecycle Manager", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/cleanup/trigger")
async def trigger_cleanup():
    """Manual cleanup trigger for debugging."""
    await cleanup_idle_conversations()
    await cleanup_orphaned_sandboxes()
    await cleanup_old_pvcs()
    return {"status": "ok"}


def main():
    uvicorn.run(
        "openhands_lifecycle.app:app",
        host="0.0.0.0",
        port=8081,
        log_level="info",
    )
