import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

import requests
from inotify_simple import INotify, flags

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
WATCH_DIRS = [
    d.strip()
    for d in os.getenv("WATCH_DIRS", "/watch/etc").split(",")
    if d.strip()
]
# Strip this prefix from container paths to restore original host paths.
# e.g. /watch/etc/hostname  ->  /etc/hostname
WATCH_PREFIX_STRIP = os.getenv("WATCH_PREFIX_STRIP", "/watch").rstrip("/")

try:
    BATCH_WINDOW_SECONDS = max(1, int(os.getenv("BATCH_WINDOW_SECONDS", "60")))
except ValueError:
    BATCH_WINDOW_SECONDS = 60


def normalize_path(path: str) -> str:
    """Restore the original host path by stripping the watch prefix."""
    if WATCH_PREFIX_STRIP and path.startswith(WATCH_PREFIX_STRIP):
        return path[len(WATCH_PREFIX_STRIP):]
    return path


def wait_for_backend() -> None:
    """Block until the backend API is reachable (up to ~60 s)."""
    url = f"{BACKEND_URL}/api/files"
    for attempt in range(30):
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                logger.info("Backend is ready")
                return
        except Exception:
            pass
        logger.info("Waiting for backend... (%d/30)", attempt + 1)
        time.sleep(2)
    logger.warning("Backend not ready after 60 s, proceeding anyway")


