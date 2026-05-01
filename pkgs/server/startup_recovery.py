"""Recover orphaned start tasks on server startup.

When the server restarts, any in-flight start tasks (WAITING_FOR_SANDBOX,
PREPARING_REPOSITORY, etc.) have lost their background asyncio.Task and
will never complete. This module marks them as ERROR so the UI shows an
actionable state instead of spinning forever.
"""

import logging


async def recover_orphaned_start_tasks(logger: logging.Logger | None = None) -> list[str]:
    """Mark non-terminal start tasks as ERROR.

    Returns list of recovered task IDs.
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker
    from openhands.app_server.app_conversation.sql_app_conversation_start_task_service import (
        StoredAppConversationStartTask,
    )
    from openhands.app_server.app_conversation.app_conversation_models import (
        AppConversationStartTaskStatus,
    )
    from openhands.app_server.config import get_global_config

    non_terminal = [
        AppConversationStartTaskStatus.WORKING.value,
        AppConversationStartTaskStatus.WAITING_FOR_SANDBOX.value,
        AppConversationStartTaskStatus.PREPARING_REPOSITORY.value,
        AppConversationStartTaskStatus.RUNNING_SETUP_SCRIPT.value,
        AppConversationStartTaskStatus.SETTING_UP_GIT_HOOKS.value,
        AppConversationStartTaskStatus.SETTING_UP_SKILLS.value,
        AppConversationStartTaskStatus.STARTING_CONVERSATION.value,
    ]

    config = get_global_config()
    engine = await config.db_session.get_async_db_engine()
    async_session = sa_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        stmt = (
            update(StoredAppConversationStartTask)
            .where(StoredAppConversationStartTask.status.in_(non_terminal))
            .values(
                status=AppConversationStartTaskStatus.ERROR,
                detail="Server restarted while this task was in progress. Please start a new conversation.",
            )
            .returning(StoredAppConversationStartTask.id)
        )
        result = await session.execute(stmt)
        recovered_ids = [str(row[0]) for row in result.fetchall()]
        await session.commit()

    if recovered_ids:
        logger.info(
            f"Recovered {len(recovered_ids)} orphaned start tasks: {recovered_ids}"
        )
    else:
        logger.info("No orphaned start tasks found on startup")

    return recovered_ids
