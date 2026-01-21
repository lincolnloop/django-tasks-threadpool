"""Tests for WorkerPool."""

import threading
import time


from tasks_threadpool.pool import PrioritizedItem, WorkerPool


class TestPrioritizedItem:
    def test_ordering_by_priority(self):
        """Items are ordered by priority (lower first)."""
        high = PrioritizedItem(priority=-10, sequence=0, item="high")
        low = PrioritizedItem(priority=10, sequence=1, item="low")

        assert high < low

    def test_ordering_by_sequence_when_same_priority(self):
        """Items with same priority are ordered by sequence (FIFO)."""
        first = PrioritizedItem(priority=0, sequence=0, item="first")
        second = PrioritizedItem(priority=0, sequence=1, item="second")

        assert first < second

    def test_item_not_compared(self):
        """The item field is not used in comparisons."""
        a = PrioritizedItem(priority=0, sequence=0, item="zzz")
        b = PrioritizedItem(priority=0, sequence=0, item="aaa")

        # Should be equal since priority and sequence match
        assert not (a < b) and not (b < a)


class TestWorkerPool:
    def test_basic_submit(self):
        """Pool processes submitted items and stores results."""

        def handler(result_id, item):
            return f"processed:{item}"

        pool = WorkerPool(handler=handler, max_workers=2, name="test")
        try:
            pool.submit("id1", "item1")
            pool.submit("id2", "item2")
            time.sleep(0.2)

            assert pool.get_result("id1") == "processed:item1"
            assert pool.get_result("id2") == "processed:item2"
        finally:
            pool.shutdown()

    def test_initial_result(self):
        """Initial result is available immediately after submit."""

        def handler(result_id, item):
            time.sleep(0.5)
            return "done"

        pool = WorkerPool(handler=handler, max_workers=1, name="initial")
        try:
            pool.submit("id1", "item", initial_result="pending")
            # Should be available immediately
            assert pool.get_result("id1") == "pending"
        finally:
            pool.shutdown()

    def test_priority_ordering(self):
        """Higher priority items (lower numbers) are processed first."""
        results = []
        lock = threading.Lock()
        started = threading.Event()

        def handler(result_id, item):
            if item == "blocking":
                started.set()
                time.sleep(0.3)
            with lock:
                results.append(item)
            return item

        pool = WorkerPool(handler=handler, max_workers=1, name="priority")
        try:
            # Block the single worker
            pool.submit("b", "blocking", priority=0)
            started.wait()

            # Submit low priority first, then high priority
            pool.submit("l", "low", priority=50)
            pool.submit("h", "high", priority=-50)

            time.sleep(0.5)

            # High priority should run before low
            assert results == ["blocking", "high", "low"]
        finally:
            pool.shutdown()

    def test_fifo_within_same_priority(self):
        """Items with same priority maintain FIFO order."""
        results = []
        lock = threading.Lock()
        started = threading.Event()

        def handler(result_id, item):
            if item == "blocking":
                started.set()
                time.sleep(0.2)
            with lock:
                results.append(item)
            return item

        pool = WorkerPool(handler=handler, max_workers=1, name="fifo")
        try:
            pool.submit("b", "blocking", priority=0)
            started.wait()

            pool.submit("1", "first", priority=0)
            pool.submit("2", "second", priority=0)
            pool.submit("3", "third", priority=0)

            time.sleep(0.5)

            assert results == ["blocking", "first", "second", "third"]
        finally:
            pool.shutdown()

    def test_worker_count(self):
        """Pool creates correct number of workers."""
        pool = WorkerPool(handler=lambda r, x: None, max_workers=5, name="count")
        try:
            assert pool.worker_count == 5
        finally:
            pool.shutdown()

    def test_max_results(self):
        """Pool exposes max_results setting."""
        pool = WorkerPool(handler=lambda r, x: None, max_results=500, name="max")
        try:
            assert pool.max_results == 500
        finally:
            pool.shutdown()

    def test_shutdown(self):
        """Shutdown stops the pool."""
        pool = WorkerPool(handler=lambda r, x: None, max_workers=2, name="shutdown")

        assert not pool.is_shutdown
        pool.shutdown()
        assert pool.is_shutdown

    def test_shutdown_waits_for_current_task(self):
        """Shutdown waits for currently running task to complete."""
        completed = threading.Event()

        def handler(result_id, item):
            time.sleep(0.2)
            completed.set()
            return "done"

        pool = WorkerPool(handler=handler, max_workers=1, name="wait")
        pool.submit("id", "task")
        time.sleep(0.05)  # Let it start

        pool.shutdown(wait=True, timeout=1.0)
        assert completed.is_set()

    def test_concurrent_submits(self):
        """Pool handles concurrent submissions safely."""
        results = []
        lock = threading.Lock()

        def handler(result_id, item):
            with lock:
                results.append(item)
            return item

        pool = WorkerPool(handler=handler, max_workers=4, name="concurrent")
        try:
            threads = []
            for i in range(10):

                def submit_items(start):
                    for j in range(5):
                        pool.submit(f"{start}-{j}", f"item-{start}-{j}")

                t = threading.Thread(target=submit_items, args=(i,))
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            time.sleep(0.5)
            assert len(results) == 50
        finally:
            pool.shutdown()

    def test_handler_exception_doesnt_crash_worker(self):
        """Worker continues processing after handler raises exception."""
        results = []

        def handler(result_id, item):
            if item == "fail":
                raise ValueError("intentional")
            results.append(item)
            return item

        pool = WorkerPool(handler=handler, max_workers=1, name="exception")
        try:
            pool.submit("1", "before")
            pool.submit("2", "fail")
            pool.submit("3", "after")

            time.sleep(0.3)

            assert results == ["before", "after"]
        finally:
            pool.shutdown()

    def test_result_eviction(self):
        """Old results are evicted when exceeding max_results."""

        def handler(result_id, item):
            return f"result:{item}"

        pool = WorkerPool(handler=handler, max_workers=2, max_results=3, name="evict")
        try:
            # Submit 5 items
            for i in range(5):
                pool.submit(f"id{i}", f"item{i}")
                time.sleep(0.05)  # Stagger to ensure ordering

            time.sleep(0.3)

            # First 2 should be evicted
            assert pool.get_result("id0") is None
            assert pool.get_result("id1") is None

            # Last 3 should still exist
            assert pool.get_result("id2") == "result:item2"
            assert pool.get_result("id3") == "result:item3"
            assert pool.get_result("id4") == "result:item4"
        finally:
            pool.shutdown()

    def test_has_result(self):
        """has_result returns True for existing results."""

        def handler(result_id, item):
            return item

        pool = WorkerPool(handler=handler, max_workers=1, name="has")
        try:
            pool.submit("exists", "item", initial_result="pending")
            assert pool.has_result("exists")
            assert not pool.has_result("missing")
        finally:
            pool.shutdown()

    def test_update_result(self):
        """update_result modifies existing results."""

        def handler(result_id, item):
            time.sleep(0.5)  # Slow handler
            return "final"

        pool = WorkerPool(handler=handler, max_workers=1, name="update")
        try:
            pool.submit("id", "item", initial_result="initial")
            assert pool.get_result("id") == "initial"

            # Update before handler finishes
            assert pool.update_result("id", "updated")
            assert pool.get_result("id") == "updated"

            # Can't update non-existent result
            assert not pool.update_result("missing", "value")
        finally:
            pool.shutdown()
