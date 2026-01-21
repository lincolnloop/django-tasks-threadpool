# API Reference

## ThreadPoolBackend

```python
from tasks_threadpool import ThreadPoolBackend
```

A Django Tasks backend that executes tasks in a thread pool.

### Class Attributes

| Attribute | Value | Description |
|-----------|-------|-------------|
| `supports_defer` | `False` | No scheduled/delayed execution |
| `supports_async_task` | `False` | No native async support |
| `supports_get_result` | `True` | Results can be retrieved by ID |

### Methods

#### `enqueue(task, args=None, kwargs=None)`

Enqueue a task for background execution.

Returns a `TaskResult` with the current status (`READY`, `RUNNING`, or `SUCCESSFUL`). Call `result.refresh()` to get the latest status.

#### `get_result(result_id)`

Retrieve a task result by its UUID string.

Raises `TaskResultDoesNotExist` if not found (either never existed or was evicted).

#### `close()`

Shut down the thread pool. Waits for running tasks to complete and cancels queued tasks.

## Context Variable

### `current_result_id`

A `ContextVar[str]` that holds the current task's result ID. Only available within a running task.

```python
from tasks_threadpool.backend import current_result_id

@task
def my_task():
    result_id = current_result_id.get()
```
