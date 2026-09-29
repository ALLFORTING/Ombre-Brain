"""Archive-session-only durable requests. No import, merge, or repair execution."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3

import frontmatter

from bucket_manager import BucketManager
from bucket_write_lock import bucket_write_scope, initialize_bucket_write_lock, BucketWriteLockError
from maintenance_write_gate import guarded_mutation
from utils import generate_bucket_id
from confirmed_delete_admission import DurableDeleteAdmission, DeleteAdmissionError


TABLE = "ob_archive_session_operations"
MARKER_KIND = "archive_session"


class ArchiveSessionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def archive_now():
    return datetime.now().isoformat(timespec="seconds")


def validate_operation_id(operation_id):
    if operation_id is not None and (
        not isinstance(operation_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id) is None
    ):
        raise ArchiveSessionError("archive_operation_id_invalid")


def canonical_payload(summary, highlights="", mood="", valence=-1, arousal=-1,
                      letter="", sealed=False, topics=None):
    if not isinstance(summary, str) or not summary.strip():
        raise ArchiveSessionError("summary 不能为空。")
    if any(not isinstance(value, str) for value in (highlights, mood, letter)):
        raise ArchiveSessionError("archive_payload_invalid")
    for value in (valence, arousal):
        if (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
                or not (value == -1 or 0 <= value <= 1)):
            raise ArchiveSessionError("archive_payload_invalid")
    if not isinstance(sealed, bool):
        raise ArchiveSessionError("archive_payload_invalid")
    if topics is not None and (
        not isinstance(topics, list) or any(not isinstance(item, str) for item in topics)
    ):
        raise ArchiveSessionError("topics must be a list of strings.")
    normalized = list(dict.fromkeys(item.strip() for item in (topics or []) if item.strip()))
    return {"summary": summary.strip(), "highlights": highlights.strip(), "mood": mood.strip(),
            "valence": 0.0 if valence == 0 else float(valence),
            "arousal": 0.0 if arousal == 0 else float(arousal), "letter": letter.strip(),
            "sealed": sealed, "topics": normalized}


class ArchiveSessionOperations:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.db = self.root / "bucket_history.sqlite3"
        self.delete_admission = DurableDeleteAdmission(self.root)

    def _admit_source(self, plan, kind, *, allow_missing=False):
        try:
            return self.delete_admission.admit(plan['bucket_id'],root_binding=self.root,
                expected_source={'path':plan['relative_path'],'file_hash':plan['file_digest']},
                kind=kind,allow_missing=allow_missing)
        except DeleteAdmissionError as exc:
            raise ArchiveSessionError('archive_'+exc.code) from exc

    def checkpoint(self, boundary, operation):
        """Synchronous fault-injection seam; production does no work."""

    def _connect(self, *, readonly=False):
        conn = sqlite3.connect(self.db.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                               uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        try:
            op = dict(row)
            op["plan"] = json.loads(op["plan_json"])
            p = op["plan"]
            if (op["schema_version"] != 1
                    or op["status"] not in {"pending", "blocked", "completed"}
                    or _digest(op["canonical_payload_json"]) != op["payload_digest"]
                    or _digest(op["plan_json"]) != op["plan_digest"]
                    or _json(p["payload"]) != op["canonical_payload_json"]
                    or any(op[k] != p[k] for k in ("operation_id", "bucket_id", "session_name",
                                                  "session_date", "created_at", "result_text"))
                    or _digest(p["file_text"]) != p["file_digest"]
                    or p["relative_path"] != f"archive/session/{p['session_name']}_{p['bucket_id']}.md"
                    or (op["publication_digest"] is not None
                        and op["publication_digest"] != p["file_digest"])):
                raise ValueError()
            for name in ("embedding_resolution", "letter_resolution", "emotion_resolution",
                         "completed_receipt"):
                op[name] = json.loads(op[name + "_json"]) if op[name + "_json"] else None
            if op["status"] == "completed" and (
                op["completed_receipt"] != _receipt(op) or op["publication_digest"] is None
                or op["boot_event_id"] is None or op["embedding_resolution"] is None
                or op["letter_resolution"] is None or op["emotion_resolution"] is None
                or op["completed_at"] is None
            ):
                raise ValueError()
            return op
        except (ValueError, TypeError, KeyError):
            raise ArchiveSessionError("archive_journal_invalid") from None

    def lookup(self, operation_id, payload=None):
        """Cold, read-only lookup: absent storage/table never creates anything."""
        if not self.db.is_file():
            return None
        try:
            with closing(self._connect(readonly=True)) as conn:
                if not self._has_table(conn):
                    return None
                op = self._read(conn, operation_id)
        except sqlite3.Error:
            raise ArchiveSessionError("archive_journal_unavailable") from None
        if op is not None and payload is not None and op["canonical_payload_json"] != _json(payload):
            raise ArchiveSessionError("archive_operation_payload_conflict")
        return op

    @staticmethod
    def _has_table(conn):
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone() is not None

    def _read(self, conn, operation_id):
        return self._decode(conn.execute(
            "SELECT * FROM ob_archive_session_operations WHERE operation_id=?", (operation_id,)
        ).fetchone())

    def _inventory(self):
        identities, names = set(), []
        for kind in ("permanent", "dynamic", "archive", "feel"):
            directory = self.root / kind
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise ArchiveSessionError("archive_inventory_invalid")
            if not directory.exists():
                continue
            def unreadable(_error):
                raise ArchiveSessionError("archive_inventory_invalid")
            for parent, dirs, files in os.walk(directory, onerror=unreadable):
                if any((Path(parent) / name).is_symlink() for name in dirs):
                    raise ArchiveSessionError("archive_inventory_invalid")
                for name in files:
                    if not name.endswith(".md"):
                        continue
                    path = Path(parent) / name
                    try:
                        if path.is_symlink():
                            raise ValueError()
                        post = frontmatter.load(path)
                        identity = post.get("id")
                        if (not isinstance(identity, str) or not identity
                                or identity != identity.strip()
                                or not (path.stem == identity or path.stem.endswith("_" + identity))
                                or identity in identities):
                            raise ValueError()
                        identities.add(identity)
                        if isinstance(post.get("name"), str):
                            names.append(post["name"])
                    except Exception:
                        raise ArchiveSessionError("archive_inventory_invalid") from None
        return identities, names

    def _plan(self, payload, operation_id, conn, config):
        identities, names = self._inventory()
        if conn is not None and self._has_table(conn):
            for row in conn.execute("SELECT bucket_id,session_name FROM ob_archive_session_operations"):
                identities.add(row[0])
                names.append(row[1])
        created_at = archive_now()
        date = created_at.split("T", 1)[0]
        pattern = re.compile(r"session_" + re.escape(date) + r"_([0-9]+)")
        suffixes = [int(match[1]) for name in names if (match := pattern.fullmatch(name))]
        name = f"session_{date}_{max(suffixes, default=0) + 1:02d}"
        identity = generate_bucket_id()
        while identity in identities:
            identity = generate_bucket_id()
        parts = [f"# {name}", "", "## Summary", payload["summary"]]
        for label, field in (("Highlights", "highlights"), ("Mood", "mood")):
            if payload[field]:
                parts += ["", "## " + label, payload[field]]
        v, a = payload["valence"], payload["arousal"]
        post = BucketManager._build_bucket_post(
            identity, "\n".join(parts), tags=["session", "archive"], importance=5,
            domain=["session"], valence=v if v >= 0 else .5, arousal=a if a >= 0 else .3,
            bucket_type="archived", name=name, sealed=payload["sealed"], topics=payload["topics"],
            provenance_kind="summary", created=created_at, last_active=created_at, created_date=date,
        )
        digest = _digest(_json(payload))
        if operation_id is not None:
            BucketManager._append_operation_marker(post, operation_key="archive_session:" + identity,
                operation_kind=MARKER_KIND, payload_digest=digest)
        file_text = frontmatter.dumps(post)
        model = config.get("embedding", {}).get("model", "gemini-embedding-001")
        embedding_input = post.content[:2000]
        snapshot = None
        if not payload["sealed"] and v >= 0 and a >= 0:
            snapshot = {"timestamp": created_at, "valence": round(v, 3), "arousal": round(a, 3),
                        "source": "archive", "bucket_id": identity}
        return {"operation_id": operation_id, "payload": payload, "bucket_id": identity,
                "session_name": name, "session_date": date, "created_at": created_at,
                "file_text": file_text, "file_digest": _digest(file_text),
                "resolved_valence": post["valence"], "resolved_arousal": post["arousal"],
                "relative_path": f"archive/session/{name}_{identity}.md",
                "embedding_input": embedding_input, "embedding_model": model,
                "embedding_input_digest": _digest(_json({"text": embedding_input, "model": model})),
                "snapshot": snapshot,
                "result_text": f"已归档本次对话: {name} bucket_id:{identity}"}

    @guarded_mutation("archive_session_plan")
    def lookup_or_plan(self, operation_id, payload, config):
        os.makedirs(self.root, exist_ok=True)
        BucketManager._sync_directory(str(self.root.parent))
        initialize_bucket_write_lock(self.root)
        with bucket_write_scope(self.root), closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ob_archive_session_operations (
                    operation_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL, canonical_payload_json TEXT NOT NULL,
                    plan_json TEXT NOT NULL, plan_digest TEXT NOT NULL,
                    bucket_id TEXT NOT NULL UNIQUE, session_name TEXT NOT NULL UNIQUE,
                    session_date TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','blocked','completed')),
                    publication_digest TEXT, boot_event_id INTEGER UNIQUE,
                    embedding_resolution_json TEXT, letter_id INTEGER UNIQUE,
                    letter_resolution_json TEXT, emotion_resolution_json TEXT,
                    result_text TEXT NOT NULL, completed_receipt_json TEXT,
                    completed_at TEXT, last_error_code TEXT
                )
            """)
            op = self._read(conn, operation_id)
            if op is not None:
                if op["canonical_payload_json"] != _json(payload):
                    raise ArchiveSessionError("archive_operation_payload_conflict")
                return op
            plan = self._plan(payload, operation_id, conn, config)
            conn.execute("""
                INSERT INTO ob_archive_session_operations
                (operation_id,schema_version,payload_digest,canonical_payload_json,plan_json,
                 plan_digest,bucket_id,session_name,session_date,created_at,status,result_text)
                VALUES(?,1,?,?,?,?,?,?,?,?,'pending',?)
            """, (operation_id, _digest(_json(payload)), _json(payload), _json(plan),
                  _digest(_json(plan)), plan["bucket_id"], plan["session_name"],
                  plan["session_date"], plan["created_at"], plan["result_text"]))
            conn.commit()
            op = self._read(conn, operation_id)
        self.checkpoint("after_plan", op)
        return op

    def _verify_bucket(self, plan, *, missing_ok=False):
        path = self.root / plan["relative_path"]
        identities, _ = self._inventory()
        if not path.exists():
            if plan["bucket_id"] in identities or not missing_ok:
                raise ArchiveSessionError("archive_bucket_missing_or_moved")
            return False
        try:
            raw = path.read_bytes()
            post = frontmatter.loads(raw.decode("utf-8"))
            if (path.is_symlink() or _digest(raw.decode("utf-8")) != plan["file_digest"]
                    or post.get("id") != plan["bucket_id"] or post.get("type") != "archived"):
                raise ValueError()
            if plan["operation_id"] is not None and post.get("_ob_import_operations") != [{
                "operation_key": "archive_session:" + plan["bucket_id"],
                "operation_kind": MARKER_KIND, "payload_digest": _digest(_json(plan["payload"]))
            }]:
                raise ValueError()
        except Exception:
            raise ArchiveSessionError("archive_bucket_evidence_conflict") from None
        return True

    @staticmethod
    def _sealed_cleanup(root, identity):
        db = Path(root) / "embeddings.db"
        if db.exists():
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("DELETE FROM embeddings WHERE bucket_id=?", (identity,))
                if conn.execute("SELECT 1 FROM embeddings WHERE bucket_id=?", (identity,)).fetchone():
                    raise ArchiveSessionError("archive_sealed_embedding_conflict")

    def _publish(self, plan, *, previously_published=False):
        verified = self._verify_bucket(plan, missing_ok=not previously_published)
        self._admit_source(plan,'archive_publication',allow_missing=not previously_published)
        if verified:
            # The preceding executor may have stopped between replace and fsync.
            BucketManager._sync_directory(str(self.root / "archive"))
            BucketManager._sync_directory(str((self.root / plan["relative_path"]).parent))
            return
        if plan["payload"]["sealed"]:
            self._sealed_cleanup(self.root, plan["bucket_id"])
        directory = self.root / "archive" / "session"
        directory.mkdir(parents=True, exist_ok=True)
        BucketManager._sync_directory(str(self.root))
        BucketManager._sync_directory(str(directory.parent))
        BucketManager._write_bytes_atomic(str(self.root / plan["relative_path"]),
            plan["file_text"].encode("utf-8"),before_publish=lambda:
                self._admit_source(plan,'archive_publication',allow_missing=not previously_published))
        self._verify_bucket(plan)

    @guarded_mutation("archive_session_legacy_publish")
    def publish_legacy(self, payload, config):
        # Runtime has initialized the root mutex. No journal schema initialization.
        with bucket_write_scope(self.root), closing(self._connect(readonly=True)) as conn:
            plan = self._plan(payload, None, conn, config)
            self._publish(plan)
            return plan

    @guarded_mutation("archive_session_publish")
    def publish(self, operation_id):
        with bucket_write_scope(self.root), closing(self._connect()) as conn, conn:
            op = self._read(conn, operation_id)
            if op["status"] == "completed":
                return
            self._publish(op["plan"], previously_published=op["publication_digest"] is not None)
            self.checkpoint("after_publish", op)
            conn.execute("UPDATE ob_archive_session_operations SET publication_digest=? WHERE operation_id=?",
                         (op["plan"]["file_digest"], operation_id))

    @staticmethod
    def _verify_boot(conn, op):
        row = conn.execute("SELECT bucket_id,event_type,payload_json,occurred_at "
                           "FROM boot_delta_events WHERE id=?", (op["boot_event_id"],)).fetchone()
        if row is None or tuple(row) != (op["bucket_id"], "created", "{}", op["created_at"]):
            raise ArchiveSessionError("archive_boot_receipt_conflict")

    @guarded_mutation("archive_session_boot_event")
    def boot_event(self, operation_id):
        with bucket_write_scope(self.root), closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            op = self._read(conn, operation_id)
            if op["status"] == "completed":
                return
            self._verify_bucket(op["plan"])
            if op["boot_event_id"] is None:
                self._admit_source(op['plan'],'archive_boot_event')
                event_id = BucketManager._insert_boot_delta_event(
                    conn, op["bucket_id"], "created", "{}", op["created_at"])
                self.checkpoint("before_boot_commit", op)
                conn.execute("UPDATE ob_archive_session_operations SET boot_event_id=? WHERE operation_id=?",
                             (event_id, operation_id))
                conn.commit()
                self.checkpoint("after_boot_commit", op)
            self._verify_boot(conn, self._read(conn, operation_id))

    @staticmethod
    def _verify_letter(conn, op):
        expected = op["plan"]["payload"]["letter"]
        if not expected:
            if op["letter_id"] is not None or op["letter_resolution"] != {"outcome": "not_requested"}:
                raise ArchiveSessionError("archive_letter_receipt_conflict")
            return
        row = conn.execute("SELECT content,created_at,session_id,sealed FROM letters WHERE id=?",
                           (op["letter_id"],)).fetchone()
        if (row is None or tuple(row) != (expected, op["created_at"], op["bucket_id"],
                                         int(op["plan"]["payload"]["sealed"]))
                or op["letter_resolution"] != {"outcome": "written"}):
            raise ArchiveSessionError("archive_letter_receipt_conflict")

    @guarded_mutation("archive_session_letter")
    def letter(self, operation_id):
        with bucket_write_scope(self.root), closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            op = self._read(conn, operation_id)
            if op["status"] == "completed":
                return
            self._verify_bucket(op["plan"])
            if op["letter_resolution"] is None:
                if op["letter_id"] is not None:
                    raise ArchiveSessionError("archive_letter_receipt_conflict")
                letter_id = None
                content = op["plan"]["payload"]["letter"]
                if content:
                    self._admit_source(op['plan'],'archive_letter')
                    letter_id = BucketManager._insert_letter(
                        conn, content, op["bucket_id"], op["plan"]["payload"]["sealed"], op["created_at"])
                resolution = {"outcome": "written" if content else "not_requested"}
                self.checkpoint("before_letter_commit", op)
                conn.execute("UPDATE ob_archive_session_operations SET letter_id=?,letter_resolution_json=? "
                             "WHERE operation_id=?", (letter_id, _json(resolution), operation_id))
                conn.commit()
                self.checkpoint("after_letter_commit", op)
            self._verify_letter(conn, self._read(conn, operation_id))

    @guarded_mutation("archive_session_embedding")
    def embedding(self, operation_id, engine, *, vector=None, provider_finished=False):
        with bucket_write_scope(self.root), closing(self._connect()) as conn, conn:
            op = self._read(conn, operation_id)
            if op["status"] == "completed":
                return False
            p = op["plan"]
            self._admit_source(p,'archive_embedding')
            self._verify_bucket(p)
            if op["embedding_resolution"] is not None:
                self._verify_embedding(op, engine)
                return False
            resolution = None
            if p["payload"]["sealed"]:
                self._sealed_cleanup(self.root, p["bucket_id"])
                resolution = {"outcome": "sealed"}
            elif engine is None:
                resolution = {"outcome": "disabled"}
            else:
                try:
                    resolution = engine.archive_embedding_evidence(
                        p["bucket_id"], p["embedding_input_digest"], p["embedding_model"])
                    if resolution is None and provider_finished:
                        if engine._valid_archive_vector(vector):
                            engine.store_archive_embedding(p["bucket_id"], vector,
                                p["embedding_input_digest"], p["embedding_model"], p["created_at"])
                            self.checkpoint("after_vector_commit", op)
                            resolution = engine.archive_embedding_evidence(
                                p["bucket_id"], p["embedding_input_digest"], p["embedding_model"])
                        else:
                            resolution = {"outcome": "best_effort_failed"}
                    elif resolution is None and not engine.enabled:
                        resolution = {"outcome": "disabled"}
                except ValueError:
                    raise ArchiveSessionError("archive_embedding_receipt_conflict") from None
                except (sqlite3.Error, OSError):
                    resolution = {"outcome": "best_effort_failed"}
            if resolution is None:
                return True
            conn.execute("UPDATE ob_archive_session_operations SET embedding_resolution_json=? WHERE operation_id=?",
                         (_json(resolution), operation_id))
            return False

    def _verify_embedding(self, op, engine):
        evidence = op["embedding_resolution"]
        if evidence is None or evidence.get("outcome") not in {
            "stored", "sealed", "disabled", "best_effort_failed"
        }:
            raise ArchiveSessionError("archive_embedding_receipt_conflict")
        if op["plan"]["payload"]["sealed"] != (evidence["outcome"] == "sealed"):
            raise ArchiveSessionError("archive_embedding_receipt_conflict")
        if evidence["outcome"] == "sealed":
            db = self.root / "embeddings.db"
            if db.exists():
                with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
                    if conn.execute("SELECT 1 FROM embeddings WHERE bucket_id=?", (op["bucket_id"],)).fetchone():
                        raise ArchiveSessionError("archive_sealed_embedding_conflict")
        if evidence["outcome"] == "stored":
            p = op["plan"]
            try:
                actual = None if engine is None else engine.archive_embedding_evidence(
                    p["bucket_id"], p["embedding_input_digest"], p["embedding_model"])
            except ValueError:
                raise ArchiveSessionError("archive_embedding_receipt_conflict") from None
            if actual != evidence:
                raise ArchiveSessionError("archive_embedding_receipt_conflict")

    @guarded_mutation("archive_session_emotion")
    def emotion(self, operation_id, snapshot_writer):
        with bucket_write_scope(self.root), closing(self._connect()) as conn, conn:
            op = self._read(conn, operation_id)
            if op["status"] == "completed":
                return
            self._verify_bucket(op["plan"])
            entry = op["plan"]["snapshot"]
            expected = {"outcome": "written", "entry_digest": _digest(_json(entry))} if entry else {
                "outcome": "not_requested"}
            if op["emotion_resolution"] is not None and op["emotion_resolution"] != expected:
                raise ArchiveSessionError("archive_emotion_receipt_conflict")
            if entry is not None:
                self._admit_source(op['plan'],'archive_emotion')
                snapshot_writer(entry, verify_only=op["emotion_resolution"] is not None)
                self.checkpoint("after_emotion_replace", op)
            conn.execute("UPDATE ob_archive_session_operations SET emotion_resolution_json=? WHERE operation_id=?",
                         (_json(expected), operation_id))

    @guarded_mutation("archive_session_complete")
    def complete(self, operation_id, engine, snapshot_writer):
        with bucket_write_scope(self.root), closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            op = self._read(conn, operation_id)
            if op["status"] == "completed":
                return op["result_text"]
            self._verify_bucket(op["plan"])
            self._verify_boot(conn, op)
            self._verify_letter(conn, op)
            self._verify_embedding(op, engine)
            entry = op["plan"]["snapshot"]
            expected = {"outcome": "written", "entry_digest": _digest(_json(entry))} if entry else {
                "outcome": "not_requested"}
            if op["emotion_resolution"] != expected:
                raise ArchiveSessionError("archive_emotion_receipt_conflict")
            if entry is not None:
                snapshot_writer(entry, verify_only=True)
            conn.execute("UPDATE ob_archive_session_operations SET status='completed',"
                         "completed_receipt_json=?,completed_at=?,last_error_code=NULL WHERE operation_id=?",
                         (_json(_receipt(op)), archive_now(), operation_id))
            conn.commit()
            self.checkpoint("after_completed_commit", op)
            return op["result_text"]

    @guarded_mutation("archive_session_blocked")
    def blocked(self, operation_id, code):
        if re.fullmatch(r"archive_[a-z0-9_]+", code) is None:
            code = "archive_session_storage_failed"
        with bucket_write_scope(self.root), closing(self._connect()) as conn, conn:
            conn.execute("UPDATE ob_archive_session_operations SET status='blocked',last_error_code=? "
                         "WHERE operation_id=? AND status!='completed'", (code, operation_id))


