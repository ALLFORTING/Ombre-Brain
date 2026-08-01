"""Disabled-by-default quiesced encrypted capture channel primitives."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import inspect
from typing import Any, AsyncIterator, BinaryIO, Callable, Mapping
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from maintenance_write_gate import (
    MaintenanceWriteCoordinator,
    MaintenanceWriteError,
)
from offline_backup_bundle import (
    BUNDLE_SUFFIX,
    BackupBundleError,
    CaptureAbortSignal,
    CaptureResult,
    _inventory_source,
    capture_external_source,
    load_backup_workspace,
)


V2_AUDIENCE = "ombre-brain-backup-v2"
V2_REPOSITORY = "ALLFORTING/ob-backup"
V2_REF = "refs/heads/main"
V2_WORKFLOW = ".github/workflows/backup-v2.yml"
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT_PATTERN = re.compile(r"x25519-sha256:[0-9a-f]{64}")
_RUN_ID_PATTERN = re.compile(r"[1-9][0-9]{0,19}")
_REQUEST_STATES = {
    "accepted", "draining", "capturing", "ready", "consumed", "failed", "stale",
}


class CaptureChannelError(RuntimeError):
    """Stable public capture-channel failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CaptureLimits:
    freeze_timeout_seconds: float
    max_freeze_seconds: float
    max_source_bytes: int
    max_bundle_bytes: int
    minimum_free_bytes: int = 64 * 1024 * 1024
    ready_ttl_seconds: int = 900

    def validate(self) -> None:
        values = (
            self.freeze_timeout_seconds,
            self.max_freeze_seconds,
            self.max_source_bytes,
            self.max_bundle_bytes,
            self.minimum_free_bytes,
            self.ready_ttl_seconds,
        )
        if any(isinstance(value, bool) or value <= 0 for value in values):
            raise CaptureChannelError("capture_config_invalid")


@dataclass
class CaptureJob:
    request_id: str
    oidc_run_id: str
    oidc_run_attempt: str
    runtime_commit: str
    recipient_fingerprint: str
    state: str
    created_at: str
    updated_at: str
    bundle_id: str | None = None
    bundle_name: str | None = None
    encrypted_size: int | None = None
    encrypted_sha256: str | None = None
    failure_code: str | None = None
    orphan_present: bool = False

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("bundle_name", None)
        return payload


class StrictBackupV2OidcPolicy:
    """Validate already-decoded claims without performing network I/O."""

    def __init__(
        self,
        *,
        expected_repository_id: str,
        expected_repository_owner_id: str,
    ) -> None:
        if (
            _RUN_ID_PATTERN.fullmatch(str(expected_repository_id)) is None
            or _RUN_ID_PATTERN.fullmatch(str(expected_repository_owner_id)) is None
        ):
            raise CaptureChannelError("capture_config_invalid")
        self.expected_repository_id = str(expected_repository_id)
        self.expected_repository_owner_id = str(expected_repository_owner_id)

    def verify(self, claims: Mapping[str, Any]) -> dict[str, str]:
        if not isinstance(claims, Mapping):
            raise CaptureChannelError("oidc_denied")
        repository = claims.get("repository")
        ref = claims.get("ref")
        event = claims.get("event_name")
        audience = claims.get("aud")
        workflow_ref = claims.get("workflow_ref")
        run_id = str(claims.get("run_id", ""))
        run_attempt = str(claims.get("run_attempt", ""))
        repository_owner = claims.get("repository_owner")
        if (
            repository != V2_REPOSITORY
            or ref != V2_REF
            or event != "workflow_dispatch"
            or audience != V2_AUDIENCE
            or repository_owner != "ALLFORTING"
            or not isinstance(workflow_ref, str)
            or workflow_ref != f"{V2_REPOSITORY}/{V2_WORKFLOW}@{V2_REF}"
            or _RUN_ID_PATTERN.fullmatch(run_id) is None
            or _RUN_ID_PATTERN.fullmatch(run_attempt) is None
        ):
            raise CaptureChannelError("oidc_denied")
        owner_id = str(claims.get("repository_owner_id", ""))
        repository_id = str(claims.get("repository_id", ""))
        if (
            owner_id != self.expected_repository_owner_id
            or repository_id != self.expected_repository_id
        ):
            raise CaptureChannelError("oidc_denied")
        return {"run_id": run_id, "run_attempt": run_attempt}


