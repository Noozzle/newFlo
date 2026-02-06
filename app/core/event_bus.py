"""Event bus for publish/subscribe pattern."""

from __future__ import annotations

import asyncio
import heapq
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine

from loguru import logger

from app.core.events import BaseEvent


# Type alias for event handlers
EventHandler = Callable[[BaseEvent], Coroutine[Any, Any, None]]


@dataclass(order=True)
class PrioritizedEvent:
    """Wrapper for events in priority queue."""
    timestamp: datetime
    sequence: int
    event: BaseEvent = field(compare=False)


class EventBus:
    """
    Event bus with priority queue and async publish/subscribe.

    Events are processed in timestamp order, allowing proper
    simulation of historical data in backtest mode.
    """

    def __init__(self) -> None:
        self._queue: list[PrioritizedEvent] = []
        self._handlers: dict[type, list[EventHandler]] = defaultdict(list)
        self._global_handlers: list[EventHandler] = []
        self._handler_cache: dict[type, list[EventHandler]] = {}
        self._sequence = 0
        self._running = False
        self._lock = asyncio.Lock()

    def subscribe(
        self,
        event_type: type[BaseEvent] | None,
        handler: EventHandler,
    ) -> None:
        """
        Subscribe a handler to an event type.

        Args:
            event_type: The event type to subscribe to, or None for all events
            handler: Async handler function
        """
        if event_type is None:
            self._global_handlers.append(handler)
            logger.debug(f"Registered global handler: {handler.__name__}")
        else:
            self._handlers[event_type].append(handler)
            logger.debug(f"Registered handler for {event_type.__name__}: {handler.__name__}")
        self._handler_cache.clear()

    def unsubscribe(
        self,
        event_type: type[BaseEvent] | None,
        handler: EventHandler,
    ) -> None:
        """Unsubscribe a handler from an event type."""
        if event_type is None:
            if handler in self._global_handlers:
                self._global_handlers.remove(handler)
        else:
            if handler in self._handlers[event_type]:
                self._handlers[event_type].remove(handler)
        self._handler_cache.clear()

    async def publish(self, event: BaseEvent) -> None:
        """
        Publish an event to the queue.

        Events are added to a priority queue sorted by timestamp.
        """
        async with self._lock:
            self._sequence += 1
            prioritized = PrioritizedEvent(
                timestamp=event.timestamp,
                sequence=self._sequence,
                event=event,
            )
            heapq.heappush(self._queue, prioritized)
            logger.trace(f"Published event: {type(event).__name__} at {event.timestamp}")

    async def publish_immediate(self, event: BaseEvent) -> None:
        """
        Publish and immediately process an event.

        Use for events that need immediate handling (e.g., fills in live mode).
        """
        await self._dispatch(event)

    async def _dispatch(self, event: BaseEvent) -> None:
        """Dispatch event to all registered handlers."""
        event_type = type(event)
        handlers = self._handler_cache.get(event_type)
        if handlers is None:
            handlers = self._handlers.get(event_type, []) + self._global_handlers
            self._handler_cache[event_type] = handlers

        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.exception(f"Error in handler {handler.__name__} for {event_type.__name__}: {e}")

    async def process_one(self) -> BaseEvent | None:
        """
        Process the next event from the queue.

        Returns:
            The processed event, or None if queue is empty.
        """
        async with self._lock:
            if not self._queue:
                return None
            prioritized = heapq.heappop(self._queue)

        event = prioritized.event
        await self._dispatch(event)
        return event

    async def process_until(self, until_time: datetime) -> int:
        """
        Process all events up to a given timestamp.

        Args:
            until_time: Process events with timestamp <= this value

        Returns:
            Number of events processed.
        """
        count = 0
        while True:
            async with self._lock:
                if not self._queue or self._queue[0].timestamp > until_time:
                    break
                prioritized = heapq.heappop(self._queue)

            await self._dispatch(prioritized.event)
            count += 1

        return count

    async def process_all(self) -> int:
        """
        Process all events in the queue.

        Returns:
            Number of events processed.
        """
        count = 0
        while True:
            event = await self.process_one()
            if event is None:
                break
            count += 1
        return count

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """
        Run the event processing loop.

        Args:
            stop_event: Optional event to signal stop
        """
        self._running = True
        logger.info("Event bus started")

        try:
            while self._running:
                if stop_event and stop_event.is_set():
                    break

                event = await self.process_one()
                if event is None:
                    # Queue is empty, wait a bit
                    await asyncio.sleep(0.001)
        finally:
            self._running = False
            logger.info("Event bus stopped")

    def stop(self) -> None:
        """Signal the event bus to stop."""
        self._running = False

    @property
    def is_empty(self) -> bool:
        """Check if the queue is empty."""
        return len(self._queue) == 0

    @property
    def queue_size(self) -> int:
        """Get current queue size."""
        return len(self._queue)

    def peek_next(self) -> BaseEvent | None:
        """Peek at the next event without removing it."""
        if not self._queue:
            return None
        return self._queue[0].event

    def clear(self) -> None:
        """Clear all events from the queue."""
        self._queue.clear()
        self._sequence = 0
