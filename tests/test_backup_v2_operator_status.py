from __future__ import annotations

import base64
import copy
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import backup_v2_runtime
from maintenance_write_gate import DEFAULT_WRITE_COORDINATOR, MaintenanceWriteCoordinator
from production_backup_capture import CaptureJob
from tests.test_stage8h_g1d_backup_v2_runtime import _server, _key_env, COMMIT


TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
REQUEST_ID = "12345678-1234-1234-1234-123456789abc"
QUERY = "?original_run_id=123&original_run_attempt=1"
PATH = "/api/backup/v2/operator-status/" + REQUEST_ID
AUTH = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def registered(tmp_path, monkeypatch):
    import server
    monkeypatch.setenv("OMBRE_BACKUP_V2_STATUS_TOKEN", TOKEN)
    monkeypatch.setattr(server, "_backup_v2_status_rate_limiter", server._BackupV2StatusRateLimiter())
    module = _server(tmp_path)
    module._require_backup_v2_status_auth = server._require_backup_v2_status_auth
    env = _key_env(tmp_path)
    backup_v2_runtime.register_backup_v2_if_enabled(module, "streamable-http", environ=env)
    controller = module._backup_v2_controller
    job = CaptureJob(REQUEST_ID, "123", "1", COMMIT, controller.recipient_fingerprint,
                     "accepted", "2026-10-05T00:00:00+00:00", "2026-10-05T00:00:00+00:00")
    with controller._status_lock:
        controller._jobs[REQUEST_ID] = job
    # Use the actual registered endpoints/methods, including original routes.
    from starlette.routing import Route
    routes = []
    for route in module.mcp._custom_starlette_routes:
        mounted = Route(route.path, route.endpoint, methods=route.methods)
        if hasattr(route, "handle"):
            mounted.handle = route.handle
        routes.append(mounted)
    app = Starlette(routes=routes)
    with TestClient(app) as client:
        yield module, controller, job, env, client


