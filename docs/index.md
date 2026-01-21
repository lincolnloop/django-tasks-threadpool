# django-tasks-threadpool

A thread pool backend for Django 6's built-in tasks framework.

## Why?

Django 6 ships with `ImmediateBackend` (blocks the request) and `DummyBackend` (does nothing).

This backend provides **background execution with zero infrastructure**. Tasks run in a thread pool, freeing your request immediately.

Ideal for development and low-volume production.

## Installation

```bash
pip install django-tasks-threadpool
```

## Quick Start

```python
# settings.py
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

```python
# views.py
from django.tasks import task

@task
def send_welcome_email(user_id):
    # This runs in a background thread
    ...

# In your view
send_welcome_email.enqueue(user.id)
```

## Limitations

- Results are stored in memory (lost on restart)
- No scheduled/delayed execution (`supports_defer = False`)
- No native async support (`supports_async_task = False`)

See [Gotchas](gotchas.md) for edge cases to be aware of.
