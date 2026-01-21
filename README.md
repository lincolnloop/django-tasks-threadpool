# django-tasks-local

Django 6 ships with `ImmediateBackend` (blocks the request) and `DummyBackend` (does nothing).

This package provides **background execution with zero infrastructure**. Tasks run in a thread or process pool, freeing your request immediately.

Ideal for development and low-volume production.

## Installation

```bash
pip install django-tasks-local
```

## Usage

### ThreadPoolBackend (I/O-bound tasks)

Best for tasks that wait on I/O: sending emails, calling APIs, database operations.

```python
# settings.py
TASKS = {
    "default": {
        "BACKEND": "django_tasks_local.ThreadPoolBackend",
        "OPTIONS": {
            "MAX_WORKERS": 10,    # Thread pool size (default: 10)
            "MAX_RESULTS": 1000,  # Result retention limit (default: 1000)
        }
    }
}
```

### ProcessPoolBackend (CPU-bound tasks)

Best for tasks that need true parallelism: image processing, data analysis, PDF generation.

```python
# settings.py
TASKS = {
    "default": {
        "BACKEND": "django_tasks_local.ThreadPoolBackend",
    },
    "cpu_intensive": {
        "BACKEND": "django_tasks_local.ProcessPoolBackend",
        "OPTIONS": {
            "MAX_WORKERS": 4,  # Typically number of CPU cores
        }
    }
}
```

**Note:** ProcessPoolBackend requires task arguments and return values to be pickleable.

### Using Tasks

```python
from django.tasks import task

@task
def send_welcome_email(user_id):
    # This runs in a background thread
    ...

# In your view
send_welcome_email.enqueue(user.id)
```

### Accessing Result ID from Within a Task

```python
from django_tasks_local import current_result_id

@task
def my_task():
    result_id = current_result_id.get()
    # Use result_id for logging, caching progress, etc.
```

See [example.py](example.py) for a complete working example with SSE progress streaming.

## Backend Capabilities

|                        | ThreadPoolBackend | ProcessPoolBackend |
| ---------------------- | ----------------- | ------------------ |
| `supports_priority`    | No                | No                 |
| `supports_defer`       | No                | No                 |
| `supports_get_result`  | Yes               | Yes                |
| `current_result_id`    | Yes               | Yes                |
| Pickling required      | No                | Yes                |
| GIL-free parallelism   | No                | Yes                |

## In-Memory Only

Task results live in memory. When your process restarts, pending tasks and results are gone. This is fine for development and use cases where losing a task on deploy is acceptable.

For persistence, see [django-tasks](https://pypi.org/project/django-tasks/) which provides a `DatabaseBackend` and `RQBackend`.