def test_matched_schema_and_identity_hiding(registered):
    module, controller, job, env, client = registered
    response = client.get(PATH + QUERY, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert set(data) == {"schema_version", "status", "runtime_commit", "observed_at",
                         "snapshot_consistent", "controller_busy", "job_lookup", "job",
                         "coordinator", "original_job_lease_release"}
    assert data["job"] == job.public() and len(data["job"]) == 13
    assert data["job_lookup"] == "matched"
    assert data["original_job_lease_release"] == "unknown"
    assert data["snapshot_consistent"] is True
    assert data["runtime_commit"] == COMMIT
    assert data["controller_busy"] is False
    assert data["observed_at"].endswith("+00:00")
    assert len(data["observed_at"].split(".")[1].split("+")[0]) == 6
    assert set(data["coordinator"]) == {"state", "active_writers", "generation", "lease_present",
                                       "freeze_started_at", "freeze_deadline", "freeze_reason"}
    mismatch = client.get(PATH + "?original_run_id=124&original_run_attempt=1", headers=AUTH).json()
    missing = client.get(PATH.replace(REQUEST_ID, str(uuid.uuid4())) + QUERY, headers=AUTH).json()
    assert mismatch["job_lookup"] == missing["job_lookup"] == "not_found"
    assert mismatch["job"] is missing["job"] is None
    assert mismatch["coordinator"] == missing["coordinator"]


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer " + TOKEN + " "},
    {"Authorization": "Bearer  " + TOKEN}, {"Authorization": "Bearer " + TOKEN + ", Bearer " + TOKEN},
    {"Authorization": "Basic " + TOKEN}, {"Authorization": "Bearer " + "A" * 43},
    [("Authorization", "Bearer " + TOKEN), ("authorization", "Bearer " + TOKEN)],
])
def test_auth_rejects_raw_duplicates_and_bad_credentials(registered, headers):
    response = registered[-1].get(PATH + QUERY, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"status": "unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("configured", [None, "", " " + TOKEN, TOKEN + "=", "!" * 43, "A" * 42 + "B"])
def test_unconfigured_or_noncanonical_token_unavailable(registered, monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("OMBRE_BACKUP_V2_STATUS_TOKEN")
    else:
        monkeypatch.setenv("OMBRE_BACKUP_V2_STATUS_TOKEN", configured)
    response = registered[-1].get(PATH + QUERY, headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"status": "status_unavailable"}


def test_auth_is_purpose_limited_and_not_from_cookie_query_body(registered, monkeypatch):
    import server
    monkeypatch.setenv("OMBRE_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("OMBRE_API_KEY", TOKEN)
    client = registered[-1]
    response = client.get(PATH + QUERY, headers={"Authorization": "bEaReR " + TOKEN})
    assert response.status_code == 200
    response = client.request("GET", PATH + QUERY + "&token=" + TOKEN,
                              headers={"Cookie": "token=" + TOKEN}, content=TOKEN)
    assert response.status_code == 401
    monkeypatch.setattr(server, "_backup_v2_status_rate_limiter", server._BackupV2StatusRateLimiter())
    for method, path in [("POST", "/api/backup/v2/captures"),
                         ("GET", "/api/backup/v2/captures/" + REQUEST_ID + "/bundle"),
                         ("POST", "/api/backup/v2/captures/" + REQUEST_ID + "/ack")]:
        response = client.request(method, path, headers=AUTH, json={
            "request_id": REQUEST_ID, "expected_runtime_commit": COMMIT,
            "expected_recipient_fingerprint": registered[1].recipient_fingerprint})
        assert response.status_code != 200 and response.status_code != 202
        assert response.json()["status"] == "oidc_denied"


@pytest.mark.parametrize("path,query", [
    (PATH, ""), (PATH, "?original_run_id=123"),
    (PATH, QUERY + "&original_run_id=123"), (PATH, QUERY + "&extra=1"),
    (PATH, QUERY + "&request_id=" + REQUEST_ID),
    (PATH, "?original_run_id=01&original_run_attempt=1"),
    (PATH, "?original_run_id=123&original_run_attempt=0"),
    (PATH, "?original_run_id=123&original_run_attempt=%201"),
    (PATH, "?original_run_id=123&original_run_attempt=%EF%BC%91"),
    (PATH, "?original_run_id=" + "9" * 21 + "&original_run_attempt=1"),
    (PATH.replace(REQUEST_ID, REQUEST_ID.upper()), QUERY), (PATH.replace(REQUEST_ID, "invalid"), QUERY),
])
def test_strict_parameters(registered, path, query):
    # Preserve the route's case, change only UUID for the uppercase case.
    path = path.replace("/API/BACKUP/V2/OPERATOR-STATUS/", "/api/backup/v2/operator-status/")
    response = registered[-1].get(path + query, headers=AUTH)
    assert response.status_code == 400
    assert response.json() == {"status": "request_invalid"}


def test_rate_limit_shared_all_attempts_monotonic(registered, monkeypatch):
    import server
    now = [10.0]
    limiter = server._BackupV2StatusRateLimiter(monotonic=lambda: now[0])
    monkeypatch.setattr(server, "_backup_v2_status_rate_limiter", limiter)
    client = registered[-1]
    for _ in range(5):
        assert client.get(PATH + QUERY).status_code == 401
    response = client.get(PATH + QUERY, headers=AUTH)
    assert response.status_code == 429
    assert response.json() == {"status": "rate_limited"}
    assert response.headers["retry-after"] == "2"
    now[0] += 1.1
    assert client.get(PATH + QUERY).headers["retry-after"] == "1"
    now[0] += .9
    assert client.get(PATH + QUERY, headers=AUTH).status_code == 200


@pytest.mark.parametrize("failure", ["missing", "lazy", "split", "commit", "replacement"])
def test_unavailable_does_not_initialize_or_return_snapshot(registered, failure):
    module, controller, job, env, client = registered
    if failure == "missing":
        del module._backup_v2_controller
    elif failure == "lazy":
        module._runtime_components = None
        module._get_runtime_components = lambda: pytest.fail("initialization reached")
    elif failure == "split":
        module.asset_store.write_coordinator = MaintenanceWriteCoordinator()
    elif failure == "commit":
        env["RENDER_GIT_COMMIT"] = "2" * 40
    else:
        module._backup_v2_controller = object()
    response = client.get(PATH + QUERY, headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"status": "status_unavailable"}


def test_snapshot_conflict_and_unexpected_error_redacted(registered, monkeypatch):
    module, controller, job, env, client = registered
    coordinator = controller.coordinator
    with coordinator._condition:
        previous = coordinator._state
        coordinator._state = "frozen"
    try:
        response = client.get(PATH + QUERY, headers=AUTH)
        assert response.status_code == 409 and response.json() == {"status": "snapshot_conflict"}
    finally:
        with coordinator._condition:
            coordinator._state = previous
    async def broken(*args, **kwargs):
        raise RuntimeError("private-detail")
    monkeypatch.setattr(controller, "operator_status", broken)
    response = client.get(PATH + QUERY, headers=AUTH)
    assert response.status_code == 500 and response.json() == {"status": "internal_error"}


def test_query_zero_side_effects_and_restart_not_found(registered, monkeypatch):
    module, controller, job, env, client = registered
    before_job = copy.deepcopy(job.public())
    before_coordinator = controller.coordinator.status()
    before_files = sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                          for p in controller.workspace.root.rglob("*"))
    def forbidden(*args, **kwargs):
        raise AssertionError("side effect reached")
    for name in ("_preflight", "cleanup_stale", "_finish_bundle", "_fail_and_cleanup"):
        monkeypatch.setattr(controller, name, forbidden)
    monkeypatch.setattr(controller.coordinator, "validate_lease", forbidden)
    monkeypatch.setattr(controller.coordinator, "_release_lease", forbidden)
    monkeypatch.setattr(module, "_get_runtime_components", forbidden, raising=False)
    assert client.get(PATH + QUERY, headers=AUTH).status_code == 200
    assert job.public() == before_job and controller.coordinator.status() == before_coordinator
    assert before_files == sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                                  for p in controller.workspace.root.rglob("*"))
    with controller._status_lock:
        controller._jobs.clear()  # Model the new process's empty in-memory job table.
    data = client.get(PATH + QUERY, headers=AUTH).json()
    assert data["job_lookup"] == "not_found" and data["job"] is None
    assert data["coordinator"]["state"] == "open"
    assert data["original_job_lease_release"] == "unknown"
    response = client.head(PATH + QUERY, headers=AUTH)
    assert response.status_code == 405 and not response.content
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method", ["HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "CUSTOM"])
def test_only_get_has_status_and_rejections_are_no_store(registered, method):
    response = registered[-1].request(method, PATH + QUERY, headers=AUTH)
    assert response.status_code == 405
    assert response.headers["cache-control"] == "no-store"
    if method != "HEAD":
        assert response.json() == {"status": "method_not_allowed"}
    denied = registered[-1].request(method, PATH + QUERY)
    assert denied.status_code == 401


