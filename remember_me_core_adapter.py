"""Ombre-Brain compatibility boundary for the public Remember-Me Core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any

from remember_me.core import (
    AssetFileUnavailable,
    AssetNotFoundError,
    AssetUnavailable,
    DeleteAssetRequest,
    GetAssetRequest,
    ImagePixelLimitExceeded,
    ImageValidationError,
    IngestImageRequest,
    InvalidMetadata,
    ReindexEmbeddingsRequest,
    RememberMeError,
    ResolveAssetRequest,
    SearchAssetsRequest,
    StorageConsistencyError,
    UpdateMetadataRequest,
    UploadSizeMismatch,
    UploadTooLarge,
)

_ASSET_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_OB_METADATA_ERROR_CODES = {
    "description_too_long",
    "invalid_description",
    "invalid_tag",
    "invalid_tags",
    "invalid_title",
    "tag_too_long",
    "title_too_long",
    "too_many_tags",
}
_OB_SEARCH_ERROR_CODES = {
    "invalid_query",
    "invalid_limit",
    "invalid_offset",
    "invalid_kind",
    "invalid_mime_type",
    "invalid_created_from",
    "invalid_created_to",
    "invalid_date_range",
    "invalid_tags",
    "invalid_tag",
    "tag_too_long",
    "too_many_tags",
}


class RememberMeCoreAdapterError(RuntimeError):
    """Stable, path-free error returned by the OB Core compatibility layer."""

    def __init__(self, code: str, *, ob_code: str | None = None):
        self.code = code
        self.ob_code = ob_code
        super().__init__(code)


@dataclass(frozen=True)
class RememberMeReindexResult:
    scanned: int
    indexed: int
    skipped: int
    failed: int

    def __post_init__(self) -> None:
        counters = (
            self.scanned,
            self.indexed,
            self.skipped,
            self.failed,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in counters
        ) or self.scanned != self.indexed + self.skipped + self.failed:
            raise ValueError("invalid_reindex_counters")


class RememberMeCoreAdapter:
    """Translate public Remember-Me Core operations into OB-safe structures."""

    def __init__(self, runtime: Any, *, host_adapter: Any = None) -> None:
        if runtime is None or not all(
            hasattr(runtime, name)
            for name in ("service", "repository", "blob_store")
        ):
            raise RememberMeCoreAdapterError("runtime_unavailable")
        self._runtime = runtime
        self._host_adapter = host_adapter

    @classmethod
    def from_host_adapter(
        cls,
        host_adapter: Any,
        data_root: Path,
        *,
        vector_provider: Any = None,
    ):
        """Explicitly create one RM runtime through the Stage 8B host owner."""
        if not isinstance(data_root, Path):
            raise RememberMeCoreAdapterError("invalid_data_root")
        try:
            runtime = host_adapter.create_runtime(
                data_root,
                vector_provider=vector_provider,
            )
        except Exception as exc:
            code = str(exc)
            if code == "remember_me_data_root_already_owned":
                raise RememberMeCoreAdapterError(
                    "runtime_already_owned"
                ) from exc
            raise RememberMeCoreAdapterError("runtime_unavailable") from exc
        return cls(runtime, host_adapter=host_adapter)

    def ingest_image(
        self,
        content: bytes,
        expected_bytes: int,
        filename: str,
        mime_type: str = "application/octet-stream",
        *,
        title: str = "",
        description: str = "",
        tags: list[str] | tuple[str, ...] = (),
    ) -> dict:
        try:
            clean_tags = self._tag_tuple(tags)
            result = self._runtime.service.ingest_image(
                IngestImageRequest(
                    content=content,
                    expected_bytes=expected_bytes,
                    filename=filename,
                    mime_type=mime_type,
                    title=title,
                    description=description,
                    tags=clean_tags,
                )
            )
            asset = self._asset_dict(result.asset)
            asset["deduplicated"] = bool(result.deduplicated)
            return asset
        except Exception as exc:
            self._raise_mapped(exc)

    def ingest_ob_public_metadata(
        self,
        content: bytes,
        expected_bytes: int,
        filename: str,
        mime_type: str = "application/octet-stream",
        *,
        title: str = "",
        description: str = "",
        tags: list[str] | tuple[str, ...] = (),
    ) -> dict:
        try:
            clean_tags = self._tag_tuple(tags)
            result = self._runtime.service.ingest_image(
                IngestImageRequest(
                    content=content,
                    expected_bytes=expected_bytes,
                    filename=filename,
                    mime_type=mime_type,
                    title=title,
                    description=description,
                    tags=clean_tags,
                )
            )
            asset = self._ob_public_metadata(result.asset)
            asset["deduplicated"] = bool(result.deduplicated)
            return asset
        except Exception as exc:
            self._raise_mapped(exc)

    def get(self, asset_id: str) -> dict | None:
        if not self._valid_asset_id(asset_id):
            return None
        try:
            asset = self._runtime.service.get_asset(
                GetAssetRequest(asset_id.strip())
            )
            return self._asset_dict(asset)
        except AssetNotFoundError:
            return None
        except Exception as exc:
            self._raise_mapped(exc)

    def get_ob_public_metadata(self, asset_id: str) -> dict | None:
        """Return only fields already public in the current OB MCP contract."""
        if not self._valid_asset_id(asset_id):
            return None
        try:
            asset = self._runtime.service.get_asset(
                GetAssetRequest(asset_id.strip())
            )
            return self._ob_public_metadata(asset)
        except AssetNotFoundError:
            return None
        except RememberMeError as exc:
            self._raise_mapped(exc)

    def update_metadata(
        self,
        asset_id: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        asset = self._update_metadata_asset(
            asset_id,
            title=title,
            description=description,
            tags=tags,
        )
        return self._asset_dict(asset)

    def update_ob_public_metadata(
        self,
        asset_id: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        """Mutate once and return the current OB public metadata shape."""
        asset = self._update_metadata_asset(
            asset_id,
            title=title,
            description=description,
            tags=tags,
        )
        return self._ob_public_metadata(asset)

    def _update_metadata_asset(
        self,
        asset_id: str,
        *,
        title: str | None,
        description: str | None,
        tags: list[str] | tuple[str, ...] | None,
    ) -> Any:
        self._require_asset_id(asset_id)
        try:
            clean_tags = (
                None if tags is None else self._tag_tuple(tags)
            )
            return self._runtime.service.update_metadata(
                UpdateMetadataRequest(
                    asset_id=asset_id.strip(),
                    title=title,
                    description=description,
                    tags=clean_tags,
                )
            )
        except Exception as exc:
            self._raise_mapped(exc)

    async def search(
        self,
        query: str = "",
        tags: list[str] | tuple[str, ...] | None = None,
        kind: str = "",
        mime_type: str = "",
        created_from: str = "",
        created_to: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        try:
            clean_tags = self._tag_tuple(tags or ())
            result = await self._runtime.service.search_assets(
                SearchAssetsRequest(
                    query=query,
                    tags=clean_tags,
                    kind=kind,
                    mime_type=mime_type,
                    created_from=created_from,
                    created_to=created_to,
                    limit=limit,
                    offset=offset,
                )
            )
            items = []
            for item in result.results:
                asset = self._asset_dict(item.asset, search_result=True)
                asset["match_reasons"] = list(item.match_reasons)
                if item.semantic_score is not None:
                    asset["semantic_score"] = item.semantic_score
                items.append(asset)
            return {
                "total": result.total,
                "offset": result.offset,
                "limit": result.limit,
                "results": items,
            }
        except Exception as exc:
            candidate = str(exc)
            if candidate in _OB_SEARCH_ERROR_CODES:
                raise RememberMeCoreAdapterError(candidate) from exc
            self._raise_mapped(exc)

    async def reindex_embeddings(
        self,
        asset_id: str = "",
        limit: int = 100,
    ) -> RememberMeReindexResult:
        try:
            result = await self._runtime.service.reindex_embeddings(
                ReindexEmbeddingsRequest(
                    asset_id=(asset_id or "").strip(),
                    limit=limit,
                )
            )
            return RememberMeReindexResult(
                scanned=result.scanned,
                indexed=result.indexed,
                skipped=result.skipped,
                failed=result.failed,
            )
        except InvalidMetadata as exc:
            if str(exc) == "invalid_limit":
                raise RememberMeCoreAdapterError("invalid_limit") from exc
            raise RememberMeCoreAdapterError("asset_unavailable") from exc
        except AssetUnavailable as exc:
            raise RememberMeCoreAdapterError("asset_unavailable") from exc
        except Exception as exc:
            raise RememberMeCoreAdapterError("asset_unavailable") from exc

    def resolve_blob(self, asset_id: str) -> tuple[dict, bytes]:
        self._require_asset_id(asset_id)
        try:
            resolved = self._runtime.service.resolve_asset(
                ResolveAssetRequest(asset_id.strip())
            )
            content = self._runtime.blob_store.read(resolved.blob_key)
            return self._asset_dict(resolved.asset), content
        except Exception as exc:
            self._raise_mapped(exc)

    def resolve_ob_download(self, asset_id: str) -> tuple[dict, bytes]:
        self._require_asset_id(asset_id)
        try:
            resolved = self._runtime.service.resolve_asset(
                ResolveAssetRequest(asset_id.strip())
            )
            content = self._runtime.blob_store.read(resolved.blob_key)
            metadata = self._ob_public_metadata(resolved.asset)
            if not isinstance(content, bytes):
                raise RememberMeCoreAdapterError("repository_failure")
            if len(content) != metadata["stored_bytes"]:
                raise RememberMeCoreAdapterError("repository_failure")
            if hashlib.sha256(content).hexdigest() != metadata["stored_sha256"]:
                raise RememberMeCoreAdapterError("repository_failure")
            return metadata, bytes(content)
        except Exception as exc:
            self._raise_mapped(exc)

    def delete(self, asset_id: str) -> dict:
        self._require_asset_id(asset_id)
        try:
            result = self._runtime.service.delete_asset(
                DeleteAssetRequest(asset_id.strip())
            )
            return {
                "asset_id": result.asset_id,
                "deleted": bool(result.deleted),
                "cleanup_pending": bool(result.cleanup_pending),
            }
        except Exception as exc:
            self._raise_mapped(exc)

    @staticmethod
    def _asset_dict(asset: Any, *, search_result: bool = False) -> dict:
        timestamp_fields = {
            "created_at": _normalize_timestamp(asset.created_at),
            "updated_at": _normalize_timestamp(asset.updated_at),
        }
        result = {
            "asset_id": asset.asset_id,
            "original_filename": asset.original_filename,
            "mime_type": asset.mime_type,
            "kind": asset.kind,
            "decoded_bytes": asset.decoded_bytes,
            "stored_bytes": asset.stored_bytes,
            "width": asset.width,
            "height": asset.height,
            "title": asset.title,
            "description": asset.description,
            "tags": list(asset.tags),
            **timestamp_fields,
        }
        if search_result:
            result["filename"] = result.pop("original_filename")
        return result

    @staticmethod
    def _ob_public_metadata(asset: Any) -> dict:
        """Match the hashes and metadata already exposed by OB tools."""
        return {
            "asset_id": asset.asset_id,
            "source_sha256": asset.source_sha256,
            "stored_sha256": asset.stored_sha256,
            "decoded_bytes": asset.decoded_bytes,
            "stored_bytes": asset.stored_bytes,
            "mime_type": asset.mime_type,
            "filename": asset.original_filename,
            "kind": asset.kind,
            "width": asset.width,
            "height": asset.height,
            "created_at": _normalize_timestamp(asset.created_at),
            "title": asset.title,
            "description": asset.description,
            "tags": list(asset.tags),
            "updated_at": _normalize_timestamp(asset.updated_at),
        }

    @staticmethod
    def _valid_asset_id(asset_id: Any) -> bool:
        return (
            isinstance(asset_id, str)
            and _ASSET_ID_PATTERN.fullmatch(asset_id.strip()) is not None
        )

    @classmethod
    def _require_asset_id(cls, asset_id: Any) -> None:
        if not cls._valid_asset_id(asset_id):
            raise RememberMeCoreAdapterError("invalid_asset_id")

    @staticmethod
    def _tag_tuple(tags: Any) -> tuple[str, ...]:
        if not isinstance(tags, (list, tuple)):
            raise RememberMeCoreAdapterError("invalid_metadata")
        return tuple(tags)

    @staticmethod
    def _raise_mapped(exc: Exception) -> None:
        ob_code = None
        if isinstance(exc, RememberMeCoreAdapterError):
            raise exc
        if isinstance(exc, AssetFileUnavailable):
            code = "blob_missing"
        elif isinstance(exc, AssetNotFoundError):
            code = "asset_not_found"
        elif isinstance(exc, UploadTooLarge):
            code = "upload_too_large"
        elif isinstance(exc, UploadSizeMismatch):
            code = "upload_size_mismatch"
        elif isinstance(exc, ImagePixelLimitExceeded):
            code = "pixel_limit"
        elif isinstance(exc, ImageValidationError):
            code = "invalid_image"
        elif isinstance(exc, InvalidMetadata):
            candidate = str(exc)
            ob_code = (
                candidate
                if candidate in _OB_METADATA_ERROR_CODES
                else None
            )
            code = "invalid_metadata"
        elif isinstance(exc, StorageConsistencyError):
            code = "repository_failure"
        elif isinstance(exc, RememberMeError):
            code = "core_failure"
        else:
            code = "repository_failure"
        raise RememberMeCoreAdapterError(
            code,
            ob_code=ob_code,
        ) from exc


def _normalize_timestamp(value: str) -> str:
    """Return the seconds-precision UTC format currently emitted by OB."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RememberMeCoreAdapterError("repository_failure") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "RememberMeCoreAdapter",
    "RememberMeCoreAdapterError",
    "RememberMeReindexResult",
]
