"""Lazy Ombre-Brain host boundary for the pinned public Remember-Me Core."""

from __future__ import annotations

from remember_me_dependency import DEPENDENCY

from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
import base64
import hashlib
import importlib
import json
import sys
import threading
import weakref


EXPECTED_DISTRIBUTION = "remember-me"
EXPECTED_PACKAGE_VERSION = DEPENDENCY.version
EXPECTED_DATA_COMPATIBILITY = "ombre-brain-assets-v1"
EXPECTED_SANITIZER_ID = "remember-me-pillow-v1"
EXPECTED_PILLOW_RANGE = "Pillow>=10.4,<13"
EXPECTED_MCP_TOOLS = (
    "rm_asset_upload_link",
    "rm_asset_upload_status",
    "rm_asset_get",
    "rm_asset_update_metadata",
    "rm_asset_reindex_embeddings",
    "rm_asset_search",
    "rm_asset_download_link",
    "rm_asset_view",
    "rm_asset_inspect",
)


class RememberMeAdapterError(RuntimeError):
    """Fail-closed adapter error that does not include host data."""


@dataclass(frozen=True)
class RememberMeContract:
    distribution_name: str
    package_version: str
    data_compatibility: str
    sanitizer_id: str
    pillow_range: str
    mcp_tools: tuple[str, ...]


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _validate_archive_provenance(raw: str) -> None:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
        if not isinstance(value, dict) or set(value) != {"url", "archive_info"}:
            raise ValueError("invalid_direct_url")
        archive = value["archive_info"]
        if value["url"] != DEPENDENCY.url or not isinstance(archive, dict):
            raise ValueError("invalid_direct_url")
        if not archive or set(archive) - {"hash", "hashes"}:
            raise ValueError("invalid_archive_info")
        hashes = archive.get("hashes", {})
        if not isinstance(hashes, dict):
            raise ValueError("invalid_hashes")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in hashes.items()):
            raise ValueError("invalid_hashes")
        legacy = archive.get("hash")
        if "hash" in archive:
            if not isinstance(legacy, str) or legacy.count("=") != 1:
                raise ValueError("invalid_hash")
            algorithm, digest = legacy.split("=")
            if not algorithm or not digest or (algorithm in hashes and hashes[algorithm] != digest):
                raise ValueError("conflicting_hashes")
            hashes = {**hashes, algorithm: digest}
        if hashes.get("sha256") != DEPENDENCY.sha256:
            raise ValueError("wrong_digest")
    except (ValueError, TypeError, KeyError) as exc:
        raise RememberMeAdapterError("remember_me_contract_mismatch:provenance") from exc


def _validate_distribution_modules(distribution) -> None:
    """Reject shadow modules and changed installed source recorded by this distribution."""
    try:
        files = distribution.files
        if not files:
            raise ValueError("missing_record")
        owned = {Path(distribution.locate_file(f)).resolve(): f for f in files}
        package = importlib.import_module("remember_me")
        modules = [module for name, module in tuple(sys.modules.items())
                   if name == "remember_me" or name.startswith("remember_me.")]
        for module in modules:
            origin = Path(module.__file__).resolve()
            if Path(module.__spec__.origin).resolve() != origin or origin not in owned:
                raise ValueError("shadow_module")
            record = owned[origin]
            if record.hash is None or record.hash.mode != "sha256":
                raise ValueError("missing_source_hash")
            digest = base64.urlsafe_b64encode(hashlib.sha256(origin.read_bytes()).digest()).decode().rstrip("=")
            if digest != record.hash.value:
                raise ValueError("changed_source")
            if hasattr(module, "__path__") and tuple(Path(p).resolve() for p in module.__path__) != (origin.parent,):
                raise ValueError("shadow_package_path")
        if package is not sys.modules.get("remember_me"):
            raise ValueError("shadow_package")
    except Exception as exc:
        raise RememberMeAdapterError("remember_me_contract_mismatch:module_source") from exc


def _checked_distribution():
    try:
        distribution = metadata.distribution(EXPECTED_DISTRIBUTION)
        if distribution.version != EXPECTED_PACKAGE_VERSION:
            raise RememberMeAdapterError("remember_me_contract_mismatch:package_version")
        _validate_archive_provenance(distribution.read_text("direct_url.json"))
        _validate_distribution_modules(distribution)
        return distribution
    except RememberMeAdapterError:
        raise
    except Exception as exc:
        raise RememberMeAdapterError("remember_me_contract_unavailable") from exc


