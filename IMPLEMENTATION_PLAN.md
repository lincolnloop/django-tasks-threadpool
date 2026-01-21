# Implementation Plan: Concurrent Futures Backend

## Objective

Refactor `django-tasks-threadpool` to use Python's standard `concurrent.futures` module, enabling both **ThreadPoolExecutor** (I/O-bound) and **ProcessPoolExecutor** (CPU-bound) backends with a shared codebase. Maintains the "zero infrastructure" philosophy.

**Package name:** `django-tasks-local`

## Architecture

```
django.tasks.backends.base.BaseTaskBackend
    └── FuturesBackend (abstract base)
            ├── ThreadPoolBackend (ThreadPoolExecutor)
            └── ProcessPoolBackend (ProcessPoolExecutor)
```

### Backend Capabilities

|                        | ThreadPoolBackend | ProcessPoolBackend |
| ---------------------- | ----------------- | ------------------ |
| `supports_priority`    | False             | False              |
| `supports_defer`       | False             | False              |
| `supports_get_result`  | True              | True               |
| `current_result_id`    | Yes               | Yes                |
| Pickling required      | No                | Yes                |
| GIL-free parallelism   | No                | Yes                |

**Priority support:** Dropped. `concurrent.futures` executors don't support priority queues. Implementing priority on top adds complexity that defeats the "simple standard library" goal.

## Implementation Details

### Executor State Registry

Django may instantiate multiple backend instances (e.g., per-thread). We need shared executors **and shared result storage** to avoid resource waste and ensure result lookups work across instances.

```python
from concurrent.futures import Executor, Future, ThreadPoolExecutor, ProcessPoolExecutor
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

@dataclass
class ExecutorState:
    """Shared state for all backend instances using the same executor."""
    executor: Executor
    futures: dict[str, Future] = field(default_factory=dict)
    results: dict[str, TaskResult] = field(default_factory=dict)
    completed_ids: deque[str] = field(default_factory=deque)
    max_results: int = 1000
    lock: Lock = field(default_factory=Lock)


_executor_states: dict[str, ExecutorState] = {}
_registry_lock = Lock()


def get_executor_state(
    name: str,
    executor_class: type[Executor],
    max_workers: int,
    max_results: int,
) -> ExecutorState:
    """Get or create shared executor state by name.

    Multiple backend instances with the same name share:
    - The executor (thread/process pool)
    - The futures dict (in-flight tasks)
    - The results dict (completed tasks)
    """
    with _registry_lock:
        if name not in _executor_states:
            _executor_states[name] = ExecutorState(
                executor=executor_class(max_workers=max_workers),
                max_results=max_results,
            )
        return _executor_states[name]


def shutdown_executor(name: str, wait: bool = True) -> None:
    """Shutdown a shared executor by name. Affects all backend instances using it."""
    with _registry_lock:
        state = _executor_states.pop(name, None)
        if state:
            state.executor.shutdown(wait=wait)
```

### Task Wrapper Function

Must be module-level (not a method) for ProcessPoolExecutor pickling support.

```python
from contextvars import ContextVar
from django.utils.module_loading import import_string

current_result_id: ContextVar[str] = ContextVar("current_result_id")


def _execute_task(func_path: str, args: tuple, kwargs: dict, result_id: str):
    """
    Execute task in worker thread/process. Sets ContextVar for task access.

    ContextVar works in both backends:
    - ThreadPoolExecutor: shared memory, ContextVar set in worker thread
    - ProcessPoolExecutor: separate memory, ContextVar set in child process
    """
    token = current_result_id.set(result_id)
    try:
        func = import_string(func_path)
        return func(*args, **kwargs)
    finally:
        current_result_id.reset(token)
```

**Note:** We pass `func_path` (string) instead of the function object because:
- Decorated functions often aren't pickleable
- Methods and closures can't be pickled
- Import path resolution works reliably across process boundaries

### Task Status Lifecycle

```
READY → RUNNING → SUCCESSFUL
                → FAILED
```

`concurrent.futures` has no "task started" callback, but `Future.running()` and `Future.done()` can be checked on demand. We determine status at `get_result()` time:

- Future exists, not done, `running()` is False → `READY` (queued in executor)
- Future exists, not done, `running()` is True → `RUNNING` (currently executing)
- Future done or no future → check stored result → `SUCCESSFUL` or `FAILED`

### FuturesBackend Base Class

```python
class FuturesBackend(BaseTaskBackend):
    """Base class for concurrent.futures-based backends."""

    supports_defer = False
    supports_async_task = False
    supports_get_result = True
    supports_priority = False

    executor_class: type[Executor] = None  # Subclasses must set

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

    def enqueue(
        self,
        task: Task,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> TaskResult:
        self.validate_task(task)
        self._validate_pickleable(task, args, kwargs)  # ProcessPool only

        result_id = str(uuid.uuid4())
        func_path = f"{task.func.__module__}.{task.func.__qualname__}"

        # Submit to executor
        future = self._submit(func_path, args or (), kwargs or {}, result_id)

        # Store initial result
        initial_result = TaskResult(
            id=result_id,
            task=task,
            status=TaskResultStatus.READY,
            enqueued_at=datetime.now(timezone.utc),
            args=list(args or ()),
            kwargs=dict(kwargs or {}),
            backend=self.alias,
            # ... other fields
        )

        with self._state.lock:
            self._state.futures[result_id] = future
            self._state.results[result_id] = initial_result

        # Handle completion asynchronously
        future.add_done_callback(
            lambda f: self._on_complete(result_id, task, initial_result, f)
        )

        return initial_result

    def _submit(self, func_path, args, kwargs, result_id) -> Future:
        """Submit task to executor."""
        return self._state.executor.submit(
            _execute_task, func_path, args, kwargs, result_id
        )

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
                args=initial_result.args,
                kwargs=initial_result.kwargs,
                backend=self.alias,
                errors=[],
            )
            object.__setattr__(final_result, "_return_value", return_value)
        except Exception as e:
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
                args=initial_result.args,
                kwargs=initial_result.kwargs,
                backend=self.alias,
                errors=[error],
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
                args=stored_result.args,
                kwargs=stored_result.kwargs,
                backend=self.alias,
                errors=[],
            )

        return stored_result

    def _validate_pickleable(self, task, args, kwargs):
        """Override in ProcessPoolBackend to check pickling."""
        pass

    def close(self) -> None:
        """Shutdown the shared executor.

        WARNING: This shuts down the executor for ALL backend instances
        using the same alias. Call only during application shutdown.
        """
        shutdown_executor(self._name)
```