def test_real_fastmcp_registration_preserves_operator_rejection_handler(registered):
    from mcp.server.fastmcp import FastMCP
    module, controller, job, env, client = registered
    module.mcp = FastMCP("synthetic-status", stateless_http=True)
    backup_v2_runtime.register_backup_v2_if_enabled(module, "streamable-http", environ=env)
    assert len(module.mcp._custom_starlette_routes) == 5
    backup_v2_runtime.register_backup_v2_if_enabled(module, "streamable-http", environ=env)
    assert len(module.mcp._custom_starlette_routes) == 5
    with TestClient(module.mcp.streamable_http_app()) as real_client:
        response = real_client.get(PATH + QUERY, headers=AUTH)
        assert response.status_code == 200 and response.json()["job_lookup"] == "not_found"
        response = real_client.post(PATH + QUERY, headers=AUTH)
        assert response.status_code == 405
        assert response.json() == {"status": "method_not_allowed"}
        assert response.headers["cache-control"] == "no-store"



@pytest.fixture
def production_http(registered, monkeypatch):
    import server
    from mcp.server.fastmcp import FastMCP
    module, controller, job, env, client = registered
    monkeypatch.setenv("OMBRE_HTTP_ALLOWED_ORIGINS", "https://synthetic.invalid")
    monkeypatch.setattr(server, "_backup_v2_status_rate_limiter",
                        server._BackupV2StatusRateLimiter(monotonic=lambda: 10.0))
    module.mcp = FastMCP("synthetic-production-status", stateless_http=True)
    backup_v2_runtime.register_backup_v2_if_enabled(module, "streamable-http", environ=env)
    # Same assembly entry point used by __main__, on the real FastMCP app.
    monkeypatch.setattr(server, "mcp", module.mcp)
    app = server.add_http_transport_middleware(server.build_streamable_http_app())
    assert module.mcp.settings.stateless_http is True
    with TestClient(app) as real_client:
        yield real_client