def read_utf8_file(path: str):
    """Return UTF-8 text content, or None for binary/unreadable files."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        logger.debug("Skipping binary file: %s", path)
        return None
    except (IsADirectoryError, FileNotFoundError):
        return None
    except PermissionError:
        logger.warning("Permission denied: %s", path)
        return None
    except OSError as exc:
        logger.warning("Cannot read %s: %s", path, exc)
        return None


@dataclass
class BatchBuffer:
    touched_paths: Set[str] = field(default_factory=set)
    deleted_paths: Set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def mark_modified(self, path: str) -> None:
        with self.lock:
            self.touched_paths.add(path)
            self.deleted_paths.discard(path)

    def mark_deleted(self, path: str) -> None:
        with self.lock:
            self.touched_paths.add(path)
            self.deleted_paths.add(path)

    def pop_snapshot(self) -> Tuple[Set[str], Set[str]]:
        with self.lock:
            touched = set(self.touched_paths)
            deleted = set(self.deleted_paths)
            self.touched_paths.clear()
            self.deleted_paths.clear()
        return touched, deleted


class InotifyWatcher:
    WATCH_MASK = (
        flags.CLOSE_WRITE
        | flags.CREATE
        | flags.MOVED_TO
        | flags.DELETE
        | flags.MOVED_FROM
        | flags.DELETE_SELF
        | flags.MOVE_SELF
        | flags.IGNORED
    )

    def __init__(self, watch_dirs: List[str], buffer: BatchBuffer):
        self.watch_dirs = watch_dirs
        self.buffer = buffer
        self.inotify = INotify()
        self.wd_to_path: Dict[int, str] = {}
        self.path_to_wd: Dict[str, int] = {}

    def _add_watch(self, directory: str) -> None:
        directory = os.path.normpath(directory)
        if directory in self.path_to_wd:
            return
        try:
            wd = self.inotify.add_watch(directory, self.WATCH_MASK)
            self.path_to_wd[directory] = wd
            self.wd_to_path[wd] = directory
            logger.info("Watching directory: %s", directory)
        except FileNotFoundError:
            logger.warning("Directory disappeared before watch: %s", directory)
        except PermissionError:
            logger.warning("Permission denied while watching: %s", directory)
        except OSError as exc:
            logger.warning("Failed to watch %s: %s", directory, exc)

    def _remove_watch(self, directory: str) -> None:
        wd = self.path_to_wd.pop(directory, None)
        if wd is None:
            return
        self.wd_to_path.pop(wd, None)
        try:
            self.inotify.rm_watch(wd)
        except OSError:
            pass

    def _remove_watches_under(self, root: str) -> None:
        root = os.path.normpath(root)
        to_remove = [
            p for p in self.path_to_wd.keys() if p == root or p.startswith(root + os.sep)
        ]
        for path in to_remove:
            self._remove_watch(path)

    def add_watch_recursive(self, root: str) -> None:
        root = os.path.normpath(root)
        if not os.path.isdir(root):
            logger.warning("Directory not found, skipping: %s", root)
            return
        for dirpath, _dirnames, _filenames in os.walk(root):
            self._add_watch(dirpath)

    def start(self) -> int:
        scheduled = 0
        for watch_dir in self.watch_dirs:
            before = len(self.path_to_wd)
            self.add_watch_recursive(watch_dir)
            if len(self.path_to_wd) > before:
                scheduled += 1
                logger.info(
                    "Watching root: %s (host path: %s)",
                    watch_dir,
                    normalize_path(os.path.normpath(watch_dir)),
                )
        return scheduled

    def process_events(self) -> None:
        for event in self.inotify.read(timeout=0):
            base_dir = self.wd_to_path.get(event.wd)
            if not base_dir:
                continue

            mask_flags = flags.from_mask(event.mask)
            if flags.IGNORED in mask_flags:
                self.wd_to_path.pop(event.wd, None)
                continue

            path = os.path.join(base_dir, event.name) if event.name else base_dir
            is_dir = flags.ISDIR in mask_flags

            if flags.CREATE in mask_flags:
                if is_dir:
                    self.add_watch_recursive(path)
                else:
                    self.buffer.mark_modified(path)

            if flags.MOVED_TO in mask_flags:
                if is_dir:
                    self.add_watch_recursive(path)
                else:
                    self.buffer.mark_modified(path)

            if flags.CLOSE_WRITE in mask_flags and not is_dir:
                self.buffer.mark_modified(path)

            if flags.DELETE in mask_flags:
                if is_dir:
                    self._remove_watches_under(path)
                else:
                    self.buffer.mark_deleted(path)

            if flags.MOVED_FROM in mask_flags:
                if is_dir:
                    self._remove_watches_under(path)
                else:
                    self.buffer.mark_deleted(path)

            if flags.DELETE_SELF in mask_flags or flags.MOVE_SELF in mask_flags:
                self._remove_watches_under(base_dir)


def post_single_event(event_payload: Dict) -> bool:
    try:
        resp = requests.post(
            f"{BACKEND_URL}/api/ingest",
            json=event_payload,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.error("POST /api/ingest failed for %s: %s", event_payload.get("path"), exc)
        return False


def flush_batch(buffer: BatchBuffer) -> None:
    touched, deleted = buffer.pop_snapshot()
    if not touched:
        return

    events = []
    for path in sorted(touched):
        host_path = normalize_path(path)
        if path in deleted:
            events.append({"path": host_path, "event_type": "deleted", "content": None})
            continue

        content = read_utf8_file(path)
        if content is None:
            continue
        events.append({"path": host_path, "event_type": "modified", "content": content})

    if not events:
        return

    batch_payload = {"events": events}
    try:
        resp = requests.post(
            f"{BACKEND_URL}/api/ingest/batch",
            json=batch_payload,
            timeout=20,
        )
        resp.raise_for_status()
        logger.info("Flushed batch with %d event(s)", len(events))
        return
    except Exception as exc:
        logger.warning("POST /api/ingest/batch failed, falling back: %s", exc)

    ok_count = 0
    for event_payload in events:
        if post_single_event(event_payload):
            ok_count += 1
    logger.info("Fallback sent %d/%d event(s)", ok_count, len(events))


def main() -> None:
    wait_for_backend()

    buffer = BatchBuffer()
    watcher = InotifyWatcher(WATCH_DIRS, buffer)

    roots_watched = watcher.start()
    if roots_watched == 0:
        logger.error("No valid directories to watch. Exiting.")
        return

    logger.info(
        "Watcher started with inotify; batch_window=%ss across %d root director%s",
        BATCH_WINDOW_SECONDS,
        roots_watched,
        "y" if roots_watched == 1 else "ies",
    )

    stop_event = threading.Event()

    def _handle_shutdown(signum, _frame):
        logger.info("Received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    next_flush = time.monotonic() + BATCH_WINDOW_SECONDS
    try:
        while not stop_event.is_set():
            watcher.process_events()
            now = time.monotonic()
            if now >= next_flush:
                flush_batch(buffer)
                next_flush = now + BATCH_WINDOW_SECONDS
            time.sleep(0.2)
    finally:
        flush_batch(buffer)
        logger.info("Watcher stopped")


if __name__ == "__main__":
    main()
