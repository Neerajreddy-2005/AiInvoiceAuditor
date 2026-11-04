"""Invoice monitor agent (no LLM).

Polls the mounted incoming directory, checksums each file (SHA-256 of
the content, so renamed copies are still recognised), skips anything
already in the audit trail, records every detected file (including
unsupported and failing ones), and routes new files into the workflow.

Polling rather than filesystem events: inotify events do not cross a
Docker Desktop bind mount from a Windows/macOS host reliably.

The checksum claim is an INSERT ... ON CONFLICT DO NOTHING, so even two
agent replicas would never process the same invoice twice.
"""
import json
import logging
import os
import stat
import threading
import time
from pathlib import Path

from . import repository as repo
from . import workflow
from .extraction import file_sha256
from .rag import indexing
from .settings import agent_config, settings

log = logging.getLogger(__name__)
MAX_SIDECAR_BYTES = 64 * 1024


class InvoiceMonitor:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._checksums: dict[tuple[str, int, float], str] = {}  # (path, size, mtime) -> sha256
        self._last_seen: dict[str, tuple[int, float]] = {}  # path -> (size, mtime) at the previous scan
        self._file_errors: list[str] = []
        self._index_failures: dict[str, int] = {}
        self._next_index_sweep = 0.0
        self.last_scan_at: float | None = None
        self.last_error: str | None = None

    # -------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="invoice-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        interval = agent_config()["monitor"]["poll_interval_seconds"]
        log.info("Monitoring %s every %ss", settings().incoming_dir, interval)
        while not self._stop.is_set():
            errors = []
            for step in (self.scan_once, self.retry_failed, self.index_sweep):
                try:
                    step()
                except Exception as exc:  # keep the monitor alive; surface the error in /health
                    errors.append(str(exc))
                    log.exception("Monitor step %s failed", step.__name__)
            self.last_error = "; ".join(self._file_errors + errors) or None
            self.last_scan_at = time.time()
            self._wake.wait(interval)
            self._wake.clear()

    # -------------------------------------------------- work
    def _checksum(self, path: Path) -> str:
        info = path.stat()
        key = (str(path), info.st_size, info.st_mtime)
        if key not in self._checksums:
            self._checksums[key] = file_sha256(path)
        return self._checksums[key]

    @staticmethod
    def _sidecar(path: Path) -> dict | None:
        meta = path.with_name(path.stem + ".meta.json")
        try:
            # Checked on the open file, so it can't be swapped afterwards. A symlink fails to
            # open, and a pipe doesn't block.
            fd = os.open(meta, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("Ignoring unreadable sidecar %s: %s", meta.name, exc)
            return None
        try:
            with os.fdopen(fd, "rb") as fh:
                info = os.fstat(fh.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("sidecar is not a regular file")
                if info.st_size > MAX_SIDECAR_BYTES:
                    raise ValueError("sidecar is too large")
                data = json.loads(fh.read(MAX_SIDECAR_BYTES + 1).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("sidecar is not a JSON object")
            return data
        except (OSError, ValueError, RecursionError) as exc:
            log.warning("Ignoring unreadable sidecar %s: %s", meta.name, exc)
            return None

    def scan_once(self) -> int:
        cfg = agent_config()["monitor"]
        incoming = settings().incoming_dir
        if not incoming.is_dir():
            raise RuntimeError(f"Incoming directory {incoming} is not mounted")
        processed = 0
        self._file_errors = []
        paths = sorted(incoming.iterdir())
        for path in paths:
            if processed >= cfg["max_new_files_per_scan"]:
                break
            try:
                processed += self._scan_file(path, cfg)
            except Exception as exc:  # one bad file must not stop the others
                self._file_errors.append(f"{path.name!r}: {exc}")
                log.warning("Skipping file %r: %s", path.name, exc)
        present = {str(p) for p in paths}
        self._last_seen = {k: v for k, v in self._last_seen.items() if k in present}
        self._checksums = {k: v for k, v in self._checksums.items() if k[0] in present}
        return processed

    def _scan_file(self, path: Path, cfg: dict) -> int:
        # lstat, not is_file: a symlink could point at a file outside the incoming folder.
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.name.startswith(".") or path.name.endswith(".meta.json")
                or path.suffix.lower() in (".tmp", ".part", ".crdownload")):
            return 0
        unchanged = self._last_seen.get(str(path)) == (info.st_size, info.st_mtime)
        self._last_seen[str(path)] = (info.st_size, info.st_mtime)
        # Copies can keep the source's old mtime, so age alone can't prove a copy has finished.
        if not unchanged or time.time() - info.st_mtime < cfg["min_file_age_seconds"]:
            return 0  # still being copied
        limit_mb = agent_config()["extraction"]["limits"]["max_file_mb"]
        if info.st_size > limit_mb * 1024 * 1024:  # before hashing, which would read all of it
            raise ValueError(f"File is over the {limit_mb} MB limit, so it was not read")
        checksum = self._checksum(path)
        if repo.get_by_checksum(checksum):
            return 0  # already audited (possibly under another file name)

        file_type = cfg["supported_extensions"].get(path.suffix.lower())
        sidecar = self._sidecar(path)  # read once, so what is stored is what is used
        invoice_id = repo.claim_file(file_name=path.name, file_path=str(path), checksum=checksum,
                                     file_type=file_type, size=info.st_size, email_metadata=sidecar)
        if invoice_id is None:
            return 0
        if file_type is None:
            repo.mark_unsupported(invoice_id, f"Unsupported file type '{path.suffix}'")
            log.warning("Recorded unsupported file %s", path.name)
            return 0
        log.info("New invoice %s (%s, sha256 %s…) -> %s", path.name, file_type, checksum[:12], invoice_id)
        try:
            workflow.run_new(invoice_id=invoice_id, file_name=path.name, file_path=str(path),
                             file_type=file_type, email_metadata=sidecar)
            return 1
        except workflow.WorkflowError as exc:
            log.error("%s: %s", path.name, exc)
            return 0

    def retry_failed(self) -> None:
        cfg = agent_config()["monitor"]
        for row in repo.failed_for_retry(cfg["max_attempts"], cfg["retry_backoff_seconds"]):
            if not repo.claim_retry(row["invoice_id"]):
                continue  # already retried from the dashboard
            log.info("Resuming %s from checkpoint (attempt %s)", row["file_name"], row["attempts"] + 1)
            try:
                workflow.resume(row["invoice_id"], row["thread_id"])
            except workflow.WorkflowError as exc:
                log.error("%s: %s", row["file_name"], exc)

    def index_sweep(self) -> None:
        """Index completed invoices whose indexing failed earlier (e.g. Gemini throttling)."""
        cfg = agent_config()["monitor"]
        if time.monotonic() < self._next_index_sweep:
            return
        given_up = {i for i, n in self._index_failures.items() if n >= cfg["max_index_failures"]}
        for invoice_id in repo.unindexed_invoice_ids(limit=5 + len(given_up)):
            if invoice_id in given_up:
                continue
            try:
                indexing.index_invoice(invoice_id)
                self._index_failures.pop(invoice_id, None)
            except Exception as exc:
                self._index_failures[invoice_id] = self._index_failures.get(invoice_id, 0) + 1
                self._next_index_sweep = time.monotonic() + cfg["index_retry_delay_seconds"]
                log.warning("Index sweep for %s failed: %s", invoice_id, exc)
                break


monitor = InvoiceMonitor()
