"""Offline reservation/lifecycle tests. No Flask, database or hardware imports."""

import threading
import unittest
from unittest.mock import patch

from apriltag_test import JobCapacityError, JobConflictError, JobRegistry


class JobRegistryTests(unittest.TestCase):
    def test_hard_cap_and_idempotency_when_full(self):
        registry = JobRegistry(max_jobs=1)
        job, created = registry.create("same", 5)
        self.assertTrue(created)
        self.assertEqual(registry.create("same", "5"), (job, False))
        with self.assertRaises(JobCapacityError):
            registry.create("other", 5)
        self.assertEqual(len(registry._jobs), 1)

    def test_conflict_takes_precedence_over_capacity(self):
        registry = JobRegistry(max_jobs=1)
        registry.create("same", 5)
        with self.assertRaises(JobConflictError):
            registry.create("same", 6)

    def test_request_ids_are_normalized(self):
        registry = JobRegistry()
        job, _ = registry.create(123, 5)
        self.assertIs(registry.get("123"), job)
        self.assertEqual(registry.create("123", 5), (job, False))

    def test_logical_terminal_states_never_release_a_worker_slot(self):
        for terminal in ("succeed", "fail", "cancelled", "timed_out"):
            with self.subTest(terminal=terminal):
                registry = JobRegistry(max_jobs=1, ttl_seconds=0)
                job, _ = registry.create("stuck", 5)
                if terminal == "succeed":
                    job.succeed({"success": True})
                elif terminal == "fail":
                    job.fail("error", "failed")
                else:
                    getattr(job, terminal)()
                job.finished_at -= 1000
                job.cancel_timers()
                registry.prune()
                self.assertFalse(job.snapshot()["worker_finished"])
                self.assertIs(registry.get("stuck"), job)
                with self.assertRaises(JobCapacityError):
                    registry.create("replacement", 6)

    def test_completed_workers_are_evicted_oldest_first(self):
        registry = JobRegistry(max_jobs=2)
        old, _ = registry.create("old", 5)
        new, _ = registry.create("new", 6)
        for job in (old, new):
            job.succeed({})
            job.finish_worker()
        old.finished_at = new.finished_at - 1
        registry.create("replacement", 7)
        self.assertIsNone(registry.get("old"))
        self.assertIs(registry.get("new"), new)
        self.assertEqual(len(registry._jobs), 2)

    def test_ttl_only_removes_workers_that_really_exited(self):
        registry = JobRegistry(max_jobs=2, ttl_seconds=10)
        exited, _ = registry.create("exited", 5)
        stuck, _ = registry.create("stuck", 6)
        exited.timed_out()
        stuck.timed_out()
        exited.finish_worker()
        with patch("apriltag_test.time.time", return_value=exited.finished_at + 11):
            registry.prune()
        self.assertIsNone(registry.get("exited"))
        self.assertIs(registry.get("stuck"), stuck)

    def test_alive_thread_is_counted_even_after_finish_worker(self):
        registry = JobRegistry(max_jobs=1, ttl_seconds=0)
        job, _ = registry.create("alive", 5)
        released = threading.Event()
        marked = threading.Event()

        def worker():
            job.timed_out()
            job.finish_worker()
            marked.set()
            released.wait(3)

        thread = threading.Thread(target=worker)
        job.attach_worker(thread)
        thread.start()
        try:
            self.assertTrue(marked.wait(1))
            self.assertFalse(job.snapshot()["worker_finished"])
            registry.prune()
            with self.assertRaises(JobCapacityError):
                registry.create("replacement", 6)
        finally:
            released.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(job.snapshot()["worker_finished"])
        self.assertTrue(registry.create("replacement", 6)[1])

    def test_thread_start_failure_releases_reservation(self):
        registry = JobRegistry(max_jobs=1)
        job, _ = registry.create("start-failed", 5)
        job.attach_worker(threading.Thread(target=lambda: None))
        job.fail("start_failed", "start failed")
        job.finish_worker()
        self.assertTrue(registry.create("replacement", 6)[1])

    def test_worker_cannot_be_bound_twice(self):
        registry = JobRegistry()
        job, _ = registry.create("one", 5)
        worker = threading.Thread(target=lambda: None)
        job.bind_worker(worker)
        with self.assertRaises(RuntimeError):
            job.attach_worker(worker)

    def test_atomic_parallel_reservations_never_exceed_cap(self):
        registry = JobRegistry(max_jobs=3)
        barrier = threading.Barrier(20)
        results = []
        result_lock = threading.Lock()

        def reserve(index):
            barrier.wait(timeout=3)
            try:
                registry.create(str(index), 5)
                accepted = True
            except JobCapacityError:
                accepted = False
            with result_lock:
                results.append(accepted)

        threads = [threading.Thread(target=reserve, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 20)
        self.assertEqual(sum(results), 3)
        self.assertEqual(len(registry._jobs), 3)

    def test_parallel_duplicate_requests_reserve_once(self):
        registry = JobRegistry(max_jobs=1)
        barrier = threading.Barrier(12)
        results = []
        result_lock = threading.Lock()

        def reserve():
            barrier.wait(timeout=3)
            result = registry.create("same", 5)
            with result_lock:
                results.append(result)

        threads = [threading.Thread(target=reserve) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 12)
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({id(job) for job, _ in results}), 1)

    def test_invalid_limits_are_rejected(self):
        for limit in (0, -1, 1.5, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                JobRegistry(max_jobs=limit)


if __name__ == "__main__":
    unittest.main()
