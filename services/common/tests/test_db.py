"""Tests for the shared database layer.

Uses an in-memory SQLite database for speed (the ORM models are
database-agnostic). Production uses PostgreSQL via asyncpg.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from openhands_common import db
from openhands_common.models import Base


@pytest_asyncio.fixture
async def setup_db():
    """Initialize an in-memory SQLite database for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Monkey-patch the db module to use our test engine
    db._engine = engine
    db._session_factory = session_factory

    yield

    await engine.dispose()
    db._engine = None
    db._session_factory = None


# ---------------------------------------------------------------------------
# Conversation map
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_and_lookup_conversation(setup_db):
    """Store a conversation mapping and look it up."""
    await db.store_conversation("gitlab", "issue", "repo#1", "conv-123")

    result = await db.lookup_conversation("gitlab", "issue", "repo#1")
    assert result == "conv-123"


@pytest.mark.asyncio
async def test_lookup_nonexistent_returns_none(setup_db):
    """Looking up a resource with no mapping returns None."""
    result = await db.lookup_conversation("gitlab", "issue", "nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_store_conversation_upsert(setup_db):
    """Storing a second time for the same resource updates the conversation_id."""
    await db.store_conversation("gitlab", "issue", "repo#1", "conv-old")
    await db.store_conversation("gitlab", "issue", "repo#1", "conv-new")

    result = await db.lookup_conversation("gitlab", "issue", "repo#1")
    assert result == "conv-new"


@pytest.mark.asyncio
async def test_update_last_status(setup_db):
    """Updating last_status persists correctly."""
    await db.store_conversation("gitlab", "issue", "repo#1", "conv-123")
    await db.update_last_status("conv-123", "RUNNING")

    convs = await db.get_active_conversations()
    # Note: get_active_conversations filters for note_id IS NOT NULL OR slack_ts IS NOT NULL
    # So we need to set note metadata first
    await db.store_note_metadata("conv-123", 42, "issues", 1, 999)

    convs = await db.get_active_conversations()
    assert len(convs) == 1
    assert convs[0]["conversation_id"] == "conv-123"
    assert convs[0]["last_status"] == "RUNNING"


@pytest.mark.asyncio
async def test_store_note_metadata(setup_db):
    """Store GitLab note metadata for a conversation."""
    await db.store_conversation("gitlab", "issue", "repo#1", "conv-123")
    await db.store_note_metadata("conv-123", 42, "issues", 1, 999)

    convs = await db.get_active_conversations()
    assert len(convs) == 1
    assert convs[0]["project_id"] == 42
    assert convs[0]["note_id"] == 999


@pytest.mark.asyncio
async def test_store_slack_metadata(setup_db):
    """Store Slack metadata for a conversation."""
    await db.store_conversation("slack", "thread", "C123:ts1", "conv-456")
    await db.store_slack_metadata("conv-456", "C123", "1234567890.123456")

    convs = await db.get_active_conversations()
    assert len(convs) == 1
    assert convs[0]["slack_channel"] == "C123"
    assert convs[0]["slack_ts"] == "1234567890.123456"


# ---------------------------------------------------------------------------
# Pending conversations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_conversation_detection(setup_db):
    """Pending placeholder is detected correctly."""
    assert db.is_pending(db.PENDING_CONVERSATION_ID)
    assert not db.is_pending("conv-123")
    assert not db.is_pending(None)


@pytest.mark.asyncio
async def test_pending_message_queue(setup_db):
    """Messages queued during conversation creation are retrievable."""
    await db.store_pending_message("gitlab", "issue", "repo#1", "msg-1")
    await db.store_pending_message("gitlab", "issue", "repo#1", "msg-2")

    messages = await db.get_and_clear_pending_messages("gitlab", "issue", "repo#1")
    assert messages == ["msg-1", "msg-2"]

    # Second call returns empty (messages were cleared)
    messages = await db.get_and_clear_pending_messages("gitlab", "issue", "repo#1")
    assert messages == []


@pytest.mark.asyncio
async def test_clear_conversation(setup_db):
    """Clearing a conversation removes the mapping."""
    await db.store_conversation("gitlab", "issue", "repo#1", db.PENDING_CONVERSATION_ID)
    await db.clear_conversation("gitlab", "issue", "repo#1")

    result = await db.lookup_conversation("gitlab", "issue", "repo#1")
    assert result is None


# ---------------------------------------------------------------------------
# Jira tickets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_jira_ticket(setup_db):
    """Link a Jira ticket to a conversation."""
    is_new = await db.link_jira_ticket("conv-123", "PLAT-555")
    assert is_new is True

    # Duplicate link returns False
    is_new = await db.link_jira_ticket("conv-123", "PLAT-555")
    assert is_new is False


@pytest.mark.asyncio
async def test_get_primary_jira_ticket(setup_db):
    """Primary ticket is the first one linked."""
    await db.link_jira_ticket("conv-123", "PLAT-555")
    await db.link_jira_ticket("conv-123", "PLAT-666")

    primary = await db.get_primary_jira_ticket("conv-123")
    assert primary == "PLAT-555"


@pytest.mark.asyncio
async def test_get_jira_tickets(setup_db):
    """Get all tickets linked to a conversation."""
    await db.link_jira_ticket("conv-123", "PLAT-555")
    await db.link_jira_ticket("conv-123", "PLAT-666")

    tickets = await db.get_jira_tickets("conv-123")
    assert tickets == ["PLAT-555", "PLAT-666"]


@pytest.mark.asyncio
async def test_conversation_for_jira_ticket(setup_db):
    """Look up conversation by Jira ticket key."""
    await db.link_jira_ticket("conv-123", "PLAT-555")

    conv = await db.get_conversation_for_jira_ticket("PLAT-555")
    assert conv == "conv-123"

    conv = await db.get_conversation_for_jira_ticket("PLAT-999")
    assert conv is None


# ---------------------------------------------------------------------------
# Resource links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_resource(setup_db):
    """Link a resource to a conversation."""
    is_new = await db.link_resource("conv-123", "gitlab", "merge_request", "repo!42")
    assert is_new is True

    # Duplicate returns False
    is_new = await db.link_resource("conv-123", "gitlab", "merge_request", "repo!42")
    assert is_new is False


@pytest.mark.asyncio
async def test_get_conversation_for_resource(setup_db):
    """Look up conversation by linked resource."""
    await db.link_resource("conv-123", "gitlab", "merge_request", "repo!42")

    conv = await db.get_conversation_for_resource("gitlab", "merge_request", "repo!42")
    assert conv == "conv-123"

    conv = await db.get_conversation_for_resource("gitlab", "issue", "repo#99")
    assert conv is None


# ---------------------------------------------------------------------------
# Credential scopes (broker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_credential_scope(setup_db):
    """Add a credential scope for a conversation."""
    is_new = await db.add_credential_scope("conv-123", "github", "owner/repo")
    assert is_new is True

    # Duplicate returns False
    is_new = await db.add_credential_scope("conv-123", "github", "owner/repo")
    assert is_new is False


@pytest.mark.asyncio
async def test_get_credential_scopes(setup_db):
    """Get all credential scopes for a conversation+provider."""
    await db.add_credential_scope("conv-123", "github", "owner/repo1")
    await db.add_credential_scope("conv-123", "github", "owner/repo2")
    await db.add_credential_scope("conv-123", "gitlab", "group/project")

    scopes = await db.get_credential_scopes("conv-123", "github")
    assert set(scopes) == {"owner/repo1", "owner/repo2"}

    scopes = await db.get_credential_scopes("conv-123", "gitlab")
    assert scopes == ["group/project"]

    scopes = await db.get_credential_scopes("conv-123", "slack")
    assert scopes == []
