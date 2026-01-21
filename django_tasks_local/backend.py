"""
Concurrent futures backends for Django 6 tasks.

This module provides ThreadPoolBackend and ProcessPoolBackend for background
task execution without external infrastructure. Uses Python's standard
concurrent.futures module.

Usage in settings:
    TASKS = {
        "default": {
            "BACKEND": "django_tasks_local.ThreadPoolBackend",
            "OPTIONS": {
                "MAX_WORKERS": 10,    # Pool size
                "MAX_RESULTS": 1000,  # Result retention limit
            }
        }
    }
"""

import logging
import pickle
import traceback
import uuid
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from django.tasks import TaskResult, TaskResultStatus
from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import Task, TaskError
from django.tasks.exceptions import TaskResultDoesNotExist
from django.utils.module_loading import import_string

from .state import get_executor_state, shutdown_executor

logger = logging.getLogger(__name__)

# Context variable for tasks to access their own result ID
current_result_id: ContextVar[str] = ContextVar("current_result_id")


def _execute_task(func_path: str, args: tuple, kwargs: dict, result_id: str):
    """Execute task in worker thread/process.

    Sets ContextVar for task access. Works in both backends:
    - ThreadPoolExecutor: shared memory, ContextVar set in worker thread
    - ProcessPoolExecutor: separate memory, ContextVar set in child process

    Must be module-level (not a method) for ProcessPoolExecutor pickling.
    """
    token = current_result_id.set(result_id)
    try:
        obj = import_string(func_path)
        # If it's a Task wrapper, get the underlying function
        func = getattr(obj, "func", obj)
        return func(*args, **kwargs)
    finally:
        current_result_id.reset(token)


