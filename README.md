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

## Usage

```python
# settings.py
TASKS = {
    "default": {
        "BACKEND": "tasks_threadpool.ThreadPoolBackend",
        "OPTIONS": {
            "MAX_WORKERS": 10,    # Thread pool size (default: 10)
            "MAX_RESULTS": 1000,  # Result retention limit (default: 1000)
        }
    }
}
```

Then use Django's tasks as normal:

```python
from django.tasks import task

@task
def send_welcome_email(user_id):
    # This runs in a background thread
    ...

# In your view
send_welcome_email.enqueue(user.id)
```

See [example.py](example.py) for a complete working example.

## In-Memory Only

Task results live in memory. When your process restarts, pending tasks and results are gone. This is fine for development and use cases where losing a task on deploy is acceptable.

For persistence, see [django-tasks](https://pypi.org/project/django-tasks/) which provides a `DatabaseBackend` and `RQBackend`.
