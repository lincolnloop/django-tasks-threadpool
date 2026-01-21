"""Tests for ThreadPoolBackend."""

import threading
import time

import django
from django.conf import settings

# Configure Django settings before importing tasks
if not settings.configured:
    settings.configure(
        DEBUG=True,
        DATABASES={},
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
        ],
        TASKS={
            "default": {
                "BACKEND": "tasks_threadpool.ThreadPoolBackend",
                "OPTIONS": {
                    "MAX_WORKERS": 2,
                    "MAX_RESULTS": 5,
                },
            }
        },
    )
    django.setup()

import pytest
from django.tasks import TaskResultStatus, task
from django.tasks.exceptions import TaskResultDoesNotExist

from tasks_threadpool import ThreadPoolBackend
from tasks_threadpool.backend import current_result_id

# Module-level task functions (Django requirement)
_received_args = {}
_captured_result_id = {}


@task
def simple_task():
    return "done"


@task
def slow_task():
    time.sleep(0.5)
    return "done"


@task
def add(a, b):
    return a + b


@task
def capture_args(a, b, c=None):
    _received_args["a"] = a
    _received_args["b"] = b
    _received_args["c"] = c


@task
def fail():
    raise ValueError("intentional error")


@task
def capture_result_id():
    _captured_result_id["id"] = current_result_id.get()


@task
def quick():
    return True


@task
def long_running_task():
    time.sleep(1)


@task
def increment():
    return 1


@pytest.fixture
def backend():
    """Create a fresh backend instance for each test."""
    b = ThreadPoolBackend(
        alias="test",
        params={"OPTIONS": {"MAX_WORKERS": 2, "MAX_RESULTS": 5}},
    )
    yield b
    b.close()


@pytest.fixture
def default_backend():
    """Create a backend with default options."""
    b = ThreadPoolBackend(alias="default", params={})
    yield b
    b.close()


@pytest.fixture(autouse=True)
def clear_globals():
    """Clear global state between tests."""
    from tasks_threadpool.pool import _pools

    _received_args.clear()
    _captured_result_id.clear()
    # Clear shared pools to ensure fresh state per test
    _pools.clear()
    yield


class TestBackendInitialization:
    def test_default_options(self, default_backend):
        """Backend uses sensible defaults when no options provided."""
        assert default_backend._pool.max_results == 1000
        assert default_backend._pool.worker_count == 10

    def test_custom_options(self, backend):
        """Backend respects custom MAX_WORKERS and MAX_RESULTS."""
        assert backend._pool.max_results == 5
        assert backend._pool.worker_count == 2

    def test_capability_flags(self, backend):
        """Backend advertises correct capabilities."""
        assert backend.supports_defer is False
        assert backend.supports_async_task is False
        assert backend.supports_get_result is True
        assert backend.supports_priority is True

    def test_multiple_instances_share_state(self):
        """Multiple backend instances with same alias share the same pool."""
        backend1 = ThreadPoolBackend(
            alias="shared", params={"OPTIONS": {"MAX_WORKERS": 2}}
        )
        backend2 = ThreadPoolBackend(
            alias="shared", params={"OPTIONS": {"MAX_WORKERS": 2}}
        )

        # Should share the same pool
        assert backend1._pool is backend2._pool

        # Enqueue on one, retrieve from the other
        result = backend1.enqueue(simple_task)
        time.sleep(0.1)
        retrieved = backend2.get_result(result.id)
        assert retrieved.id == result.id

        backend1.close()


class TestEnqueue:
    def test_enqueue_returns_result(self, backend):
        """enqueue() returns a TaskResult with id and backend."""
        result = backend.enqueue(simple_task)

        assert result.id is not None
        assert result.backend == backend.alias
        # Status could be READY, RUNNING, or SUCCESSFUL depending on timing
        assert result.status in (
            TaskResultStatus.READY,
            TaskResultStatus.RUNNING,
            TaskResultStatus.SUCCESSFUL,
        )

    def test_slow_task_starts_running(self, backend):
        """Slow task status becomes RUNNING when worker starts executing."""
        result = backend.enqueue(slow_task)
        time.sleep(0.1)  # Wait for worker to pick it up

        refreshed = backend.get_result(result.id)
        assert refreshed.status == TaskResultStatus.RUNNING

    def test_enqueue_with_args(self, backend):
        """enqueue() passes args and kwargs to task."""
        backend.enqueue(capture_args, args=(1, 2), kwargs={"c": 3})
        time.sleep(0.1)  # Wait for execution

        assert _received_args == {"a": 1, "b": 2, "c": 3}


class TestTaskExecution:
    def test_successful_task(self, backend):
        """Successful task updates status and stores return value."""
        result = backend.enqueue(add, args=(2, 3))
        time.sleep(0.1)  # Wait for execution

        updated = backend.get_result(result.id)
        assert updated.status == TaskResultStatus.SUCCESSFUL
        assert updated.return_value == 5

    def test_failing_task(self, backend):
        """Failed task updates status and stores error info."""
        result = backend.enqueue(fail)
        time.sleep(0.1)  # Wait for execution

        updated = backend.get_result(result.id)
        assert updated.status == TaskResultStatus.FAILED
        assert len(updated.errors) == 1
        assert "ValueError" in updated.errors[0].exception_class_path
        assert "intentional error" in updated.errors[0].traceback

    def test_current_result_id_context_var(self, backend):
        """Task can access its own result ID via context variable."""
        result = backend.enqueue(capture_result_id)
        time.sleep(0.1)

        assert _captured_result_id["id"] == result.id


