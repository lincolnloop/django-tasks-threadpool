# Usage

## Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| `MAX_WORKERS` | 10 | Number of threads in the pool |
| `MAX_RESULTS` | 1000 | Maximum results to keep in memory (LRU eviction) |

```python
TASKS = {
    "default": {
        "BACKEND": "tasks_threadpool.ThreadPoolBackend",
        "OPTIONS": {
            "MAX_WORKERS": 10,
            "MAX_RESULTS": 1000,
        }
    }
}
```

## Accessing Task Result ID

Tasks can access their own result ID via a context variable:

```python
from django.tasks import task
from tasks_threadpool.backend import current_result_id

@task
def my_task():
    result_id = current_result_id.get()
    # Use result_id for logging, progress tracking, etc.
```

## Retrieving Results

```python
from django.tasks import task

@task
def add(a, b):
    return a + b

# Enqueue and get result object
result = add.enqueue(2, 3)
print(result.id)      # UUID string
print(result.status)  # READY, RUNNING, or SUCCESSFUL depending on timing

# Refresh to get latest status
result.refresh()
print(result.status)  # TaskResultStatus.RUNNING or SUCCESSFUL

# Or retrieve by ID
from django.tasks import get_task_backend

backend = get_task_backend()
result = backend.get_result(result.id)
print(result.status)        # TaskResultStatus.SUCCESSFUL
print(result.return_value)  # 5
```

