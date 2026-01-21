# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

django-tasks-local provides zero-infrastructure task backends for Django 6's built-in tasks framework. It offers both `ThreadPoolBackend` (I/O-bound) and `ProcessPoolBackend` (CPU-bound) using Python's standard `concurrent.futures` module.

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
uv run pytest --cov=django_tasks_local --cov-report=term-missing

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

**`django_tasks_local/state.py`** - Shared executor state registry:

- **ExecutorState**: Dataclass holding executor, futures, results, and lock
- **get_executor_state()**: Returns shared state by name (handles Django creating multiple backend instances)
- **shutdown_executor()**: Cleanly shuts down a shared executor

**`django_tasks_local/backend.py`** - Django backend implementations:

- **FuturesBackend**: Abstract base class implementing Django's `BaseTaskBackend`
  - Uses `concurrent.futures` executors for background execution
  - `enqueue()` creates UUID, stores initial result, submits to executor
  - `get_result()` retrieves result by ID, checks Future state for RUNNING status
  - `_on_complete()` callback handles task completion and LRU eviction
- **ThreadPoolBackend**: Uses `ThreadPoolExecutor` for I/O-bound tasks
- **ProcessPoolBackend**: Uses `ProcessPoolExecutor` for CPU-bound tasks
  - Validates pickling of arguments upfront
- **_execute_task()**: Module-level wrapper that sets ContextVar and invokes the task function
- **current_result_id**: ContextVar allowing tasks to access their own result ID

**Backend capabilities:**

- `supports_defer = False` (no scheduled/delayed execution)
- `supports_async_task = False` (no native async)
- `supports_get_result = True`
- `supports_priority = False` (concurrent.futures doesn't support priority)

## Key Design Decisions

1. **Shared state registry**: Multiple backend instances with the same alias share executor and result storage via `ExecutorState`
2. **Task status from Future**: READY/RUNNING determined by `Future.running()` at `get_result()` time
3. **ContextVar in both backends**: Works by setting the ContextVar inside the worker thread/process
4. **Function path resolution**: Tasks are imported by path string for ProcessPool pickling compatibility

## Key Limitation

Results are stored in memory only - lost on restart.
