"""Per-provider sequential job queue using asyncio.

Jobs sharing the same Chrome profile are serialized together to prevent
page navigation conflicts (e.g., gemini_image navigating away while
gemini_video is waiting for a result on the same Chrome page).

Supports queue_timeout to prevent clients from blocking indefinitely
when a long-running job is ahead in the queue.
"""
import asyncio
import logging
from collections import defaultdict

from providers.base import JobResult

logger = logging.getLogger("ai-hub.queue")


class QueueBusyError(Exception):
    """Raised when a job cannot start within the queue timeout."""
    pass


class JobQueue:
    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._workers: dict[str, asyncio.Task] = {}
        self._busy: dict[str, bool] = defaultdict(lambda: False)
        # Track which provider is currently executing per queue key
        self._running: dict[str, str] = {}

    def is_busy(self, provider: str) -> bool:
        return self._busy.get(provider, False)

    def get_queue_status(self, queue_key: str) -> dict:
        """Get queue status for a given key."""
        q = self._queues.get(queue_key)
        return {
            "queue_key": queue_key,
            "pending": q.qsize() if q else 0,
            "running": self._running.get(queue_key, None),
        }

    async def submit(self, provider: str, job_fn, timeout: int = 120,
                     queue_key: str = None, queue_timeout: int = None):
        """Submit a job and wait for result.

        Jobs are executed sequentially per queue_key. If queue_key is not
        provided, falls back to provider name.

        Args:
            provider: Provider name (for logging and busy tracking)
            job_fn: Async callable to execute
            timeout: Max execution time in seconds
            queue_key: Queue grouping key (use chrome_profile for browser providers)
            queue_timeout: Max seconds to wait in queue before starting.
                          If exceeded, raises QueueBusyError.
                          If None, waits indefinitely.
        """
        key = queue_key or provider
        future = asyncio.get_event_loop().create_future()
        # started_event is set when the worker picks up this job
        started_event = asyncio.Event()
        await self._queues[key].put((job_fn, future, timeout, provider, started_event))

        # Ensure worker exists for this key
        if key not in self._workers or self._workers[key].done():
            self._workers[key] = asyncio.create_task(
                self._worker(key)
            )

        # Wait for the job to START (not finish) within queue_timeout
        if queue_timeout is not None:
            try:
                await asyncio.wait_for(started_event.wait(), timeout=queue_timeout)
            except asyncio.TimeoutError:
                # Job didn't start in time — cancel it
                if not future.done():
                    future.set_exception(
                        QueueBusyError(
                            f"Queue '{key}' busy (running: {self._running.get(key, '?')}). "
                            f"Could not start within {queue_timeout}s."
                        )
                    )
                raise QueueBusyError(
                    f"Queue '{key}' busy, waited {queue_timeout}s. "
                    f"Currently running: {self._running.get(key, '?')}"
                )

        return await future

    async def _worker(self, key: str):
        queue = self._queues[key]
        while True:
            try:
                job_fn, future, timeout, provider, started_event = await asyncio.wait_for(
                    queue.get(), timeout=60
                )
            except asyncio.TimeoutError:
                # No jobs for 60s, worker exits
                logger.debug(f"Worker {key} idle, exiting")
                self._running.pop(key, None)
                return

            # Check if this job was already cancelled (queue_timeout expired)
            if future.done():
                logger.info(f"Job for {provider} (queue={key}) was cancelled before starting, skipping")
                queue.task_done()
                continue

            self._busy[provider] = True
            self._running[key] = provider
            started_event.set()  # Signal that the job has started

            try:
                result = await asyncio.wait_for(job_fn(), timeout=timeout)
                if not future.done():
                    future.set_result(result)
            except asyncio.TimeoutError:
                if not future.done():
                    future.set_exception(
                        asyncio.TimeoutError(f"{provider} execution timed out after {timeout}s")
                    )
                logger.error(f"Job timeout for {provider} (queue={key}): {timeout}s")
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
                logger.error(f"Job error for {provider} (queue={key}): {type(e).__name__}: {e}")
            finally:
                self._busy[provider] = False
                self._running.pop(key, None)
                queue.task_done()
