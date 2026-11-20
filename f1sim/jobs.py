"""Background-thread job manager for long runs (race Monte Carlo, optimiser, calibration).

    job_id = JOBS.submit("race", lambda progress: run(..., progress=progress))
    JOBS.get(job_id) -> {id, kind, status, progress, message, result, error}
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid

MAX_JOBS = 40           # finished jobs kept in memory
MAX_AGE_S = 3600.0      # finished jobs older than this are dropped


class JobManager:
    def __init__(self, max_jobs: int = MAX_JOBS, max_age_s: float = MAX_AGE_S):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.max_jobs = max_jobs
        self.max_age_s = max_age_s

    def submit(self, kind: str, fn, on_done=None) -> str:
        """Run fn(progress) in a daemon thread; progress(fraction, message=None) updates the job.
        on_done(job_dict) is called after the job finishes (either way)."""
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "kind": kind, "status": "running", "progress": 0.0, "message": "queued",
               "result": None, "error": None, "started": time.time(), "finished": None}
        with self._lock:
            self._prune()
            self._jobs[job_id] = job

        def progress(fraction, message=None):
            with self._lock:
                try:
                    job["progress"] = min(1.0, max(0.0, float(fraction)))
                except (TypeError, ValueError):
                    pass
                if message is not None:
                    job["message"] = str(message)

        def run():
            try:
                result = fn(progress)
                with self._lock:
                    job.update(status="done", progress=1.0, message="done", result=result)
            except Exception as e:  # noqa: BLE001 - a failed job must never take the server down
                traceback.print_exc()
                with self._lock:
                    job.update(status="error", message="failed", error=f"{type(e).__name__}: {e}")
            finally:
                with self._lock:
                    job["finished"] = time.time()
                if on_done is not None:
                    try:
                        on_done(job)
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()

        threading.Thread(target=run, name=f"job-{kind}-{job_id}", daemon=True).start()
        return job_id

    def get(self, job_id: str) -> dict | None:
        """Public view of a job (the contract of GET /api/jobs/<id>), or None if unknown/pruned."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {k: job[k] for k in ("id", "kind", "status", "progress", "message", "result", "error")}

    def running(self, kind: str | None = None) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values()
                       if j["status"] == "running" and (kind is None or j["kind"] == kind))

    def _prune(self):
        now = time.time()
        done = sorted((j for j in self._jobs.values() if j["status"] != "running"), key=lambda j: j["finished"] or 0)
        drop = [j for j in done if now - (j["finished"] or now) > self.max_age_s]
        keep = [j for j in done if j not in drop]
        excess = len(keep) - (self.max_jobs - 1)
        if excess > 0:
            drop += keep[:excess]
        for j in drop:
            self._jobs.pop(j["id"], None)


JOBS = JobManager()
