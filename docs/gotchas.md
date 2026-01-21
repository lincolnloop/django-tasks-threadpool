# Gotchas

## In-Memory Only

**This is the main limitation to understand.**

All task results and the pending task queue exist only in memory. When your process restarts:

- All pending tasks are lost
- All completed task results are gone
- There's no way to recover them

This backend is designed for development and low-volume production where losing tasks on restart is acceptable. If you need persistence, use `django-tasks` with `DatabaseBackend`.

## LRU Eviction

When `MAX_RESULTS` is exceeded, the oldest completed results are evicted. If you try to retrieve an evicted result, you'll get `TaskResultDoesNotExist`.

Tune `MAX_RESULTS` based on your needs, but remember it all lives in memory.

## ContextVars in Child Threads

If your task spawns additional threads, those threads won't inherit ContextVar values:

```python
from django.tasks import task
from tasks_threadpool.backend import current_result_id
import threading

@task
def my_task():
    # Works - we're in the thread pool worker
    result_id = current_result_id.get()  # ✓

    def child_work():
        # Fails - child thread has empty context
        result_id = current_result_id.get()  # ✗ LookupError

    t = threading.Thread(target=child_work)
    t.start()
```

**Workaround:** Copy the context explicitly:

```python
import contextvars

@task
def my_task():
    ctx = contextvars.copy_context()
    t = threading.Thread(target=ctx.run, args=(child_work,))
    t.start()
```

Most tasks don't spawn threads, so this rarely comes up.