class TestGetResult:
    def test_get_existing_result(self, backend):
        """get_result() returns the stored result."""
        result = backend.enqueue(quick)
        retrieved = backend.get_result(result.id)

        assert retrieved.id == result.id

    def test_get_nonexistent_result(self, backend):
        """get_result() raises TaskResultDoesNotExist for unknown ID."""
        with pytest.raises(TaskResultDoesNotExist):
            backend.get_result("nonexistent-id")


class TestEviction:
    def test_evicts_oldest_when_over_limit(self, backend):
        """Results are evicted in FIFO order when exceeding MAX_RESULTS."""
        # Enqueue more tasks than MAX_RESULTS (5)
        results = []
        for _ in range(7):
            results.append(backend.enqueue(quick))
            time.sleep(0.05)  # Stagger to ensure ordering

        time.sleep(0.3)  # Wait for all to complete

        # First 2 should be evicted
        for old_result in results[:2]:
            with pytest.raises(TaskResultDoesNotExist):
                backend.get_result(old_result.id)

        # Last 5 should still exist
        for recent_result in results[2:]:
            retrieved = backend.get_result(recent_result.id)
            assert retrieved.status == TaskResultStatus.SUCCESSFUL


class TestClose:
    def test_close_shuts_down_workers(self, backend):
        """close() signals workers to stop."""
        backend.enqueue(long_running_task)
        backend.close()

        assert backend._pool.is_shutdown


class TestConcurrency:
    def test_thread_safety(self, backend):
        """Backend handles concurrent enqueues safely."""
        results = []
        lock = threading.Lock()

        def enqueue_many():
            for _ in range(10):
                r = backend.enqueue(increment)
                with lock:
                    results.append(r)

        threads = [threading.Thread(target=enqueue_many) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        time.sleep(0.5)  # Wait for completion

        # All 30 enqueues should succeed (though some may be evicted)
        assert len(results) == 30


# Tasks for priority testing - defined at module level
_execution_order = []
_execution_lock = threading.Lock()


@task
def record_execution(label):
    """Record when this task executes."""
    with _execution_lock:
        _execution_order.append(label)
    return label


@task(priority=-50)
def high_priority_task():
    """A high priority task (lower number = higher priority)."""
    with _execution_lock:
        _execution_order.append("high")
    return "high"


@task(priority=50)
def low_priority_task():
    """A low priority task."""
    with _execution_lock:
        _execution_order.append("low")
    return "low"


@task
def blocking_task():
    """Blocks to allow queue to fill up."""
    time.sleep(0.3)
    with _execution_lock:
        _execution_order.append("blocking")


class TestPriority:
    @pytest.fixture(autouse=True)
    def clear_execution_order(self):
        """Clear execution order tracking between tests."""
        _execution_order.clear()
        yield

    def test_priority_higher_runs_first(self):
        """Tasks with lower priority number (higher priority) run before higher numbers."""
        # Use a single worker so we can control execution order
        backend = ThreadPoolBackend(
            alias="priority_test",
            params={"OPTIONS": {"MAX_WORKERS": 1, "MAX_RESULTS": 10}},
        )

        try:
            # First enqueue a blocking task to hold the single worker
            backend.enqueue(blocking_task)
            time.sleep(0.05)  # Let it start

            # Now enqueue low priority first, then high priority
            # If priority works, high should execute before low
            backend.enqueue(low_priority_task)
            backend.enqueue(high_priority_task)

            # Wait for all tasks to complete
            time.sleep(0.6)

            # Blocking should be first (it started immediately)
            # Then high priority (-50), then low priority (50)
            assert _execution_order == ["blocking", "high", "low"]
        finally:
            backend.close()

    def test_same_priority_fifo(self):
        """Tasks with same priority maintain FIFO order."""
        backend = ThreadPoolBackend(
            alias="fifo_test",
            params={"OPTIONS": {"MAX_WORKERS": 1, "MAX_RESULTS": 10}},
        )

        try:
            # Block the worker
            backend.enqueue(blocking_task)
            time.sleep(0.05)

            # Enqueue several tasks with default priority (0) - should stay FIFO
            backend.enqueue(record_execution, args=("first",))
            backend.enqueue(record_execution, args=("second",))
            backend.enqueue(record_execution, args=("third",))

            time.sleep(0.6)

            # Should execute in enqueue order
            assert _execution_order == ["blocking", "first", "second", "third"]
        finally:
            backend.close()

    def test_priority_with_using(self):
        """Priority can be modified at runtime with task.using()."""
        backend = ThreadPoolBackend(
            alias="using_test",
            params={"OPTIONS": {"MAX_WORKERS": 1, "MAX_RESULTS": 10}},
        )

        try:
            backend.enqueue(blocking_task)
            time.sleep(0.05)

            # Use .using() to override default priority
            backend.enqueue(record_execution.using(priority=50), args=("low",))
            backend.enqueue(record_execution.using(priority=-50), args=("high",))

            time.sleep(0.6)

            assert _execution_order == ["blocking", "high", "low"]
        finally:
            backend.close()
