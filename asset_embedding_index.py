from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
from datetime import datetime, timezone

from maintenance_write_gate import (
    DEFAULT_WRITE_COORDINATOR,
    guarded_mutation,
)


logger = logging.getLogger("ombre_brain.asset_embedding")

ASSET_SEMANTIC_THRESHOLD = 0.42


class _SemanticScores(dict):
    """Private score bindings, rechecked in the final asset read snapshot."""

    def __init__(self, scores, index, model, bindings):
        super().__init__(scores)
        self._index, self._model, self._bindings = index, model, bindings

    def _validated_scores(self, conn, rows, tags_by_asset):
        index = self._index
        if index.embedding_engine.model != self._model:
            return {}
        result = {}
        for row in rows:
            identity = row['asset_id']
            binding = self._bindings.get(identity)
            if binding is None:
                continue
            asset = dict(row)
            asset['tags'] = [tag['tag_display'] for tag in tags_by_asset[identity]]
            if (index._row(conn, identity) == binding
                    and binding['content_hash'] == index._content_hash(index.build_index_text(asset))):
                result[identity] = self[identity]
        return result


class AssetEmbeddingIndex:
    def __init__(self, asset_store, embedding_engine):
        self.asset_store = asset_store
        self.embedding_engine = embedding_engine
        self.write_coordinator = getattr(
            asset_store,
            "write_coordinator",
            DEFAULT_WRITE_COORDINATOR,
        )
        self.db_path = asset_store.db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS asset_embeddings (
                    asset_id TEXT PRIMARY KEY,
                    embedding TEXT NOT NULL,
                    model TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
                        ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_asset_embeddings_model "
                "ON asset_embeddings(model)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_asset_embeddings_content_hash "
                "ON asset_embeddings(content_hash)"
            )
            conn.execute(
                """
                DELETE FROM asset_embeddings
                WHERE asset_id NOT IN (SELECT asset_id FROM assets)
                """
            )

    @staticmethod
    def build_index_text(asset: dict) -> str:
        title = str(asset.get("title", "") or "").strip()
        description = str(asset.get("description", "") or "").strip()
        tags = [
            str(tag).strip()
            for tag in asset.get("tags", [])
            if str(tag).strip()
        ]
        if not title and not description and not tags:
            return ""
        return "\n".join(
            [
                f"Title: {title}",
                f"Description: {description}",
                f"Tags: {', '.join(tags)}",
                f"Filename: {asset.get('original_filename', '')}",
                f"Kind: {asset.get('kind', '')}",
                f"MIME type: {asset.get('mime_type', '')}",
            ]
        )

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _existing(self, asset_id: str) -> dict | None:
        with self._connect() as conn:
            return self._row(conn, asset_id)

    @staticmethod
    def _row(conn, asset_id):
        row = conn.execute('SELECT * FROM asset_embeddings WHERE asset_id = ?', (asset_id,)).fetchone()
        return dict(row) if row else None

    def _asset(self, conn, asset_id):
        row = conn.execute('SELECT * FROM assets WHERE asset_id = ?', (asset_id,)).fetchone()
        if row is None:
            return None
        asset = dict(row)
        asset['tags'] = [tag['tag_display'] for tag in self.asset_store._tags_for_assets(conn, [asset_id])[asset_id]]
        return asset

    @staticmethod
    def _valid_vector(vector):
        try:
            return (isinstance(vector, list) and bool(vector)
                    and all(type(value) in (int, float) and math.isfinite(value) for value in vector)
                    and any(value != 0 for value in vector))
        except OverflowError:
            return False

    def _current(self, row, text, model):
        if row is None or row['model'] != model or row['content_hash'] != self._content_hash(text):
            return False
        try:
            return self._valid_vector(json.loads(row['embedding']))
        except (ValueError, TypeError):
            return False

    @guarded_mutation("asset_embedding_delete")
    def delete(self, asset_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", asset_id or ""):
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM asset_embeddings WHERE asset_id = ?",
                (asset_id,),
            )

    def is_current(self, asset: dict) -> bool:
        with self._connect() as conn:
            conn.execute('BEGIN')
            current = self._asset(conn, asset['asset_id'])
            if current is None:
                return False
            text = self.build_index_text(current)
            existing = self._row(conn, asset['asset_id'])
            return (existing is None if not text else self._current(existing, text, self.embedding_engine.model))

    async def index_asset(self, asset: dict) -> str:
        asset_id = asset["asset_id"]
        text = self.build_index_text(asset)
        model = self.embedding_engine.model
        content_hash = self._content_hash(text)
        with self._connect() as conn:
            conn.execute('BEGIN')
            current = self._asset(conn, asset_id)
            existing = self._row(conn, asset_id)
        if current is None or self._content_hash(self.build_index_text(current)) != content_hash:
            return "failed"
        if text and self._current(existing, text, model):
            return "skipped"
        if text and not self.embedding_engine.enabled:
            return "failed"

        try:
            # No transaction, mutex, writer scope or early deletion across await.
            embedding = await self.embedding_engine._generate_embedding(text, model=model) if text else None
        except Exception as exc:
            logger.warning(
                "Asset embedding generation failed asset_id=%s error=%s",
                asset_id,
                type(exc).__name__,
            )
            return "failed"
        if text and not self._valid_vector(embedding):
            logger.warning(
                "Asset embedding generation returned invalid vector asset_id=%s",
                asset_id,
            )
            return "failed"

        with self.write_coordinator.writer_scope("asset_embedding_store"):
            with self._lock, self._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = self._asset(conn, asset_id)
                if (current is None or self.embedding_engine.model != model
                        or self._content_hash(self.build_index_text(current)) != content_hash):
                    return "failed"
                observed = self._row(conn, asset_id)
                if observed != existing:
                    # A concurrent completed rebuild wins; never overwrite it.
                    return "skipped" if text and self._current(observed, text, model) else "failed"
                if not text:
                    conn.execute('DELETE FROM asset_embeddings WHERE asset_id = ?', (asset_id,))
                    return "skipped"
                conn.execute(
                    """
                    INSERT INTO asset_embeddings (
                        asset_id, embedding, model, content_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(asset_id) DO UPDATE SET
                        embedding = excluded.embedding,
                        model = excluded.model,
                        content_hash = excluded.content_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        asset_id,
                        json.dumps(embedding, allow_nan=False),
                        model,
                        content_hash,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        return "indexed"

    async def search(
        self,
        query: str,
        top_k: int = 100,
        threshold: float = ASSET_SEMANTIC_THRESHOLD,
    ) -> dict[str, float]:
        if not self.embedding_engine.enabled or not query.strip():
            return {}
        model = self.embedding_engine.model
        try:
            query_embedding = await self.embedding_engine._generate_embedding(query, model=model)
        except Exception as exc:
            logger.warning(
                "Asset semantic query failed error=%s",
                type(exc).__name__,
            )
            return {}
        if not self._valid_vector(query_embedding) or self.embedding_engine.model != model:
            return {}

        with self._connect() as conn:
            conn.execute('BEGIN')
            rows = conn.execute(
                """
                SELECT ae.*
                FROM asset_embeddings ae
                JOIN assets a ON a.asset_id = ae.asset_id
                WHERE ae.model = ?
                """,
                (model,),
            ).fetchall()
            candidates = [(dict(row), self._asset(conn, row['asset_id'])) for row in rows]

        results = []
        bindings = {}
        for row, asset in candidates:
            if asset is None or not self._current(row, self.build_index_text(asset), model):
                continue
            try:
                stored = json.loads(row["embedding"])
                if len(stored) != len(query_embedding):
                    continue
                score = self.embedding_engine._cosine_similarity(
                    query_embedding,
                    stored,
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if score >= threshold:
                results.append((row["asset_id"], score))
                bindings[row['asset_id']] = row
        results.sort(key=lambda item: (-item[1], item[0]))
        scores = {
            asset_id: round(score, 6)
            for asset_id, score in results[:top_k]
        }
        return _SemanticScores(scores, self, model, {identity: bindings[identity] for identity in scores})

    async def reindex(
        self,
        asset_id: str = "",
        limit: int = 100,
    ) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("invalid_limit")
        if asset_id:
            asset = self.asset_store.get(asset_id)
            if not asset:
                raise ValueError("asset_unavailable")
            assets = [asset]
        else:
            assets = self.asset_store.list_for_embedding(limit)

        counts = {
            "scanned": 0,
            "indexed": 0,
            "skipped": 0,
            "failed": 0,
        }
        for asset in assets:
            counts["scanned"] += 1
            try:
                status = await self.index_asset(asset)
            except Exception as exc:
                logger.warning(
                    "Asset embedding reindex failed asset_id=%s error=%s",
                    asset["asset_id"],
                    type(exc).__name__,
                )
                status = "failed"
            counts[status] += 1
        return counts