### ThreadPoolBackend

```python
class ThreadPoolBackend(FuturesBackend):
    """Thread-based backend for I/O-bound tasks."""

    executor_class = ThreadPoolExecutor
```

### ProcessPoolBackend

```python
class ProcessPoolBackend(FuturesBackend):
    """Process-based backend for CPU-bound tasks."""

    executor_class = ProcessPoolExecutor

    def _validate_pickleable(self, task, args, kwargs):
        """Fail fast if arguments can't be pickled."""
        import pickle
        try:
            pickle.dumps((args, kwargs))
        except (pickle.PicklingError, TypeError, AttributeError) as e:
            raise ValueError(
                f"Task arguments must be pickleable for ProcessPoolBackend: {e}"
            ) from e
```

## Error Handling

| Error                         | Handling                                              |
| ----------------------------- | ----------------------------------------------------- |
| Unpickleable arguments        | `ValueError` raised in `enqueue()` (ProcessPool only) |
| Unpickleable return value     | `PicklingError` captured as task failure              |
| Worker process crash          | `BrokenProcessPool` → task marked FAILED              |
| Function import fails         | `ImportError` captured as task failure                |
| Executor shutdown during task | Task marked FAILED with appropriate error             |

## Configuration

```python
TASKS = {
    "default": {
        "BACKEND": "django_tasks_local.ThreadPoolBackend",
        "OPTIONS": {
            "MAX_WORKERS": 10,
            "MAX_RESULTS": 1000,
        }
    },
    "cpu_intensive": {
        "BACKEND": "django_tasks_local.ProcessPoolBackend",
        "OPTIONS": {
            "MAX_WORKERS": 4,  # Typically num_cpus
            "MAX_RESULTS": 500,
        }
    }
}
```

## Migration Steps

1. **Create `django_tasks_local/` package**
   - `__init__.py` - exports `ThreadPoolBackend`, `ProcessPoolBackend`, `current_result_id`
   - `backend.py` - `FuturesBackend`, `ThreadPoolBackend`, `ProcessPoolBackend`, wrapper functions
   - `state.py` - `ExecutorState`, `get_executor_state()`, `shutdown_executor()`

2. **Remove old implementation**
   - Delete `tasks_threadpool/` directory

3. **Update package metadata**
   - Rename to `django-tasks-local` in `pyproject.toml`
   - Update README

4. **Update example.py**
   - Demonstrate both backends
   - Show CPU-bound vs I/O-bound task routing

5. **Update documentation**
   - New capabilities table
   - ProcessPoolBackend constraints (pickling, no ContextVar)
   - Configuration examples

## Testing Strategy

### Unit Tests

- `test_thread_backend.py` - ThreadPoolBackend functionality
- `test_process_backend.py` - ProcessPoolBackend functionality
- `test_executor_registry.py` - Shared executor management

### Specific Test Cases

```python
# Pickling validation
def test_process_backend_rejects_unpickleable_args():
    backend = ProcessPoolBackend(...)
    with pytest.raises(ValueError, match="pickleable"):
        backend.enqueue(my_task, args=(lambda: None,))  # lambdas can't pickle

# ContextVar works in both backends
@pytest.mark.parametrize("backend_class", [ThreadPoolBackend, ProcessPoolBackend])
def test_context_var_access(backend_class):
    @task
    def get_own_id():
        return current_result_id.get()

    backend = backend_class("default", {})
    result = backend.enqueue(get_own_id)
    # Wait for completion...
    assert result.return_value == result.id

# Process isolation
def test_process_backend_no_shared_state():
    # Verify global mutations in task don't affect parent

# Executor sharing
def test_multiple_backends_share_state():
    b1 = ThreadPoolBackend("default", {})
    b2 = ThreadPoolBackend("default", {})
    assert b1._state is b2._state

# Error handling
def test_broken_process_pool_recovery():
    # Kill a worker, verify graceful degradation
```

## Design Decisions

1. **Executor cleanup:** Implement `close()` calling `shutdown_executor()` for Django lifecycle and test isolation. No explicit `atexit` registration needed - Python 3.12+ handles this automatically for `concurrent.futures` executors.

   **Note:** `close()` shuts down the shared executor for ALL backend instances using the same alias. This is intentional - executors are shared resources. Only call `close()` during application shutdown, not on individual backend instances during normal operation.

2. **Priority support:** Not supported. `concurrent.futures` executors are FIFO. Implementing priority via a wrapper adds complexity that defeats the "simple standard library" goal. `supports_priority = False` on both backends.

3. **Shared state:** Executor and result storage are shared via `ExecutorState` registry. Multiple backend instances with the same alias share the same executor and can access each other's results. This handles Django's pattern of creating multiple backend instances (e.g., per-thread).
