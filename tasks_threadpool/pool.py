"""A priority-aware worker pool with result tracking."""

import itertools
import logging
from collections import deque
from dataclasses import dataclass, field
from queue import Empty, PriorityQueue
from threading import Event, Lock, Thread
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Module-level registry of pools, keyed by name.
# Ensures all callers with the same name share the same pool instance.
_pools: dict[str, "WorkerPool"] = {}
_registry_lock = Lock()


def get_pool(
    name: str,
    handler: Callable[[str, Any], Any],
    max_workers: int = 10,
    max_results: int = 1000,
) -> "WorkerPool":
    """Get or create a shared WorkerPool by name.

    Multiple calls with the same name return the same pool instance.
    This is useful when multiple components need to share a single pool
    (e.g., Django creating separate backend instances per thread).

    Args:
        name: Unique identifier for this pool
        handler: Callback invoked as handler(result_id, item) for each item
        max_workers: Number of worker threads (only used on first creation)
        max_results: Maximum results to retain (only used on first creation)

    Returns:
        The shared WorkerPool instance for this name
    """
    with _registry_lock:
        if name not in _pools:
            _pools[name] = WorkerPool(
                handler=handler,
                max_workers=max_workers,
                max_results=max_results,
                name=name,
            )
        return _pools[name]


@dataclass(order=True)
class PrioritizedItem:
    """Item wrapper for priority queue ordering. Lower priority = runs first."""

    priority: int
    sequence: int  # For FIFO within same priority
    item: Any = field(compare=False)


class WorkerPool:
    """A thread pool that processes items in priority order with result tracking.

    Items with lower priority numbers are processed first. Within the same
    priority level, items are processed in FIFO order.

    Results are stored and can be retrieved by ID. Old results are evicted
    in FIFO order when exceeding max_results.

    Usage:
        def handle(result_id, item):
            # Process item, return result or raise exception
            return {"status": "done", "data": item}

        pool = WorkerPool(handler=handle, max_workers=4)
        result_id = pool.submit("task1", priority=0)
        result = pool.get_result(result_id)  # Returns handler's return value
        pool.shutdown()
    """

    def __init__(
        self,
        handler: Callable[[str, Any], Any],
        max_workers: int = 10,
        max_results: int = 1000,
        name: str = "pool",
    ) -> None:
        """Create a worker pool.

        Args:
            handler: Callback invoked as handler(result_id, item) for each submitted item.
                     Return value is stored as the result. Exceptions are caught and stored.
            max_workers: Number of worker threads
            max_results: Maximum completed results to retain (LRU eviction)
            name: Prefix for thread names
        """
        self._handler = handler
        self._name = name
        self._max_results = max_results

        # Work queue
        self._queue: PriorityQueue[PrioritizedItem] = PriorityQueue()
        self._sequence = itertools.count()

        # Result storage
        self._results: dict[str, Any] = {}
        self._completed_ids: deque[str] = deque()
        self._lock = Lock()

        # Workers
        self._shutdown_event = Event()
        self._workers = [
            Thread(
                target=self._worker_loop,
                daemon=True,
                name=f"{name}-{i}",
            )
            for i in range(max_workers)
        ]
        for w in self._workers:
            w.start()

        logger.debug(
            "WorkerPool '%s' started: max_workers=%d, max_results=%d",
            name,
            max_workers,
            max_results,
        )

    def _worker_loop(self) -> None:
        """Worker thread main loop - pulls items from priority queue."""
        while not self._shutdown_event.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                result_id, payload = item.item
                result = self._handler(result_id, payload)
                self._store_result(result_id, result, completed=True)
            except Exception:
                logger.exception("Handler raised exception")
            finally:
                self._queue.task_done()

    def _store_result(
        self, result_id: str, result: Any, completed: bool = False
    ) -> None:
        """Store a result, evicting old ones if over limit."""
        with self._lock:
            self._results[result_id] = result
            if completed:
                self._completed_ids.append(result_id)
                while len(self._completed_ids) > self._max_results:
                    old_id = self._completed_ids.popleft()
                    self._results.pop(old_id, None)

    def submit(
        self, result_id: str, item: Any, priority: int = 0, initial_result: Any = None
    ) -> None:
        """Submit an item for processing.

        Args:
            result_id: Unique ID for this work item
            item: The item to process (passed to handler)
            priority: Lower numbers run first (default: 0)
            initial_result: Initial result to store before processing starts
        """
        if initial_result is not None:
            with self._lock:
                self._results[result_id] = initial_result

        self._queue.put(
            PrioritizedItem(
                priority=priority,
                sequence=next(self._sequence),
                item=(result_id, item),
            )
        )

    def get_result(self, result_id: str) -> Any:
        """Get a result by ID, or None if not found."""
        with self._lock:
            return self._results.get(result_id)

    def has_result(self, result_id: str) -> bool:
        """Check if a result exists."""
        with self._lock:
            return result_id in self._results

    def update_result(
        self, result_id: str, result: Any, completed: bool = False
    ) -> bool:
        """Update an existing result. Returns False if result doesn't exist."""
        with self._lock:
            if result_id not in self._results:
                return False
            self._results[result_id] = result
            if completed:
                self._completed_ids.append(result_id)
                while len(self._completed_ids) > self._max_results:
                    old_id = self._completed_ids.popleft()
                    self._results.pop(old_id, None)
            return True

    def shutdown(self, wait: bool = True, timeout: float = 5.0) -> None:
        """Stop all workers.

        Args:
            wait: If True, wait for workers to finish
            timeout: Max seconds to wait per worker
        """
        self._shutdown_event.set()
        if wait:
            for w in self._workers:
                w.join(timeout=timeout)

    @property
    def is_shutdown(self) -> bool:
        """True if shutdown has been initiated."""
        return self._shutdown_event.is_set()

    @property
    def worker_count(self) -> int:
        """Number of worker threads."""
        return len(self._workers)

    @property
    def max_results(self) -> int:
        """Maximum results to retain."""
        return self._max_results