@dataclass
class BundleDelivery:
    request_id: str
    handle: BinaryIO
    bundle_id: str
    encrypted_size: int
    encrypted_sha256: str
    recipient_fingerprint: str


class ProductionBackupCaptureController:
    """One-process controller; construction does not register any route."""

    def __init__(
        self,
        *,
        enabled: bool,
        worker_count: int,
        coordinator: MaintenanceWriteCoordinator,
        source_root: str | Path,
        workspace_root: str | Path,
        recipient_public_key: X25519PublicKey,
        recipient_fingerprint: str,
        runtime_commit: str,
        limits: CaptureLimits,
        oidc_policy: StrictBackupV2OidcPolicy,
        clock: Callable[[], datetime] | None = None,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    ) -> None:
        if not enabled:
            raise CaptureChannelError("capture_disabled")
        if worker_count != 1:
            raise CaptureChannelError("capture_multi_worker_unsupported")
        limits.validate()
        if _COMMIT_PATTERN.fullmatch(runtime_commit or "") is None:
            raise CaptureChannelError("capture_config_invalid")
        if not isinstance(recipient_public_key, X25519PublicKey):
            raise CaptureChannelError("capture_key_invalid")
        derived = public_key_fingerprint(recipient_public_key)
        if (
            _FINGERPRINT_PATTERN.fullmatch(recipient_fingerprint or "") is None
            or not secrets.compare_digest(derived, recipient_fingerprint)
        ):
            raise CaptureChannelError("capture_key_invalid")
        self.coordinator = coordinator
        self.source_root = Path(source_root).resolve(strict=True)
        self.workspace = load_backup_workspace(workspace_root)
        for private_root in (self.workspace.temp_root, self.workspace.bundles_root):
            with suppress(OSError):
                private_root.chmod(0o700)
        self.recipient_public_key = recipient_public_key
        self.recipient_fingerprint = recipient_fingerprint
        self.runtime_commit = runtime_commit
        self.limits = limits
        self.oidc_policy = oidc_policy
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._disk_usage = disk_usage
        self._jobs: dict[str, CaptureJob] = {}
        self._parameters: dict[str, tuple[str, str, str, str]] = {}
        self._job_lock = asyncio.Lock()
        self._active_request_id: str | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._abort_signals: dict[str, CaptureAbortSignal] = {}
        self._active_deliveries: set[str] = set()
        self._active_workers = 0

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        **dependencies,
    ) -> "ProductionBackupCaptureController":
        if config.get("enabled") is not True:
            raise CaptureChannelError("capture_disabled")
        if config.get("private_key") or config.get("recipient_private_key"):
            raise CaptureChannelError("capture_key_invalid")
        public_key = parse_public_key_b64(config.get("recipient_public_key_b64"))
        return cls(
            enabled=True,
            recipient_public_key=public_key,
            recipient_fingerprint=str(config.get("recipient_fingerprint", "")),
            runtime_commit=str(config.get("runtime_commit", "")),
            worker_count=int(config.get("worker_count", 0)),
            limits=CaptureLimits(
                freeze_timeout_seconds=float(config.get("freeze_timeout_seconds", 0)),
                max_freeze_seconds=float(config.get("max_freeze_seconds", 0)),
                max_source_bytes=int(config.get("max_source_bytes", 0)),
                max_bundle_bytes=int(config.get("max_bundle_bytes", 0)),
                minimum_free_bytes=int(config.get("minimum_free_bytes", 64 * 1024 * 1024)),
                ready_ttl_seconds=int(config.get("ready_ttl_seconds", 900)),
            ),
            **dependencies,
        )

    async def create_capture(
        self,
        *,
        request_id: str,
        expected_runtime_commit: str,
        expected_recipient_fingerprint: str,
        claims: Mapping[str, Any],
    ) -> dict[str, Any]:
        canonical_id = _request_id(request_id)
        identity = self.oidc_policy.verify(claims)
        parameters = (
            expected_runtime_commit,
            expected_recipient_fingerprint,
            identity["run_id"],
            identity["run_attempt"],
        )
        if (
            expected_runtime_commit != self.runtime_commit
            or expected_recipient_fingerprint != self.recipient_fingerprint
        ):
            raise CaptureChannelError("capture_identity_mismatch")
        async with self._job_lock:
            existing = self._jobs.get(canonical_id)
            if existing is not None:
                if self._parameters[canonical_id] != parameters:
                    raise CaptureChannelError("capture_request_conflict")
                return existing.public()
            if self._active_request_id is not None:
                raise CaptureChannelError("capture_busy")
            now = self._timestamp()
            job = CaptureJob(
                request_id=canonical_id,
                oidc_run_id=identity["run_id"],
                oidc_run_attempt=identity["run_attempt"],
                runtime_commit=self.runtime_commit,
                recipient_fingerprint=self.recipient_fingerprint,
                state="accepted",
                created_at=now,
                updated_at=now,
            )
            self._jobs[canonical_id] = job
            self._parameters[canonical_id] = parameters
            self._active_request_id = canonical_id
            task = asyncio.create_task(
                self._run_capture_job(job),
                name=f"backup-capture-{canonical_id}",
            )
            self._tasks[canonical_id] = task
        return job.public()

    async def _run_capture_job(self, job: CaptureJob) -> None:
        try:
            preflight_abort = CaptureAbortSignal()
            preflight_worker = asyncio.create_task(asyncio.to_thread(
                self._preflight, preflight_abort
            ))
            self._active_workers += 1
            try:
                try:
                    await asyncio.shield(preflight_worker)
                except asyncio.CancelledError:
                    preflight_abort.abort("capture_cancelled")
                    await self._wait_worker_exit(preflight_worker)
                    raise
            finally:
                self._active_workers -= 1
            self._set_state(job, "draining")
            async with self.coordinator.freeze(
                reason="encrypted_backup_capture",
                drain_timeout_seconds=self.limits.freeze_timeout_seconds,
                max_freeze_seconds=self.limits.max_freeze_seconds,
            ) as lease:
                self._set_state(job, "capturing")
                abort_signal = CaptureAbortSignal(
                    deadline=lease.deadline,
                    monotonic=self.coordinator.monotonic,
                )
                self._abort_signals[job.request_id] = abort_signal
                worker = asyncio.create_task(asyncio.to_thread(
                    capture_external_source,
                    self.workspace.root,
                    self.source_root,
                    self.source_root,
                    self.recipient_public_key,
                    coordinator=self.coordinator,
                    freeze_lease=lease,
                    ob_commit_sha=self.runtime_commit,
                    abort_signal=abort_signal,
                ))
                self._active_workers += 1
                try:
                    remaining = max(0.0, lease.deadline - self.coordinator.monotonic())
                    try:
                        result = await asyncio.wait_for(
                            asyncio.shield(worker), timeout=remaining
                        )
                    except asyncio.TimeoutError as exc:
                        abort_signal.abort("freeze_lease_expired")
                        await self._wait_worker_exit(worker)
                        raise CaptureChannelError("freeze_lease_expired") from exc
                    except asyncio.CancelledError:
                        abort_signal.abort("capture_cancelled")
                        await self._wait_worker_exit(worker)
                        raise
                    self._record_owned_bundle(job, result)
                    self.coordinator.validate_lease(lease)
                finally:
                    self._active_workers -= 1
                    self._abort_signals.pop(job.request_id, None)
            self._finish_bundle(job)
        except asyncio.CancelledError:
            await self._fail_and_cleanup(job, "capture_cancelled")
        except MaintenanceWriteError as exc:
            await self._fail_and_cleanup(job, exc.code)
        except BackupBundleError as exc:
            await self._fail_and_cleanup(job, exc.status)
        except CaptureChannelError as exc:
            await self._fail_and_cleanup(job, exc.code)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                await self._fail_and_cleanup(job, "internal_error")
                raise
            await self._fail_and_cleanup(job, "internal_error")
        finally:
            async with self._job_lock:
                if self._active_request_id == job.request_id:
                    self._active_request_id = None

    async def _wait_worker_exit(self, worker: asyncio.Task[CaptureResult]) -> None:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done():
            with suppress(Exception, asyncio.CancelledError):
                worker.result()

    async def wait_for_terminal(
        self,
        request_id: str,
        claims: Mapping[str, Any],
        *,
        timeout: float = 30,
    ) -> dict[str, Any]:
        identity = self.oidc_policy.verify(claims)
        job = self._owned_job(request_id, identity)
        task = self._tasks.get(job.request_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        return job.public()

    def get_job(self, request_id: str, claims: Mapping[str, Any]) -> dict[str, Any]:
        identity = self.oidc_policy.verify(claims)
        job = self._owned_job(request_id, identity)
        return job.public()

    @asynccontextmanager
    async def delivery(
        self,
        request_id: str,
        claims: Mapping[str, Any],
    ) -> AsyncIterator[BundleDelivery]:
        identity = self.oidc_policy.verify(claims)
        canonical_id = _request_id(request_id)
        handle = None
        async with self._job_lock:
            job = self._owned_job(canonical_id, identity)
            if job.state != "ready" or not job.bundle_name:
                raise CaptureChannelError("capture_not_ready")
            if canonical_id in self._active_deliveries:
                raise CaptureChannelError("capture_delivery_active")
            if self.coordinator.status().state != "open":
                raise CaptureChannelError("maintenance_in_progress")
            try:
                path = (self.workspace.bundles_root / job.bundle_name).resolve(strict=True)
                if path.parent != self.workspace.bundles_root or path.suffix != BUNDLE_SUFFIX:
                    raise CaptureChannelError("bundle_invalid")
                handle = path.open("rb")
            except (OSError, RuntimeError) as exc:
                raise CaptureChannelError("bundle_invalid") from exc
            self._active_deliveries.add(canonical_id)
        try:
            size, digest = await asyncio.to_thread(_hash_handle, handle)
            handle.seek(0)
            if size != job.encrypted_size or digest != job.encrypted_sha256:
                raise CaptureChannelError("bundle_invalid")
            yield BundleDelivery(
                request_id=canonical_id,
                handle=handle,
                bundle_id=str(job.bundle_id),
                encrypted_size=int(job.encrypted_size),
                encrypted_sha256=str(job.encrypted_sha256),
                recipient_fingerprint=job.recipient_fingerprint,
            )
        finally:
            if handle is not None:
                handle.close()
            async with self._job_lock:
                self._active_deliveries.discard(canonical_id)

    async def acknowledge(self, request_id: str, claims: Mapping[str, Any]) -> dict[str, Any]:
        identity = self.oidc_policy.verify(claims)
        async with self._job_lock:
            job = self._owned_job(request_id, identity)
            if job.state != "ready" or not job.bundle_name:
                raise CaptureChannelError("capture_not_ready")
            if job.request_id in self._active_deliveries:
                raise CaptureChannelError("capture_delivery_active")
            path = self.workspace.bundles_root / job.bundle_name
            try:
                path.unlink()
            except OSError as exc:
                raise CaptureChannelError("internal_error") from exc
            job.bundle_name = None
            self._set_state(job, "consumed")
            return job.public()

    async def cleanup_stale(self) -> int:
        now = self._now()
        cleaned = 0
        async with self._job_lock:
            for job in tuple(self._jobs.values()):
                if (
                    job.state != "ready"
                    or not job.bundle_name
                    or job.request_id in self._active_deliveries
                ):
                    continue
                updated = datetime.fromisoformat(job.updated_at)
                if now - updated < timedelta(seconds=self.limits.ready_ttl_seconds):
                    continue
                path = self.workspace.bundles_root / job.bundle_name
                try:
                    if path.parent == self.workspace.bundles_root:
                        path.unlink(missing_ok=True)
                except OSError as exc:
                    raise CaptureChannelError("internal_error") from exc
                job.bundle_name = None
                self._set_state(job, "stale")
                cleaned += 1
        return cleaned

    def _preflight(self, abort_signal: CaptureAbortSignal | None = None) -> None:
        inventory = _inventory_source(
            self.source_root,
            chunk_size=1024 * 1024,
            abort_signal=abort_signal,
        )
        source_bytes = sum(
            record.size_bytes or 0
            for record in inventory.records
            if record.item_type in {"regular", "sqlite", "sqlite_content_sidecar"}
        )
        if source_bytes > self.limits.max_source_bytes:
            raise CaptureChannelError("capture_source_too_large")
        required = source_bytes * 3 + self.limits.minimum_free_bytes
        if self._disk_usage(self.workspace.temp_root).free < required:
            raise CaptureChannelError("capture_space_insufficient")

    def _record_owned_bundle(self, job: CaptureJob, result: CaptureResult) -> None:
        job.bundle_id = result.bundle_id
        job.bundle_name = result.bundle_name

    def _finish_bundle(self, job: CaptureJob) -> None:
        if not job.bundle_name:
            raise CaptureChannelError("internal_error")
        path = self.workspace.bundles_root / job.bundle_name
        size, digest = _hash_file(path)
        if size > self.limits.max_bundle_bytes:
            raise CaptureChannelError("capture_bundle_too_large")
        try:
            path.chmod(0o600)
        except OSError as exc:
            raise CaptureChannelError("internal_error") from exc
        job.encrypted_size = size
        job.encrypted_sha256 = digest
        self._set_state(job, "ready")

    async def _fail_and_cleanup(self, job: CaptureJob, code: str) -> None:
        final_code = code
        if job.bundle_name:
            path = self.workspace.bundles_root / job.bundle_name
            try:
                if path.parent != self.workspace.bundles_root:
                    raise OSError
                path.unlink(missing_ok=True)
                job.bundle_name = None
                job.orphan_present = False
            except OSError:
                job.orphan_present = True
                final_code = "internal_error"
        self._fail(job, final_code)

    def _owned_job(self, request_id: str, identity: Mapping[str, str]) -> CaptureJob:
        job = self._jobs.get(_request_id(request_id))
        if (
            job is None
            or job.oidc_run_id != identity["run_id"]
            or job.oidc_run_attempt != identity["run_attempt"]
        ):
            raise CaptureChannelError("capture_not_found")
        return job

    def _set_state(self, job: CaptureJob, state: str) -> None:
        if state not in _REQUEST_STATES:
            raise CaptureChannelError("internal_error")
        job.state = state
        job.updated_at = self._timestamp()

    def _fail(self, job: CaptureJob, code: str) -> None:
        job.failure_code = code if re.fullmatch(r"[a-z0-9_]{1,64}", code or "") else "internal_error"
        self._set_state(job, "failed")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="seconds")