PREFLIGHT = {"Origin": "https://synthetic.invalid",
             "Access-Control-Request-Method": "GET"}


def test_production_preflight_auth_and_shared_limit(production_http):
    client = production_http
    for _ in range(5):
        response = client.options(PATH + QUERY, headers=PREFLIGHT)
        assert response.status_code == 401
        assert response.json() == {"status": "unauthorized"}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["www-authenticate"] == "Bearer"
    # Both preflight and GET share the same bucket, including rejected attempts.
    for method, headers in [("OPTIONS", PREFLIGHT), ("GET", AUTH)]:
        response = client.request(method, PATH + QUERY, headers=headers)
        assert response.status_code == 429
        assert response.json() == {"status": "rate_limited"}
        assert response.headers["retry-after"] == "2"
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method", ["OPTIONS", "HEAD", "POST"])
def test_production_authenticated_non_get(production_http, method):
    response = production_http.request(method, PATH + QUERY, headers={**PREFLIGHT, **AUTH})
    assert response.status_code == 405
    assert response.headers["cache-control"] == "no-store"
    if method == "HEAD":
        assert not response.content
    else:
        assert response.json() == {"status": "method_not_allowed"}


def test_production_get_charged_once_and_other_cors_unchanged(production_http):
    client = production_http
    # Five successful snapshots demonstrate that entry and route do not double-charge.
    for _ in range(5):
        response = client.get(PATH + QUERY, headers={"Origin": PREFLIGHT["Origin"], **AUTH})
        assert response.status_code == 200
        assert response.json()["job_lookup"] == "not_found"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["access-control-allow-origin"] == PREFLIGHT["Origin"]
    assert client.get(PATH + QUERY, headers=AUTH).status_code == 429
    # Even with the status bucket exhausted, the original MCP CORS preflight passes.
    response = client.options("/mcp", headers=PREFLIGHT)
    assert response.status_code == 200 and response.text == "OK"
    assert response.headers["access-control-allow-origin"] == PREFLIGHT["Origin"]
    assert "GET" in response.headers["access-control-allow-methods"]
    # A neighboring path must also retain normal CORS, rather than status auth.
    assert client.options("/api/backup/v2/captures", headers=PREFLIGHT).status_code == 200


def test_production_preflight_rejects_raw_duplicate_authorization(production_http):
    headers = list(PREFLIGHT.items()) + [("Authorization", AUTH["Authorization"]),
                                       ("authorization", AUTH["Authorization"])]
    response = production_http.options(PATH + QUERY, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"status": "unauthorized"}
    assert response.headers["cache-control"] == "no-store"

def test_published_runtime_resolves_only_lazy_proxies_and_checks_overrides(registered):
    import server
    module, controller, job, env, client = registered
    names = ("bucket_mgr", "asset_store", "embedding_engine", "asset_embedding_index",
             "dehydrator", "asset_backend_registry", "decay_engine", "import_engine",
             "remember_me_host_bundle")
    components = {name: vars(module)[name] for name in names}
    module._runtime_components = components
    module._LazyRuntimeComponent = server._LazyRuntimeComponent
    for name in names:
        if name == "remember_me_host_bundle":
            delattr(module, name)
        else:
            setattr(module, name, server._LazyRuntimeComponent(name))
    assert client.get(PATH + QUERY, headers=AUTH).status_code == 200
    from types import SimpleNamespace
    module.asset_store = SimpleNamespace(write_coordinator=MaintenanceWriteCoordinator())
    response = client.get(PATH + QUERY, headers=AUTH)
    assert response.status_code == 503 and response.json() == {"status": "status_unavailable"}


def test_run_identity_twenty_digit_boundary(registered):
    module, controller, job, env, client = registered
    maximum = "9" * 20
    with controller._status_lock:
        job.oidc_run_id = maximum
        job.oidc_run_attempt = maximum
    response = client.get(PATH + "?original_run_id=" + maximum + "&original_run_attempt=" + maximum,
                          headers=AUTH)
    assert response.status_code == 200 and response.json()["job_lookup"] == "matched"
