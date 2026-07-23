from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import warnings
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_IMAGE_PIXELS = 20_000_000
SUPPORTED_MIME_TYPES = {
    "application/octet-stream",
    "image/jpeg",
    "image/png",
}


class AssetStoreError(Exception):
    pass


class InvalidAssetImage(AssetStoreError):
    pass


class AssetStore:
    def __init__(self, data_root: str):
        self.data_root = Path(data_root).resolve()
        self.assets_dir = self.data_root / "assets"
        self.temp_dir = self.assets_dir / ".tmp"
        self.db_path = self.data_root / "assets.sqlite3"
        self._lock = threading.Lock()
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    stored_sha256 TEXT NOT NULL UNIQUE,
                    stored_relpath TEXT NOT NULL,
                    original_filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    decoded_bytes INTEGER NOT NULL,
                    stored_bytes INTEGER NOT NULL,
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_stored_sha256 "
                "ON assets(stored_sha256)"
            )

    def create_temp_path(self, suffix: str = ".upload") -> Path:
        fd, raw_path = tempfile.mkstemp(prefix="rm-", suffix=suffix, dir=self.temp_dir)
        os.close(fd)
        return Path(raw_path)

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        cleaned = re.sub(r"[\x00-\x1f\x7f/\\:]+", "_", (filename or "").strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return cleaned[:255] or "asset.bin"

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(block)
                size += len(block)
        return digest.hexdigest(), size

    def _clean_image(self, source_path: Path) -> tuple[Path, str, str, int, int]:
        output_path: Path | None = None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source_path) as opened:
                    image_format = (opened.format or "").upper()
                    if image_format not in {"JPEG", "PNG"}:
                        raise InvalidAssetImage("unsupported_image_format")
                    width, height = opened.size
                    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                        raise InvalidAssetImage("image_pixel_limit")
                    opened.load()
                    oriented = ImageOps.exif_transpose(opened)
                    output_format = "PNG" if image_format == "PNG" else "JPEG"
                    output_mime = "image/png" if output_format == "PNG" else "image/jpeg"
                    extension = ".png" if output_format == "PNG" else ".jpg"
                    mode = "RGBA" if output_format == "PNG" and "A" in oriented.getbands() else "RGB"
                    converted = oriented.convert(mode)
                    clean = Image.new(mode, converted.size)
                    clean.paste(converted)
                    output_path = self.create_temp_path(extension)
                    if output_format == "PNG":
                        clean.save(output_path, format="PNG", optimize=True)
                    else:
                        clean.save(
                            output_path,
                            format="JPEG",
                            quality=90,
                            optimize=True,
                            exif=b"",
                        )
                    width, height = clean.size
                    clean.close()
                    converted.close()
                    if oriented is not opened:
                        oriented.close()
            return output_path, output_mime, extension, width, height
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            if output_path:
                output_path.unlink(missing_ok=True)
            raise InvalidAssetImage("image_pixel_limit") from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            if output_path:
                output_path.unlink(missing_ok=True)
            raise InvalidAssetImage("invalid_image") from exc
        except Exception:
            if output_path:
                output_path.unlink(missing_ok=True)
            raise

    def _prepare_candidate(
        self,
        source_path: Path,
        claimed_mime: str,
    ) -> tuple[Path, str, str, str, int, int]:
        try:
            with Image.open(source_path) as probe:
                detected = (probe.format or "").upper()
        except UnidentifiedImageError:
            detected = ""
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise InvalidAssetImage("image_pixel_limit") from exc
        except OSError:
            detected = ""

        if detected:
            if detected not in {"JPEG", "PNG"}:
                raise InvalidAssetImage("unsupported_image_format")
            candidate, mime_type, extension, width, height = self._clean_image(source_path)
            return candidate, mime_type, "image", extension, width, height
        if claimed_mime in {"image/jpeg", "image/png"}:
            raise InvalidAssetImage("invalid_image")
        return source_path, "application/octet-stream", "file", ".bin", 0, 0

    def persist_upload(
        self,
        source_path: str | Path,
        source_sha256: str,
        decoded_bytes: int,
        original_filename: str,
        mime_type: str,
    ) -> dict:
        source = Path(source_path)
        candidate: Path | None = None
        destination: Path | None = None
        moved_new_file = False
        claimed_mime = (mime_type or "application/octet-stream").strip().lower()
        if claimed_mime not in SUPPORTED_MIME_TYPES:
            source.unlink(missing_ok=True)
            raise AssetStoreError("unsupported_mime_type")

        try:
            actual_source_sha, actual_source_bytes = self._hash_file(source)
            if actual_source_bytes != decoded_bytes:
                raise AssetStoreError("source_size_mismatch")
            if not re.fullmatch(r"[0-9a-f]{64}", source_sha256 or ""):
                raise AssetStoreError("invalid_source_sha256")
            if not secrets.compare_digest(actual_source_sha, source_sha256):
                raise AssetStoreError("source_hash_mismatch")

            candidate, stored_mime, kind, extension, width, height = self._prepare_candidate(
                source, claimed_mime
            )
            stored_sha256, stored_bytes = self._hash_file(candidate)
            stored_relpath = Path("assets") / stored_sha256[:2] / f"{stored_sha256}{extension}"
            destination = self.data_root / stored_relpath
            filename = self.sanitize_filename(original_filename)

            with self._lock:
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = conn.execute(
                        "SELECT * FROM assets WHERE stored_sha256 = ?",
                        (stored_sha256,),
                    ).fetchone()
                    if existing:
                        conn.commit()
                        result = dict(existing)
                        result["deduplicated"] = True
                        return result

                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        existing_sha, _ = self._hash_file(destination)
                        if not secrets.compare_digest(existing_sha, stored_sha256):
                            raise AssetStoreError("stored_file_conflict")
                    else:
                        os.replace(candidate, destination)
                        moved_new_file = True

                    asset_id = secrets.token_hex(16)
                    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    conn.execute(
                        """
                        INSERT INTO assets (
                            asset_id, source_sha256, stored_sha256, stored_relpath,
                            original_filename, mime_type, kind, decoded_bytes,
                            stored_bytes, width, height, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            asset_id,
                            source_sha256,
                            stored_sha256,
                            stored_relpath.as_posix(),
                            filename,
                            stored_mime,
                            kind,
                            decoded_bytes,
                            stored_bytes,
                            width,
                            height,
                            created_at,
                        ),
                    )
                    conn.commit()
                    result = self.get(asset_id)
                    if result is None:
                        raise AssetStoreError("asset_insert_failed")
                    result["deduplicated"] = False
                    return result
                except Exception:
                    conn.rollback()
                    if moved_new_file and destination:
                        destination.unlink(missing_ok=True)
                    raise
                finally:
                    conn.close()
        finally:
            source.unlink(missing_ok=True)
            if candidate and candidate != destination:
                candidate.unlink(missing_ok=True)

    def get(self, asset_id: str) -> dict | None:
        if not re.fullmatch(r"[0-9a-f]{32}", asset_id or ""):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return dict(row) if row else None

    def resolve_file(self, asset_id: str) -> tuple[dict, Path] | None:
        asset = self.get(asset_id)
        if not asset:
            return None
        path = (self.data_root / asset["stored_relpath"]).resolve()
        try:
            path.relative_to(self.assets_dir.resolve())
        except ValueError as exc:
            raise AssetStoreError("invalid_stored_path") from exc
        if not path.is_file():
            return None
        return asset, path