def build_backup_v2_routes(
    controller: ProductionBackupCaptureController,
    claim_verifier: Callable[[Request], Any],
) -> list[Route]:
    """Build unregistered routes; callers must explicitly mount them later."""

    async def create(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {
                "request_id", "expected_runtime_commit", "expected_recipient_fingerprint"
            }:
                raise CaptureChannelError("request_invalid")
            result = await controller.create_capture(
                request_id=body["request_id"],
                expected_runtime_commit=body["expected_runtime_commit"],
                expected_recipient_fingerprint=body["expected_recipient_fingerprint"],
                claims=await _verify_request_claims(claim_verifier, request),
            )
            return _json(result, 202)
        except Exception as exc:
            return _route_error(exc)

    async def status(request: Request):
        try:
            return _json(controller.get_job(
                request.path_params["request_id"],
                await _verify_request_claims(claim_verifier, request),
            ))
        except Exception as exc:
            return _route_error(exc)

    async def download(request: Request):
        try:
            claims = await _verify_request_claims(claim_verifier, request)
            delivery_context = controller.delivery(
                request.path_params["request_id"], claims
            )
            delivery = await delivery_context.__aenter__()

            async def body() -> AsyncIterator[bytes]:
                digest = hashlib.sha256()
                size = 0
                try:
                    while block := delivery.handle.read(1024 * 1024):
                        size += len(block)
                        digest.update(block)
                        yield block
                    if (
                        size != delivery.encrypted_size
                        or digest.hexdigest() != delivery.encrypted_sha256
                    ):
                        raise CaptureChannelError("bundle_invalid")
                finally:
                    await delivery_context.__aexit__(None, None, None)

            return StreamingResponse(
                body(),
                media_type="application/octet-stream",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="{delivery.bundle_id}.obbackup"',
                    "Content-Length": str(delivery.encrypted_size),
                    "X-Backup-Bundle-Id": delivery.bundle_id,
                    "X-Backup-SHA256": delivery.encrypted_sha256,
                    "X-Backup-Recipient-Fingerprint": delivery.recipient_fingerprint,
                },
            )
        except Exception as exc:
            return _route_error(exc)

    async def acknowledge(request: Request):
        try:
            return _json(await controller.acknowledge(
                request.path_params["request_id"],
                await _verify_request_claims(claim_verifier, request),
            ))
        except Exception as exc:
            return _route_error(exc)

    return [
        Route("/api/backup/v2/captures", create, methods=["POST"]),
        Route("/api/backup/v2/captures/{request_id}", status, methods=["GET"]),
        Route("/api/backup/v2/captures/{request_id}/bundle", download, methods=["GET"]),
        Route("/api/backup/v2/captures/{request_id}/ack", acknowledge, methods=["POST"]),
    ]


