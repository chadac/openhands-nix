"""Patch event_service.py: make subscribe_to_events non-blocking.

The FIFO lock is held for the entire duration of agent.step() (which
includes tool execution — potentially minutes). When a WebSocket
reconnects, subscribe_to_events tries to acquire the same lock on the
asyncio event loop thread, freezing the entire server. Fix: use a
non-blocking acquire with a short timeout; skip the initial state
snapshot if the lock is busy (subscriber will get updates via pub/sub).
"""
import sys

path = sys.argv[1]
with open(path) as f:
    content = f.read()

OLD = """\
            with state:
                state_update_event = (
                    ConversationStateUpdateEvent.from_conversation_state(state)
                )

            # Send state update outside the lock - the event is frozen (immutable),
            # so we don't need to hold the lock during the async send operation.
            # This prevents potential deadlocks between the sync FIFOLock and async I/O.
            try:
                await subscriber(state_update_event)
            except Exception as e:
                logger.error(
                    f"Error sending initial state to subscriber {subscriber_id}: {e}"
                )"""

NEW = """\
            # Non-blocking: try to acquire lock with short timeout.
            # If the lock is held (e.g. during a long tool execution in
            # agent.step()), skip the initial snapshot to avoid freezing
            # the event loop.
            state_update_event = None
            if state._lock.acquire(blocking=True, timeout=0.5):
                try:
                    state_update_event = (
                        ConversationStateUpdateEvent.from_conversation_state(state)
                    )
                finally:
                    state._lock.release()

            if state_update_event is not None:
                try:
                    await subscriber(state_update_event)
                except Exception as e:
                    logger.error(
                        f"Error sending initial state to subscriber {subscriber_id}: {e}"
                    )"""

assert OLD in content, f"Pattern not found in {path}"
content = content.replace(OLD, NEW, 1)

with open(path, "w") as f:
    f.write(content)

print(f"Patched {path}: subscribe_to_events now uses non-blocking lock")