def _receipt(op):
    return {key: op[key] for key in ("bucket_id", "session_name", "result_text", "plan_digest",
            "boot_event_id", "embedding_resolution", "letter_id", "letter_resolution",
            "emotion_resolution")}


async def short_step(function, *args, **kwargs):
    # Timed-out acquisition has already released all storage resources.
    while True:
        try:
            return function(*args, **kwargs)
        except BucketWriteLockError as exc:
            cause = exc.__cause__
            if ((cause is not None and getattr(cause, "sqlite_errorcode", None) in {
                    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
                    or (cause is None and str(exc) in {
                        "bucket writer mutex timeout", "bucket writer mutex initialization timeout"})):
                await asyncio.sleep(.05)
            else:
                raise ArchiveSessionError("archive_storage_lock_unavailable") from None


async def execute_archive_operation(store, operation, engine, snapshot_writer):
    identity = operation["operation_id"]
    if operation["status"] == "completed":
        return operation["result_text"]
    await short_step(store.publish, identity)
    await short_step(store.boot_event, identity)
    if await short_step(store.embedding, identity, engine):
        plan = operation["plan"]
        try:
            vector = await engine._generate_embedding(plan["embedding_input"], model=plan["embedding_model"])
        except Exception:
            vector = []  # Provider ordinary errors remain best-effort; cancellation propagates.
        await short_step(store.embedding, identity, engine, vector=vector, provider_finished=True)
    await short_step(store.letter, identity)
    await short_step(store.emotion, identity, snapshot_writer)
    return await short_step(store.complete, identity, engine, snapshot_writer)
