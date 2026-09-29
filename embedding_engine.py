# ============================================================
# Module: Embedding Engine (embedding_engine.py)
# ??:?????
#
# Generates embeddings via Gemini API (OpenAI-compatible),
# stores them in SQLite, and provides cosine similarity search.
# ?? Gemini API(OpenAI ??)?? embedding,
# ??? SQLite ?,??????????
#
# Depended on by: server.py, bucket_manager.py
# ????:server.py, bucket_manager.py
# ============================================================

import os
import json
import math
from bucket_write_lock import bucket_write_scope, initialize_bucket_write_lock
import sqlite3
import logging
import re
import hashlib
from contextlib import closing
from urllib.parse import urlsplit

from openai import AsyncOpenAI

from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_mutation,
)

logger = logging.getLogger("ombre_brain.embedding")

_ERROR_TYPE_LIMIT = 80
_REQUEST_URL_LIMIT = 500
_REDACTED_RESPONSE_BODY = "[redacted]"
_BODY_ATTRIBUTE_MISSING = object()
_BODY_EMPTY = object()
_BODY_PRESENT = object()
_BODY_UNKNOWN = object()
_ERROR_CODES = {
    "embedding_http_error",
    "embedding_provider_error",
    "embedding_search_error",
    "embedding_store_error",
    "embedding_timeout",
}
_TIMEOUT_ERROR_TYPES = {
    "APITimeoutError",
    "ConnectTimeout",
    "PoolTimeout",
    "ReadTimeout",
    "TimeoutError",
}


