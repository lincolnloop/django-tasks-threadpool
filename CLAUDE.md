# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

django-tasks-threadpool is a thread pool backend for Django 6's built-in tasks framework. It provides background task execution without external infrastructure (no Redis, Celery, or database required). Tasks run in a `ThreadPoolExecutor`, freeing the request thread immediately.

**Requirements:** Python 3.12+, Django 6.0+

## Build Commands

```bash
# Install in development mode with test dependencies
uv sync

# Run all tests
uv run pytest

# Run a single test
uv run pytest tests/test_backend.py::TestEnqueue::test_enqueue_returns_result

# Run tests with coverage
uv run pytest --cov=tasks_threadpool --cov-report=term-missing

# Build package
uv build

# Run example app
uv run example.py

# Serve docs locally
uv run --group docs zensical serve

# Build docs
uv run --group docs zensical build
```

## Architecture

**`tasks_threadpool/backend.py`** - Django backend interface:

- **ThreadPoolBackend**: Implements Django's `BaseTaskBackend`
  - Uses `WorkerPool` for background execution
  - `_execute_task()` handles task lifecycle (READY→RUNNING→SUCCESSFUL/FAILED)
  - `enqueue()` creates UUID, stores initial result, submits to pool
  - `get_result()` retrieves result by ID
- **Context variable**: `current_result_id` allows tasks to access their own result ID

**`tasks_threadpool/pool.py`** - Generic worker pool:

- **WorkerPool**: Priority-aware thread pool with result storage
  - `PriorityQueue` for task ordering (lower priority number = runs first)
  - FIFO ordering within same priority level
  - In-memory result store (`_results` dict) with UUID keys
  - LRU eviction via `_completed_ids` deque when exceeding `MAX_RESULTS`
  - Thread-safe with `Lock` protecting shared state
- **get_pool()**: Returns shared pool instance by name (handles Django creating multiple backend instances)

**Backend capabilities:**

- `supports_defer = False` (no scheduled/delayed execution)
- `supports_async_task = False` (no native async)
- `supports_get_result = True`
- `supports_priority = True` (tasks can specify priority -100 to 100)

## Key Limitation

Results are stored in memory only - lost on restart.