class FuturesBackend(BaseTaskBackend):
    """Base class for concurrent.futures-based backends."""

    supports_defer = False
    supports_async_task = False
    supports_get_result = True
    supports_priority = False

    executor_class: type = None  # Subclasses must set

    def __init__(self, alias: str, params: dict[str, Any]) -> None:
        super().__init__(alias, params)
        options = params.get("OPTIONS", {})
        max_workers = options.get("MAX_WORKERS", 10)
        max_results = options.get("MAX_RESULTS", 1000)

        self._name = f"tasks-{alias}"
        self._state = get_executor_state(
            name=self._name,
            executor_class=self.executor_class,
            max_workers=max_workers,
            max_results=max_results,
        )
        logger.debug(
            "%s '%s' initialized: max_workers=%d, max_results=%d",
            self.__class__.__name__,
            alias,
            max_workers,
            max_results,
        )

    def enqueue(
        self,
        task: Task,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> TaskResult:
        """Enqueue a task for background execution.

        Returns a TaskResult with status READY. Poll with get_result()
        to check for RUNNING, SUCCESSFUL, or FAILED status.
        """
        self.validate_task(task)
        self._validate_pickleable(task, args, kwargs)

        result_id = str(uuid.uuid4())
        func_path = f"{task.func.__module__}.{task.func.__qualname__}"

        # Store initial result before submitting (for immediate get_result calls)
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

        with self._state.lock:
            self._state.results[result_id] = initial_result

        # Submit to executor
        future = self._state.executor.submit(
            _execute_task, func_path, args or (), kwargs or {}, result_id
        )

        with self._state.lock:
            self._state.futures[result_id] = future

        # Handle completion asynchronously
        future.add_done_callback(
            lambda f: self._on_complete(result_id, task, initial_result, f)
        )

        logger.debug(
            "Task '%s' enqueued: result_id=%s",
            task.name,
            result_id,
        )

        return initial_result

    def _on_complete(
        self,
        result_id: str,
        task: Task,
        initial_result: TaskResult,
        future: Future,
    ) -> None:
        """Called when future completes. Runs in parent process/thread."""
        finished_at = datetime.now(timezone.utc)

        try:
            return_value = future.result()
            final_result = TaskResult(
                id=result_id,
                task=task,
                status=TaskResultStatus.SUCCESSFUL,
                enqueued_at=initial_result.enqueued_at,
                started_at=initial_result.enqueued_at,  # Approximation
                finished_at=finished_at,
                last_attempted_at=initial_result.enqueued_at,
                args=initial_result.args,
                kwargs=initial_result.kwargs,
                backend=self.alias,
                errors=[],
                worker_ids=[],
            )
            # TaskResult is frozen, use object.__setattr__ to set _return_value
            object.__setattr__(final_result, "_return_value", return_value)
        except Exception as e:
            logger.exception("Task %s failed: %s", task.name, e)
            error = TaskError(
                exception_class_path=f"{type(e).__module__}.{type(e).__qualname__}",
                traceback=traceback.format_exc(),
            )
            final_result = TaskResult(
                id=result_id,
                task=task,
                status=TaskResultStatus.FAILED,
                enqueued_at=initial_result.enqueued_at,
                started_at=initial_result.enqueued_at,
                finished_at=finished_at,
                last_attempted_at=initial_result.enqueued_at,
                args=initial_result.args,
                kwargs=initial_result.kwargs,
                backend=self.alias,
                errors=[error],
                worker_ids=[],
            )

        with self._state.lock:
            self._state.results[result_id] = final_result
            self._state.futures.pop(result_id, None)
            self._state.completed_ids.append(result_id)

            # LRU eviction
            while len(self._state.completed_ids) > self._state.max_results:
                old_id = self._state.completed_ids.popleft()
                self._state.results.pop(old_id, None)

    def get_result(self, result_id: str) -> TaskResult:
        """Retrieve a task result by ID.

        Returns current status:
        - READY: Task queued but not yet executing
        - RUNNING: Task currently executing
        - SUCCESSFUL: Task completed successfully
        - FAILED: Task raised an exception
        """
        with self._state.lock:
            future = self._state.futures.get(result_id)
            stored_result = self._state.results.get(result_id)

        if stored_result is None:
            raise TaskResultDoesNotExist(result_id)

        # If future still in flight, determine current status
        if future is not None and not future.done():
            status = (
                TaskResultStatus.RUNNING
                if future.running()
                else TaskResultStatus.READY
            )
            return TaskResult(
                id=result_id,
                task=stored_result.task,
                status=status,
                enqueued_at=stored_result.enqueued_at,
                started_at=stored_result.enqueued_at if status == TaskResultStatus.RUNNING else None,
                finished_at=None,
                last_attempted_at=stored_result.enqueued_at if status == TaskResultStatus.RUNNING else None,
                args=stored_result.args,
                kwargs=stored_result.kwargs,
                backend=self.alias,
                errors=[],
                worker_ids=[],
            )

        return stored_result

    def _validate_pickleable(self, task: Task, args: tuple | None, kwargs: dict | None):
        """Validate arguments can be pickled. Override in ProcessPoolBackend."""
        pass

    def close(self) -> None:
        """Shutdown the shared executor.

        WARNING: This shuts down the executor for ALL backend instances
        using the same alias. Call only during application shutdown.
        """
        shutdown_executor(self._name)


class ThreadPoolBackend(FuturesBackend):
    """Thread-based backend for I/O-bound tasks.

    Use for tasks that spend time waiting on I/O (network, disk, database).
    Tasks run in a ThreadPoolExecutor, sharing memory with the main process.
    """

    executor_class = ThreadPoolExecutor


class ProcessPoolBackend(FuturesBackend):
    """Process-based backend for CPU-bound tasks.

    Use for tasks that need true parallelism (image processing, data analysis).
    Tasks run in a ProcessPoolExecutor, bypassing the GIL.

    Constraints:
    - Task arguments and return values must be pickleable
    - No shared memory with main process (global state changes don't persist)
    """

    executor_class = ProcessPoolExecutor

    def _validate_pickleable(self, task: Task, args: tuple | None, kwargs: dict | None):
        """Fail fast if arguments can't be pickled."""
        try:
            pickle.dumps((args, kwargs))
        except (pickle.PicklingError, TypeError, AttributeError) as e:
            raise ValueError(
                f"Task arguments must be pickleable for ProcessPoolBackend: {e}"
            ) from e
