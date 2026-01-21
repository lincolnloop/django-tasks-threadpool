"""
A thread pool backend for Django 6 tasks.

This backend executes tasks in a ThreadPoolExecutor, providing background
execution without external infrastructure. Suitable for development and
low-volume production use cases.

Usage in settings:
    TASKS = {
        "default": {
            "BACKEND": "tasks_threadpool.ThreadPoolBackend",
            "OPTIONS": {
                "MAX_WORKERS": 10,    # Thread pool size
                "MAX_RESULTS": 1000,  # Result retention limit
            }
        }
    }
"""

import logging
import traceback
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from django.tasks import TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import Task, TaskError
from django.tasks.exceptions import TaskResultDoesNotExist

logger = logging.getLogger(__name__)

# Context variable for tasks to access their own result ID
current_result_id: ContextVar[str] = ContextVar("current_result_id")


class ThreadPoolBackend(BaseTaskBackend):
    """
    A Django Tasks backend that executes tasks in a thread pool.

    Simple, no infrastructure required - just threads.
    """

    # Backend capability flags - Django uses these to determine what features are available
    supports_defer = False  # No scheduled/delayed task execution
    supports_async_task = False  # No native async support (threads are synchronous)
    supports_get_result = True  # Results can be retrieved by ID

    def __init__(self, alias: str, params: dict[str, Any]) -> None:
        """
        Initialize the thread pool backend.

        Args:
            alias: The name of this backend in TASKS settings (e.g., "default")
            params: Configuration dict from TASKS settings, may contain OPTIONS
        """
        super().__init__(alias, params)
        options = params.get("OPTIONS", {})
        self._max_workers = options.get("MAX_WORKERS", 10)
        self._max_results = options.get("MAX_RESULTS", 1000)
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
        self._results = {}  # In-memory store: result_id -> TaskResult
        self._completed_ids = deque()  # Track completion order for eviction
        self._lock = Lock()  # Protects _results and _completed_ids

    def _update_result(
        self,
        result_id: str,
        status: TaskResultStatus,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        errors: list[TaskError] | None = None,
        return_value: Any = None,
        completed: bool = False,
    ) -> TaskResult | None:
        """
        Update a result's status in the store. Handles locking internally.

        Returns the updated result, or None if the result doesn't exist.
        """
        with self._lock:
            if result_id not in self._results:
                return None
            old = self._results[result_id]
            result = TaskResult(
                id=old.id,
                task=old.task,
                status=status,
                enqueued_at=old.enqueued_at,
                started_at=started_at if started_at is not None else old.started_at,
                finished_at=finished_at,
                last_attempted_at=started_at
                if started_at is not None
                else old.last_attempted_at,
                args=old.args,
                kwargs=old.kwargs,
                backend=old.backend,
                errors=errors if errors is not None else old.errors,
                worker_ids=old.worker_ids,
            )
            if return_value is not None:
                # TaskResult is frozen, use object.__setattr__ to set _return_value
                object.__setattr__(result, "_return_value", return_value)
            self._results[result_id] = result
            if completed:
                self._completed_ids.append(result_id)
                self._evict_oldest()
            return result

    def _execute_task(self, result_id: str, task: Task) -> None:
        """
        Execute task in a worker thread and update the result store.

        This runs in a background thread, not the main request thread.
        """
        # Set context var so the task can access its own result ID if needed
        current_result_id.set(result_id)

        started_at = datetime.now(timezone.utc)

        # Update status to RUNNING now that we're actually executing
        result = self._update_result(result_id, TaskResultStatus.RUNNING, started_at)
        if result is None:
            return  # Task was removed, nothing to do

        try:
            return_value = task.func(*result.args, **result.kwargs)
            self._update_result(
                result_id,
                TaskResultStatus.SUCCESSFUL,
                finished_at=datetime.now(timezone.utc),
                return_value=return_value,
                completed=True,
            )
        except Exception as e:
            logger.exception("Task %s failed: %s", task.name, e)
            error = TaskError(
                exception_class_path=f"{type(e).__module__}.{type(e).__qualname__}",
                traceback=traceback.format_exc(),
            )
            self._update_result(
                result_id,
                TaskResultStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
                errors=[error],
                completed=True,
            )

    def _evict_oldest(self) -> None:
        """Remove oldest completed results if over limit. Must hold _lock."""
        while len(self._completed_ids) > self._max_results:
            old_id = self._completed_ids.popleft()
            self._results.pop(old_id, None)

    def enqueue(
        self,
        task: Task,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> TaskResult:
        """
        Enqueue a task for background execution in the thread pool.

        Returns a TaskResult with the current status - READY if queued, RUNNING
        if a worker has already started, or even SUCCESSFUL for fast tasks.
        """
        self.validate_task(task)  # Raises if task is misconfigured

        now = datetime.now(timezone.utc)
        result = TaskResult(
            id=str(uuid.uuid4()),
            task=task,
            status=TaskResultStatus.READY,  # Always READY until worker starts
            enqueued_at=now,
            started_at=None,
            finished_at=None,
            last_attempted_at=None,
            args=list(args or ()),
            kwargs=dict(kwargs or {}),
            backend=self.alias,
            errors=[],
            worker_ids=[],
        )

        with self._lock:
            self._results[result.id] = result

        # Submit to thread pool - worker may start immediately
        self._executor.submit(self._execute_task, result.id, task)

        # Return latest status (may have changed to RUNNING if worker started)
        with self._lock:
            return self._results.get(result.id, result)

    def get_result(self, result_id: str) -> TaskResult:
        """Retrieve a task result by ID."""
        with self._lock:
            if result_id not in self._results:
                raise TaskResultDoesNotExist(result_id)
            return self._results[result_id]

    def close(self) -> None:
        """Shut down the thread pool. Cancels queued tasks, waits for running ones."""
        self._executor.shutdown(wait=True, cancel_futures=True)
