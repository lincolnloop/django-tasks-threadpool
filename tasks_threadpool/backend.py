"""
A thread pool backend for Django 6 tasks.

This backend executes tasks in a WorkerPool, providing background
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
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from django.tasks import TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import Task, TaskError
from django.tasks.exceptions import TaskResultDoesNotExist

from .pool import get_pool

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
    supports_priority = True  # Tasks can specify priority (-100 to 100)

    def __init__(self, alias: str, params: dict[str, Any]) -> None:
        """
        Initialize the thread pool backend.

        Args:
            alias: The name of this backend in TASKS settings (e.g., "default")
            params: Configuration dict from TASKS settings, may contain OPTIONS
        """
        super().__init__(alias, params)
        options = params.get("OPTIONS", {})
        max_workers = options.get("MAX_WORKERS", 10)
        max_results = options.get("MAX_RESULTS", 1000)

        self._pool = get_pool(
            name=f"tasks-{alias}",
            handler=self._execute_task,
            max_workers=max_workers,
            max_results=max_results,
        )
        logger.debug(
            "ThreadPoolBackend '%s' initialized: max_workers=%d, max_results=%d",
            alias,
            max_workers,
            max_results,
        )

    def _execute_task(self, result_id: str, task: Task) -> TaskResult:
        """
        Execute task in a worker thread and return the updated result.

        This runs in a background thread, not the main request thread.
        """
        # Set context var so the task can access its own result ID if needed
        current_result_id.set(result_id)

        started_at = datetime.now(timezone.utc)

        # Get current result and update to RUNNING
        result = self._pool.get_result(result_id)
        if result is None:
            return None  # Task was removed

        result = TaskResult(
            id=result.id,
            task=result.task,
            status=TaskResultStatus.RUNNING,
            enqueued_at=result.enqueued_at,
            started_at=started_at,
            finished_at=None,
            last_attempted_at=started_at,
            args=result.args,
            kwargs=result.kwargs,
            backend=result.backend,
            errors=result.errors,
            worker_ids=result.worker_ids,
        )
        self._pool.update_result(result_id, result)

        try:
            return_value = task.func(*result.args, **result.kwargs)
            result = TaskResult(
                id=result.id,
                task=result.task,
                status=TaskResultStatus.SUCCESSFUL,
                enqueued_at=result.enqueued_at,
                started_at=result.started_at,
                finished_at=datetime.now(timezone.utc),
                last_attempted_at=result.last_attempted_at,
                args=result.args,
                kwargs=result.kwargs,
                backend=result.backend,
                errors=[],
                worker_ids=result.worker_ids,
            )
            # TaskResult is frozen, use object.__setattr__ to set _return_value
            object.__setattr__(result, "_return_value", return_value)
            return result
        except Exception as e:
            logger.exception("Task %s failed: %s", task.name, e)
            error = TaskError(
                exception_class_path=f"{type(e).__module__}.{type(e).__qualname__}",
                traceback=traceback.format_exc(),
            )
            return TaskResult(
                id=result.id,
                task=result.task,
                status=TaskResultStatus.FAILED,
                enqueued_at=result.enqueued_at,
                started_at=result.started_at,
                finished_at=datetime.now(timezone.utc),
                last_attempted_at=result.last_attempted_at,
                args=result.args,
                kwargs=result.kwargs,
                backend=result.backend,
                errors=[error],
                worker_ids=result.worker_ids,
            )

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

        result_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        initial_result = TaskResult(
            id=result_id,
            task=task,
            status=TaskResultStatus.READY,
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

        self._pool.submit(
            result_id=result_id,
            item=task,
            priority=task.priority,
            initial_result=initial_result,
        )
        logger.debug(
            "Task '%s' enqueued: result_id=%s, priority=%d",
            task.name,
            result_id,
            task.priority,
        )

        # Return latest status (may have changed to RUNNING if worker started)
        return self._pool.get_result(result_id) or initial_result

    def get_result(self, result_id: str) -> TaskResult:
        """Retrieve a task result by ID."""
        result = self._pool.get_result(result_id)
        if result is None:
            raise TaskResultDoesNotExist(result_id)
        return result

    def close(self) -> None:
        """Shut down the thread pool. Signals workers to stop and waits for them."""
        self._pool.shutdown()
