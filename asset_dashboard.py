from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from asset_store import MAX_IMAGE_PIXELS, AssetStore, AssetStoreError


ALLOWED_IMAGE_MIME_TYPES = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
}
DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 50
THUMBNAIL_SIZE = (360, 240)


class AssetDashboardError(Exception):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class AssetImage:
    asset: dict
    path: Path
    mime_type: str
    thumbnail_bytes: bytes | None = None


class AssetDashboardService:
    """Read-only web adapter for RM assets, independent from memory buckets."""

    def __init__(
        self,
        store: AssetStore,
        *,
        max_asset_bytes: int,
        max_image_pixels: int = MAX_IMAGE_PIXELS,
    ):
        self.store = store
        self.max_asset_bytes = max_asset_bytes
        self.max_image_pixels = max_image_pixels

    @staticmethod
    def parse_pagination(limit_value: str, offset_value: str) -> tuple[int, int]:
        try:
            limit = int(limit_value or DEFAULT_PAGE_LIMIT)
            offset = int(offset_value or 0)
        except (TypeError, ValueError) as exc:
            raise AssetDashboardError("invalid_pagination") from exc
        if not 1 <= limit <= MAX_PAGE_LIMIT or offset < 0:
            raise AssetDashboardError("invalid_pagination")
        return limit, offset

    @staticmethod
    def _safe_metadata(asset: dict) -> dict:
        asset_id = asset["asset_id"]
        return {
            "asset_id": asset_id,
            "filename": asset["original_filename"],
            "title": asset.get("title", ""),
            "description": asset.get("description", ""),
            "tags": list(asset.get("tags", [])),
            "kind": asset["kind"],
            "mime_type": asset["mime_type"],
            "width": asset["width"],
            "height": asset["height"],
            "stored_bytes": asset["stored_bytes"],
            "created_at": asset["created_at"],
            "updated_at": asset["updated_at"],
            "thumbnail_url": f"/api/assets/{asset_id}/thumbnail",
            "image_url": f"/api/assets/{asset_id}/image",
            "detail_url": f"/api/assets/{asset_id}",
        }

    def list_assets(
        self,
        *,
        query: str = "",
        tag: str = "",
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
    ) -> dict:
        tags = [tag] if tag.strip() else None
        try:
            result = self.store.search(
                query=query,
                tags=tags,
                kind="image",
                limit=limit,
                offset=offset,
            )
        except AssetStoreError as exc:
            raise AssetDashboardError(str(exc)) from exc
        return {
            "total": result["total"],
            "offset": result["offset"],
            "limit": result["limit"],
            "results": [
                self._safe_metadata(
                    {
                        **item,
                        "original_filename": item["filename"],
                    }
                )
                for item in result["results"]
            ],
        }

    def get_asset(self, asset_id: str) -> dict:
        asset = self.store.get(asset_id)
        if not asset or asset.get("kind") != "image":
            raise AssetDashboardError("asset_not_found", 404)
        if asset.get("mime_type") not in ALLOWED_IMAGE_MIME_TYPES:
            raise AssetDashboardError("unsupported_image", 415)
        return self._safe_metadata(asset)

    def resolve_image(self, asset_id: str, *, thumbnail: bool = False) -> AssetImage:
        try:
            resolved = self.store.resolve_file(asset_id)
        except AssetStoreError as exc:
            raise AssetDashboardError("asset_unavailable", 404) from exc
        if not resolved:
            raise AssetDashboardError("asset_not_found", 404)
        asset, path = resolved
        mime_type = asset.get("mime_type", "")
        if asset.get("kind") != "image":
            raise AssetDashboardError("asset_not_found", 404)
        expected_format = ALLOWED_IMAGE_MIME_TYPES.get(mime_type)
        if not expected_format:
            raise AssetDashboardError("unsupported_image", 415)
        try:
            actual_bytes = path.stat().st_size
        except OSError as exc:
            raise AssetDashboardError("asset_unavailable", 404) from exc
        if actual_bytes <= 0 or actual_bytes > self.max_asset_bytes:
            raise AssetDashboardError("asset_unavailable", 422)

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as opened:
                    opened.load()
                    actual_format = (opened.format or "").upper()
                    width, height = opened.size
                    if (
                        actual_format != expected_format
                        or width <= 0
                        or height <= 0
                        or width * height > self.max_image_pixels
                        or width != asset["width"]
                        or height != asset["height"]
                    ):
                        raise AssetDashboardError("asset_unavailable", 422)
                    if not thumbnail:
                        return AssetImage(asset, path, mime_type)
                    image = opened.copy()
        except AssetDashboardError:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ) as exc:
            raise AssetDashboardError("asset_unavailable", 422) from exc

        image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        if mime_type == "image/jpeg":
            if image.mode != "RGB":
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=82, optimize=True)
        else:
            image.save(output, format="PNG", optimize=True)
        image.close()
        return AssetImage(asset, path, mime_type, output.getvalue())