class EmbeddingEngine:
    """
    Embedding generation + SQLite vector storage + cosine search.
    ???? + SQLite ???? + ?????
    """

    def __init__(self, config: dict, write_coordinator=None):
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR
        dehy_cfg = config.get("dehydration", {})
        embed_cfg = config.get("embedding", {})

        if embed_cfg.get("independent"):
            self.api_key = str(embed_cfg.get("api_key") or "").strip()
        else:
            self.api_key = (
                embed_cfg.get("api_key") or dehy_cfg.get("api_key") or ""
            ).strip()
        self.base_url = (
            (embed_cfg.get("base_url") or "").strip()
            or (dehy_cfg.get("base_url") or "").strip()
            or "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
        self.model = embed_cfg.get("model", "gemini-embedding-001")
        self.enabled = bool(self.api_key) and embed_cfg.get("enabled", True)
        self.last_error = ""
        self.last_error_details = {}

        # --- SQLite path: buckets_dir/embeddings.db ---
        db_path = os.path.join(config["buckets_dir"], "embeddings.db")
        self.db_path = db_path

        # --- Initialize client ---
        if self.enabled:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=30.0,
            )
        else:
            self.client = None

        # --- Initialize SQLite ---
        self._init_db()

    def _init_db(self):
        """Create embeddings table if not exists."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        initialize_bucket_write_lock(os.path.dirname(self.db_path))
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
        """)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()
        }
        if "model" not in columns:
            conn.execute(
                "ALTER TABLE embeddings ADD COLUMN model TEXT NOT NULL DEFAULT ''"
            )
        conn.commit()
        conn.close()

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        """
        Generate embedding for content and store in SQLite.
        ????? embedding ??? SQLite?
        Returns True on success, False on failure.
        """
        if not self.enabled or not content or not content.strip():
            return False

        try:
            capture = getattr(self,'source_capture',None)
            expected_source = None
            if capture is not None:
                with bucket_write_scope(os.path.dirname(self.db_path)):
                    expected_source = capture(bucket_id)
                    self.write_admission(bucket_id,expected_source=expected_source)
            embedding = await self._generate_embedding(content)
            if not embedding:
                return False
            if expected_source is None:
                self._store_embedding(bucket_id, embedding)
            else:
                self._store_embedding(bucket_id, embedding, expected_source=expected_source)
            self.last_error = ""
            self.last_error_details = {}
            return True
        except Exception as e:
            self._capture_error(e, error_code="embedding_store_error")
            logger.warning(
                "Embedding store failed [%s:%s]",
                self.last_error,
                self.last_error_details.get("error_type", "Exception"),
            )
            return False

    async def embed_text(self, text: str) -> list[float]:
        """Generate one embedding through the existing Host provider path."""
        return await self._generate_embedding(text)

    async def _generate_embedding(self, text: str, *, model: str | None = None) -> list[float]:
        """Call API to generate embedding vector."""
        # Truncate to avoid token limits
        truncated = text[:2000]
        try:
            response = await self.client.embeddings.create(
                model=self.model if model is None else model,
                input=truncated,
            )
            if response.data and len(response.data) > 0:
                return response.data[0].embedding
            return []
        except Exception as e:
            self._capture_error(e)
            logger.warning(
                "Embedding API call failed [%s:%s]",
                self.last_error,
                self.last_error_details.get("error_type", "Exception"),
            )
            return []

    def _capture_error(
        self,
        error: Exception,
        *,
        error_code: str | None = None,
    ) -> None:
        """Keep bounded diagnostics without retaining upstream content."""
        response = _safe_getattr(error, "response")
        request = _safe_getattr(error, "request")
        if request is None and response is not None:
            request = _safe_getattr(response, "request")

        error_type = _safe_error_type(error)
        selected_code = (
            error_code
            if error_code in _ERROR_CODES
            else _embedding_error_code(
                error,
                response=response,
                error_type=error_type,
            )
        )
        self.last_error = selected_code
        self.last_error_details = {
            "request_url": _sanitize_request_url(
                _safe_getattr(request, "url")
            ),
            "status_code": _sanitize_status_code(
                _safe_getattr(response, "status_code")
            ),
            "response_body": _redacted_response_body(response),
            "error_type": error_type,
        }

    @guarded_mutation("bucket_embedding_store")
    def _store_embedding(self, bucket_id: str, embedding: list[float], *, expected_source=None):
        """Store embedding in SQLite."""
        with bucket_write_scope(os.path.dirname(self.db_path)):
            admission = getattr(self, "write_admission", None)
            if admission is not None:
                if expected_source is None:
                    admission(bucket_id)
                else:
                    admission(bucket_id,expected_source=expected_source)
            from utils import now_iso
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                INSERT OR REPLACE INTO embeddings
                    (bucket_id, embedding, model, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (bucket_id, json.dumps(embedding), self.model, now_iso()),
            )
            conn.commit()
            conn.close()

    def archive_embedding_evidence(self, bucket_id, input_digest, model):
        """Read legacy schemas without migration; only bound, valid rows prove input."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(embeddings)")}
            if "input_digest" not in columns:
                if conn.execute("SELECT 1 FROM embeddings WHERE bucket_id=?", (bucket_id,)).fetchone():
                    raise ValueError("archive_embedding_evidence_conflict")
                return None
            row = conn.execute(
                "SELECT embedding, model, input_digest FROM embeddings WHERE bucket_id=?",
                (bucket_id,),
            ).fetchone()
        if row is None:
            return None
        if row[1:] != (model, input_digest):
            raise ValueError("archive_embedding_evidence_conflict")
        try:
            vector = json.loads(row[0])
            if not self._valid_archive_vector(vector):
                raise ValueError("archive_embedding_evidence_conflict")
        except (TypeError, ValueError):
            raise ValueError("archive_embedding_evidence_conflict") from None
        return {"outcome": "stored", "input_digest": input_digest, "model": model,
                "vector_digest": hashlib.sha256(row[0].encode()).hexdigest()}

    @staticmethod
    def _valid_archive_vector(vector):
        return (isinstance(vector, list) and bool(vector)
                and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) for value in vector))

    @guarded_mutation("archive_embedding_store")
    def store_archive_embedding(self, bucket_id, vector, input_digest, model, created_at):
        """Called only under the archive root mutex after winner revalidation."""
        with bucket_write_scope(os.path.dirname(self.db_path)):
            admission = getattr(self, "write_admission", None)
            if admission is not None:
                admission(bucket_id)
            if not self._valid_archive_vector(vector):
                raise ValueError("archive_embedding_invalid")
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                columns = {row[1] for row in conn.execute("PRAGMA table_info(embeddings)")}
                if "input_digest" not in columns:
                    conn.execute("ALTER TABLE embeddings ADD COLUMN input_digest TEXT NOT NULL DEFAULT ''")
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings "
                    "(bucket_id,embedding,model,updated_at,input_digest) VALUES(?,?,?,?,?)",
                    (bucket_id, json.dumps(vector), model, created_at, input_digest),
                )

    def trace_embedding_receipt(self, effect_key):
        with closing(sqlite3.connect(self.db_path)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ob_s4_embedding_effects'").fetchone():
                return None
            row = conn.execute('SELECT receipt_json FROM ob_s4_embedding_effects WHERE effect_key=?',
                               (effect_key,)).fetchone()
        return json.loads(row[0]) if row else None

    @guarded_mutation("trace_embedding_store")
    def store_trace_embedding(self, effect_key, bucket_id, vector, input_digest, model, logical_time, *, expected_source=None):
        """Caller holds root mutex and has revalidated its epoch and current body."""
        with bucket_write_scope(os.path.dirname(self.db_path)):
            admission = getattr(self, "write_admission", None)
            if admission is not None:
                if expected_source is None:
                    admission(bucket_id)
                else:
                    admission(bucket_id,expected_source=expected_source)
            if not self._valid_archive_vector(vector):
                raise ValueError('trace_embedding_invalid')
            serialized = json.dumps(vector)
            receipt = {'outcome': 'applied', 'bucket_id': bucket_id, 'input_digest': input_digest,
                       'model': model, 'vector_digest': hashlib.sha256(serialized.encode()).hexdigest()}
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('''CREATE TABLE IF NOT EXISTS ob_s4_embedding_effects (
                    effect_key TEXT PRIMARY KEY, receipt_json TEXT NOT NULL)''')
                row = conn.execute('SELECT receipt_json FROM ob_s4_embedding_effects WHERE effect_key=?',
                                   (effect_key,)).fetchone()
                if row:
                    return json.loads(row[0])
                conn.execute('''INSERT OR REPLACE INTO embeddings
                    (bucket_id,embedding,model,updated_at) VALUES(?,?,?,?)''',
                    (bucket_id, serialized, model, logical_time))
                conn.execute('INSERT INTO ob_s4_embedding_effects VALUES(?,?)', (effect_key, json.dumps(receipt)))
            return receipt

    @guarded_mutation("bucket_embedding_delete")
    def delete_embedding(self, bucket_id: str):
        """Remove embedding when bucket is deleted."""
        with bucket_write_scope(os.path.dirname(self.db_path)):
            admission = getattr(self, "write_admission", None)
            if admission is not None:
                admission(bucket_id, require_source=False)
            conn = sqlite3.connect(self.db_path)
            conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (bucket_id,))
            conn.commit()
            conn.close()

    @guarded_mutation("confirmed_embedding_delete")
    def delete_confirmed_embedding(self, effect_key, bucket_id, *, admission=None):
        """DELETE and its existing S-4 effect receipt share one durable transaction."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("CREATE TABLE IF NOT EXISTS ob_s4_embedding_effects (effect_key TEXT PRIMARY KEY, receipt_json TEXT NOT NULL)")
            row = conn.execute('SELECT receipt_json FROM ob_s4_embedding_effects WHERE effect_key=?',(effect_key,)).fetchone()
            if row:
                receipt = json.loads(row[0])
                if receipt.get('bucket_id') != bucket_id or receipt.get('kind') != 'delete':
                    raise ValueError('confirmed_embedding_effect_conflict')
                return receipt
            self.embedding_effect_checkpoint('before_delete',effect_key)
            if admission is not None:
                admission()
            count = conn.execute('DELETE FROM embeddings WHERE bucket_id=?',(bucket_id,)).rowcount
            receipt = {'kind':'delete','bucket_id':bucket_id,'effect_key':effect_key,
                       'outcome':'deleted' if count else 'already_absent'}
            conn.execute('INSERT INTO ob_s4_embedding_effects VALUES(?,?)',(effect_key,json.dumps(receipt)))
            self.embedding_effect_checkpoint('before_commit',effect_key)
            if admission is not None:
                admission()
            conn.commit()
            return receipt

    def embedding_effect_checkpoint(self, boundary, effect_key):
        """Synchronous transaction failure-injection seam."""

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        """Retrieve stored embedding for a bucket. Returns None if not found."""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            """
            SELECT embedding FROM embeddings
            WHERE bucket_id = ? AND model = ?
            """,
            (bucket_id, self.model),
        ).fetchone()
        conn.close()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        return None

    async def search_similar(
        self,
        query: str,
        top_k: int = 10,
        candidate_ids: set[str] | list[str] | None = None,
    ) -> list[tuple[str, float]]:
        """
        Search for buckets similar to query text.
        Returns list of (bucket_id, similarity_score) sorted by score desc.
        ?????????????? (bucket_id, ?????) ???
        """
        if not self.enabled:
            return []

        if candidate_ids is not None:
            candidate_ids = {str(bucket_id) for bucket_id in candidate_ids}
            if not candidate_ids:
                return []

        try:
            query_embedding = await self._generate_embedding(query)
            if not query_embedding:
                return []
        except Exception as e:
            self._capture_error(e, error_code="embedding_search_error")
            logger.warning(
                "Embedding search failed [%s:%s]",
                self.last_error,
                self.last_error_details.get("error_type", "Exception"),
            )
            return []

        # Load embeddings from SQLite. A candidate restriction is used by
        # structured breath filters so a valid filtered candidate cannot be
        # displaced by unrelated global top-N vectors.
        conn = sqlite3.connect(self.db_path)
        if candidate_ids is None:
            rows = conn.execute(
                "SELECT bucket_id, embedding FROM embeddings WHERE model = ?",
                (self.model,),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = conn.execute(
                "SELECT bucket_id, embedding FROM embeddings "
                f"WHERE model = ? AND bucket_id IN ({placeholders})",
                (self.model, *sorted(candidate_ids)),
            ).fetchall()
        conn.close()

        if not rows:
            return []

        # Calculate cosine similarity
        results = []
        for bucket_id, emb_json in rows:
            try:
                stored_embedding = json.loads(emb_json)
                sim = self._cosine_similarity(query_embedding, stored_embedding)
                results.append((bucket_id, sim))
            except (json.JSONDecodeError, Exception):
                continue

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


def _safe_getattr(value, name: str):
    if value is None:
        return None
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _safe_error_type(error: Exception) -> str:
    try:
        error_class = type(error)
        candidate = error_class.__name__
        if type(candidate) is not str:
            return "Exception"
        normalized = "".join(
            char
            if char.isascii() and (char.isalnum() or char == "_")
            else "_"
            for char in candidate[:_ERROR_TYPE_LIMIT]
        )
        return normalized or "Exception"
    except Exception:
        return "Exception"


def _embedding_error_code(
    error: Exception,
    *,
    response,
    error_type: str,
) -> str:
    if response is not None:
        return "embedding_http_error"
    if isinstance(error, TimeoutError) or error_type in _TIMEOUT_ERROR_TYPES:
        return "embedding_timeout"
    return "embedding_provider_error"


def _sanitize_request_url(value) -> str:
    try:
        if type(value) is not str:
            return ""
        raw = value
        if not raw or len(raw) > _REQUEST_URL_LIMIT:
            return ""
        if any(ord(char) < 32 or ord(char) == 127 for char in raw):
            return ""
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        hostname = parsed.hostname
        if ":" in hostname:
            if not re.fullmatch(r"[0-9A-Fa-f:.]+", hostname):
                return ""
        elif not re.fullmatch(r"[A-Za-z0-9.-]+", hostname):
            return ""
        port = parsed.port
        if (scheme == "https" and port == 443) or (
            scheme == "http" and port == 80
        ):
            port = None
        host = "[{}]".format(hostname) if ":" in hostname else hostname
        netloc = "{}:{}".format(host, port) if port is not None else host
        sanitized = "{}://{}".format(scheme, netloc)
        if len(sanitized) > _REQUEST_URL_LIMIT:
            return ""
        return sanitized
    except Exception:
        return ""


def _sanitize_status_code(value):
    if type(value) is int and 100 <= value <= 599:
        return value
    return None


def _redacted_response_body(response) -> str:
    if response is None:
        return ""

    text_state = _classify_body_attribute(response, "text")
    content_state = _classify_body_attribute(response, "content")
    if text_state is _BODY_PRESENT or content_state is _BODY_PRESENT:
        return _REDACTED_RESPONSE_BODY
    if text_state is _BODY_UNKNOWN or content_state is _BODY_UNKNOWN:
        return _REDACTED_RESPONSE_BODY
    if text_state is _BODY_EMPTY and content_state is _BODY_EMPTY:
        return ""
    return _REDACTED_RESPONSE_BODY


def _classify_body_attribute(response, name: str):
    try:
        body = getattr(response, name, _BODY_ATTRIBUTE_MISSING)
    except Exception:
        return _BODY_UNKNOWN
    if body is _BODY_ATTRIBUTE_MISSING:
        return _BODY_UNKNOWN
    return _classify_body_value(body)


def _classify_body_value(body):
    if body is None:
        return _BODY_EMPTY
    if type(body) in {str, bytes, bytearray, memoryview}:
        return _BODY_PRESENT if len(body) else _BODY_EMPTY
    return _BODY_PRESENT