async def _verify_request_claims(claim_verifier, request: Request) -> Mapping[str, Any]:
    result = claim_verifier(request)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Mapping):
        raise CaptureChannelError("oidc_denied")
    return result


async def receive_encrypted_bundle(
    chunks: AsyncIterator[bytes],
    destination: str | Path,
    *,
    expected_size: int,
    expected_sha256: str,
    maximum_bytes: int,
) -> Path:
    target = Path(destination)
    if target.exists() or target.suffix != BUNDLE_SUFFIX:
        raise CaptureChannelError("transport_target_invalid")
    part = target.with_name(f".{target.name}.{secrets.token_hex(8)}.part")
    digest = hashlib.sha256()
    size = 0
    try:
        with part.open("xb") as writer:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise CaptureChannelError("transport_invalid")
                size += len(chunk)
                if size > maximum_bytes:
                    raise CaptureChannelError("transport_too_large")
                writer.write(chunk)
                digest.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise CaptureChannelError("transport_integrity_failed")
        try:
            os.link(part, target)
        except FileExistsError as exc:
            raise CaptureChannelError("transport_target_invalid") from exc
        with target.open("r+b") as handle:
            os.fsync(handle.fileno())
        return target
    finally:
        part.unlink(missing_ok=True)


def parse_public_key_b64(value: Any) -> X25519PublicKey:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise CaptureChannelError("capture_key_invalid")
    try:
        raw = base64.b64decode(value, validate=True)
        if base64.b64encode(raw).decode("ascii") != value or len(raw) != 32:
            raise ValueError
        return X25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise CaptureChannelError("capture_key_invalid") from exc


def public_key_fingerprint(key: X25519PublicKey) -> str:
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "x25519-sha256:" + hashlib.sha256(raw).hexdigest()


def _request_id(value: Any) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CaptureChannelError("request_invalid") from exc
    canonical = str(parsed)
    if str(value) != canonical:
        raise CaptureChannelError("request_invalid")
    return canonical


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                size += len(block)
                digest.update(block)
    except OSError as exc:
        raise CaptureChannelError("bundle_invalid") from exc
    return size, digest.hexdigest()


def _hash_handle(handle: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    handle.seek(0)
    while block := handle.read(1024 * 1024):
        size += len(block)
        digest.update(block)
    return size, digest.hexdigest()


def _json(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


def _route_error(exc: Exception) -> JSONResponse:
    code = exc.code if isinstance(exc, CaptureChannelError) else "internal_error"
    status = 503 if code == "maintenance_in_progress" else 400
    return _json({"status": code}, status)
