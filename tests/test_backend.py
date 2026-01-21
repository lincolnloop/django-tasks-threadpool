"""Tests for FuturesBackend, ThreadPoolBackend, and ProcessPoolBackend."""

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
                "BACKEND": "django_tasks_local.ThreadPoolBackend",
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

from django_tasks_local import ThreadPoolBackend, ProcessPoolBackend, current_result_id
from django_tasks_local.state import _executor_states

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
def return_result_id():
    """Return the current result ID (for cross-process testing)."""
    return current_result_id.get()


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
    b = ThreadPoolBackend(alias="default_test", params={})
    yield b
    b.close()


@pytest.fixture(autouse=True)
def clear_globals():
    """Clear global state between tests."""
    _received_args.clear()
    _captured_result_id.clear()
    # Clear shared executor states to ensure fresh state per test
    _executor_states.clear()
    yield


class TestBackendInitialization:
    def test_default_options(self, default_backend):
        """Backend uses sensible defaults when no options provided."""
        assert default_backend._state.max_results == 1000

    def test_custom_options(self, backend):
        """Backend respects custom MAX_WORKERS and MAX_RESULTS."""
        assert backend._state.max_results == 5

    def test_capability_flags(self, backend):
        """Backend advertises correct capabilities."""
        assert backend.supports_defer is False
        assert backend.supports_async_task is False
        assert backend.supports_get_result is True
        assert backend.supports_priority is False

    def test_multiple_instances_share_state(self):
        """Multiple backend instances with same alias share the same state."""
        backend1 = ThreadPoolBackend(
            alias="shared", params={"OPTIONS": {"MAX_WORKERS": 2}}
        )
        backend2 = ThreadPoolBackend(
            alias="shared", params={"OPTIONS": {"MAX_WORKERS": 2}}
        )

        # Should share the same state
        assert backend1._state is backend2._state

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
        # Status should be READY initially
        assert result.status == TaskResultStatus.READY

    def test_slow_task_becomes_running(self, backend):
        """Slow task status becomes RUNNING when worker starts executing."""
        result = backend.enqueue(slow_task)
        time.sleep(0.1)  # Wait for worker to pick it up

        refreshed = backend.get_result(result.id)
        assert refreshed.status in (TaskResultStatus.RUNNING, TaskResultStatus.SUCCESSFUL)

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
    def test_close_shuts_down_executor(self, backend):
        """close() shuts down the executor."""
        backend.enqueue(long_running_task)
        backend.close()

        # Executor should be shut down (removed from registry)
        assert backend._name not in _executor_states


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


class TestProcessPoolBackend:
    @pytest.fixture
    def process_backend(self):
        """Create a ProcessPoolBackend for testing."""
        b = ProcessPoolBackend(
            alias="process_test",
            params={"OPTIONS": {"MAX_WORKERS": 2, "MAX_RESULTS": 5}},
        )
        yield b
        b.close()

    def test_basic_execution(self, process_backend):
        """ProcessPoolBackend executes tasks correctly."""
        result = process_backend.enqueue(add, args=(2, 3))
        time.sleep(0.5)  # ProcessPool is slower to start

        updated = process_backend.get_result(result.id)
        assert updated.status == TaskResultStatus.SUCCESSFUL
        assert updated.return_value == 5

    def test_context_var_in_process(self, process_backend):
        """ContextVar works in ProcessPoolBackend."""
        # Note: We use return_result_id which returns the value rather
        # than capture_result_id which stores in a global (globals don't
        # cross process boundaries).
        result = process_backend.enqueue(return_result_id)
        time.sleep(0.5)

        updated = process_backend.get_result(result.id)
        assert updated.status == TaskResultStatus.SUCCESSFUL
        assert updated.return_value == result.id

    def test_rejects_unpickleable_args(self, process_backend):
        """ProcessPoolBackend raises ValueError for unpickleable arguments."""
        with pytest.raises(ValueError, match="pickleable"):
            process_backend.enqueue(simple_task, args=(lambda: None,))

    def test_capability_flags(self, process_backend):
        """ProcessPoolBackend advertises correct capabilities."""
        assert process_backend.supports_defer is False
        assert process_backend.supports_async_task is False
        assert process_backend.supports_get_result is True
        assert process_backend.supports_priority is False