def inspect_remember_me_contract() -> RememberMeContract:
    """Inspect package constants without creating storage or protocol runtimes."""
    try:
        distribution = _checked_distribution()
        distribution_name = distribution.metadata.get("Name", "")

        from remember_me.imaging.pillow_sanitizer import (
            PILLOW_VERSION_RANGE,
            SANITIZER_ID,
        )
        from remember_me.mcp.server import MCP_TOOL_NAMES
        from remember_me.metadata import (
            DATA_COMPATIBILITY_VERSION,
            PROJECT_VERSION,
        )

        _validate_distribution_modules(distribution)
        installed_version = distribution.version
        if PROJECT_VERSION != installed_version:
            raise RememberMeAdapterError(
                "remember_me_contract_mismatch:package_metadata"
            )
        return RememberMeContract(
            distribution_name=distribution_name,
            package_version=installed_version,
            data_compatibility=DATA_COMPATIBILITY_VERSION,
            sanitizer_id=SANITIZER_ID,
            pillow_range=PILLOW_VERSION_RANGE,
            mcp_tools=tuple(MCP_TOOL_NAMES),
        )
    except RememberMeAdapterError:
        raise
    except Exception as exc:
        raise RememberMeAdapterError(
            "remember_me_contract_unavailable"
        ) from exc


def validate_remember_me_contract(
    contract: RememberMeContract | None = None,
) -> RememberMeContract:
    """Validate every pinned host contract field and fail closed."""
    actual = contract or inspect_remember_me_contract()
    expected = RememberMeContract(
        distribution_name=EXPECTED_DISTRIBUTION,
        package_version=EXPECTED_PACKAGE_VERSION,
        data_compatibility=EXPECTED_DATA_COMPATIBILITY,
        sanitizer_id=EXPECTED_SANITIZER_ID,
        pillow_range=EXPECTED_PILLOW_RANGE,
        mcp_tools=EXPECTED_MCP_TOOLS,
    )
    for field_name in RememberMeContract.__dataclass_fields__:
        if getattr(actual, field_name) != getattr(expected, field_name):
            raise RememberMeAdapterError(
                "remember_me_contract_mismatch:{}".format(field_name)
            )
    return actual


_RUNTIME_OWNERS: dict[Path, weakref.ReferenceType] = {}
_RUNTIME_OWNERS_LOCK = threading.Lock()


class RememberMeAdapter:
    """Own at most one explicitly created LocalRuntime."""

    def __init__(self) -> None:
        self._runtime = None
        self._data_root: Path | None = None
        self._vector_provider = None

    @property
    def runtime_created(self) -> bool:
        return self._runtime is not None

    def create_runtime(self, data_root: Path, vector_provider=None):
        if not isinstance(data_root, Path):
            raise RememberMeAdapterError("remember_me_data_root_must_be_path")
        normalized_root = data_root.expanduser().resolve()
        if self._runtime is not None:
            _checked_distribution()
            if (
                normalized_root == self._data_root
                and (
                    vector_provider is None
                    or vector_provider is self._vector_provider
                )
            ):
                return self._runtime
            raise RememberMeAdapterError("remember_me_runtime_already_created")

        validate_remember_me_contract()
        with _RUNTIME_OWNERS_LOCK:
            owner_ref = _RUNTIME_OWNERS.get(normalized_root)
            owner = owner_ref() if owner_ref is not None else None
            if owner is not None and owner is not self:
                raise RememberMeAdapterError(
                    "remember_me_data_root_already_owned"
                )

            from remember_me.factory import create_local_runtime

            _checked_distribution()

            try:
                runtime = create_local_runtime(
                    normalized_root,
                    vector_provider=vector_provider,
                )
            except Exception as exc:
                raise RememberMeAdapterError(
                    "remember_me_runtime_creation_failed"
                ) from exc
            self._runtime = runtime
            self._data_root = normalized_root
            self._vector_provider = vector_provider
            _RUNTIME_OWNERS[normalized_root] = weakref.ref(self)
            return runtime


__all__ = [
    "EXPECTED_DATA_COMPATIBILITY",
    "EXPECTED_DISTRIBUTION",
    "EXPECTED_MCP_TOOLS",
    "EXPECTED_PACKAGE_VERSION",
    "EXPECTED_PILLOW_RANGE",
    "EXPECTED_SANITIZER_ID",
    "RememberMeAdapter",
    "RememberMeAdapterError",
    "RememberMeContract",
    "inspect_remember_me_contract",
    "validate_remember_me_contract",
]
