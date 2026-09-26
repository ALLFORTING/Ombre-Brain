"""Short, reentrant storage-root mutex for bucket file read/modify/publish.

The SQLite lock database contains no business state. Scopes never await and
never modify its pages; rollback releases BEGIN IMMEDIATE on every exit.
"""
from contextlib import contextmanager
from pathlib import Path
import asyncio
import os
import sqlite3
import threading
from maintenance_write_gate import guarded_mutation


class BucketWriteLockError(RuntimeError):
    pass


_registry_lock = threading.Lock()
_registry = {}


def _state(root):
    key = (os.getpid(), os.path.normcase(str(Path(root).resolve())))
    with _registry_lock:
        return _registry.setdefault(key, (threading.RLock(), threading.local()))


def _finish_connection(connection):
    """Always close even when rollback fails; a failed close gets one cleanup retry."""
    try:
        if connection.in_transaction:
            connection.rollback()
    finally:
        try:
            connection.close()
        except BaseException:
            # Preserve the failure while making a second attempt to release the
            # transaction if the first close failed before closing the handle.
            try:
                connection.close()
            finally:
                raise


@guarded_mutation("bucket_write_lock_initialize")
def initialize_bucket_write_lock(root):
    """Initialize only during storage setup, never during a preview/read."""
    path = Path(root) / ".bucket-write.lock"
    mutex, local = _state(root)
    if not mutex.acquire(timeout=5):
        raise BucketWriteLockError("bucket writer mutex initialization timeout")
    connection = None
    try:
        if getattr(local, "depth", 0):
            if local.owner != _execution():
                raise BucketWriteLockError("bucket writer mutex must not span await")
            return
        connection = sqlite3.connect(str(path), timeout=5)
        connection.execute("CREATE TABLE IF NOT EXISTS writer_mutex (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        try:
            if connection is not None:
                _finish_connection(connection)
        finally:
            mutex.release()


def _execution():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return (threading.get_ident(), id(task) if task else None)


@contextmanager
def bucket_write_scope(root, *, timeout=5.0):
    """Fail closed on timeout/error; never fall back to an unlocked writer."""
    mutex, local = _state(root)
    if not mutex.acquire(timeout=timeout):
        raise BucketWriteLockError("bucket writer mutex timeout")
    connection = None
    nested = False
    try:
        if getattr(local, "depth", 0):
            if local.owner != _execution():
                raise BucketWriteLockError("bucket writer mutex must not span await")
            local.depth += 1
            nested = True
        else:
            path = (Path(root) / ".bucket-write.lock").resolve()
            try:
                connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                             timeout=timeout, isolation_level=None)
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise BucketWriteLockError("bucket writer mutex unavailable or timeout") from exc
            local.depth = 1
            local.owner = _execution()
        yield
    finally:
        try:
            if nested:
                local.depth -= 1
            elif connection is not None:
                try:
                    _finish_connection(connection)
                finally:
                    local.depth = 0
                    local.owner = None
        finally:
            mutex.release()
