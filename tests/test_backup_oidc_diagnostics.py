"""Synthetic OIDC denial diagnostics: preserve rejection and never log credentials."""
from __future__ import annotations

import json
import logging
from time import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

import backup_auto_runtime as auto
from backup_v2_oidc import GitHubActionsBackupV2OidcVerifier
from production_backup_capture import CaptureChannelError, _route_error
from tests.test_backup_auto_server import claims

SECRET = "SENSITIVE_SENTINEL_DO_NOT_LOG"
LOGGER = "ombre_brain.backup_oidc"


def request(token=SECRET, *, headers=None, body=b""):
    req = Request({"type": "http", "headers": headers if headers is not None else
                   [(b"authorization", ("Bearer " + token).encode())], "query_string": b""})
    req._body = body
    return req


def assert_safe(caplog, stage, *, exception=None, fields=None):
    records = [record for record in caplog.records if record.name == LOGGER]
    assert records
    messages = [record.getMessage() for record in records]
    assert any("stage=" + stage in message for message in messages)
    if exception:
        assert any("exception=" + exception in message for message in messages)
    if fields:
        assert any("fields=" + fields in message for message in messages)
    assert SECRET not in caplog.text
    assert "Bearer " not in caplog.text
    assert "Authorization" not in caplog.text
    assert all(record.exc_info is None and record.stack_info is None for record in records)
    assert all(len(record.args) == 3 and all(isinstance(value, str) for value in record.args)
               for record in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "duplicate", "scheme", "body"])
async def test_request_boundary_diagnostics_are_redacted(caplog, kind):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    req = request()
    stage = "request_bearer"
    if kind == "missing":
        req = request(headers=[])
    elif kind == "duplicate":
        req = request(headers=[(b"authorization", ("Bearer " + SECRET).encode())] * 2)
    elif kind == "scheme":
        req = request(headers=[(b"authorization", SECRET.encode())])
    else:
        stage = "request_body"
        req = request(headers=[(b"authorization", ("Bearer " + SECRET).encode()),
                               (b"content-type", b"application/json")],
                      body=json.dumps({"token": SECRET}).encode())
    def forbidden(*args):
        pytest.fail("invalid request reached JWT decoder")
    verifier = GitHubActionsBackupV2OidcVerifier(jwk_client=object(), decoder=forbidden)
    with pytest.raises(CaptureChannelError, match="oidc_denied"):
        await verifier.verify_request(req)
    assert_safe(caplog, stage, exception="CaptureChannelError")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,stage,exception", [
    ("factory", "jwks_client", "RuntimeError"),
    ("key", "jwks_key", "PyJWKClientConnectionError"),
    ("decoder", "jwt_decode", "RuntimeError"),
])
async def test_exception_messages_and_tracebacks_are_never_logged(caplog, kind, stage, exception):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    def fail(*args):
        raise (jwt.PyJWKClientConnectionError if kind == "key" else RuntimeError)(SECRET)
    if kind == "factory":
        verifier = GitHubActionsBackupV2OidcVerifier(jwk_client_factory=fail)
    elif kind == "decoder":
        verifier = GitHubActionsBackupV2OidcVerifier(jwk_client=object(), decoder=fail)
    else:
        # Header is structurally valid RS256; key lookup fails before verification.
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = jwt.encode({"secret": SECRET}, key, algorithm="RS256")
        verifier = GitHubActionsBackupV2OidcVerifier(
            jwk_client=SimpleNamespace(get_signing_key_from_jwt=fail))
    with pytest.raises(CaptureChannelError, match="oidc_denied"):
        await verifier.verify_request(request(token if kind == "key" else SECRET))
    assert_safe(caplog, stage, exception=exception)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed_claims():
    now = int(time())
    return {**claims(), "iss": "https://token.actions.githubusercontent.com", "iat": now,
            "nbf": now - 1, "exp": now + 60, "private_value": SECRET}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,stage,exception", [
    ("header", "jwt_header", "DecodeError"),
    ("algorithm", "jwt_algorithm", "CaptureChannelError"),
    ("aud", "jwt_claims", "InvalidAudienceError"),
    ("iss", "jwt_claims", "InvalidIssuerError"),
    ("exp", "jwt_claims", "ExpiredSignatureError"),
    ("nbf", "jwt_claims", "ImmatureSignatureError"),
    ("signature", "jwt_claims", "InvalidSignatureError"),
])
async def test_real_jwt_rejection_reports_only_stage_and_class(caplog, signing_key, kind, stage, exception):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    data = signed_claims()
    if kind in {"aud", "iss"}:
        data[kind] = SECRET
    elif kind == "exp":
        data["exp"] = int(time()) - 10
    elif kind == "nbf":
        data["nbf"] = int(time()) + 120
    token = jwt.encode(data, signing_key, algorithm="RS256")
    if kind == "header":
        token = SECRET
    elif kind == "algorithm":
        token = jwt.encode(data, SECRET * 2, algorithm="HS256")
    verification_key = signing_key if kind != "signature" else rsa.generate_private_key(
        public_exponent=65537, key_size=2048)
    verifier = GitHubActionsBackupV2OidcVerifier(audience=auto.AUTO_AUDIENCE,
        jwk_client=SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=verification_key.public_key())))
    with pytest.raises(CaptureChannelError, match="oidc_denied") as denied:
        await verifier.verify_request(request(token))
    response = _route_error(denied.value)
    assert response.status_code == 400 and response.body == b'{"status":"oidc_denied"}'
    assert_safe(caplog, stage, exception=exception)
    assert token not in caplog.text


@pytest.mark.parametrize("field", ["repository", "repository_id", "repository_owner",
    "repository_owner_id", "repository_visibility", "ref", "workflow_ref", "aud",
    "event_name", "run_id", "run_attempt", "job_workflow_ref", "environment"])
def test_policy_logs_only_fixed_mismatched_field_names(caplog, field):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    data = {**claims(), field: SECRET, "untrusted_field_" + SECRET: SECRET}
    with pytest.raises(CaptureChannelError, match="oidc_denied"):
        auto.StrictBackupAutoOidcPolicy().verify(data)
    assert_safe(caplog, "auto_policy", fields=field)
    assert "untrusted_field" not in caplog.text


def test_policy_records_all_mismatches_without_values(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    data = {**claims(), "repository_id": SECRET, "run_attempt": SECRET, "environment": SECRET}
    with pytest.raises(CaptureChannelError, match="oidc_denied"):
        auto.StrictBackupAutoOidcPolicy().verify(data)
    assert_safe(caplog, "auto_policy", fields="repository_id,run_attempt,environment")


@pytest.mark.parametrize("data", [None, [], SECRET])
def test_invalid_claim_container_is_not_logged(caplog, data):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    with pytest.raises(CaptureChannelError, match="oidc_denied"):
        auto.StrictBackupAutoOidcPolicy().verify(data)
    assert_safe(caplog, "auto_policy", fields="claims")


@pytest.mark.asyncio
@pytest.mark.parametrize("data,field", [([], "claims"), ({"aud": SECRET}, "aud")])
async def test_decoded_claim_shape_diagnostics(caplog, data, field):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    verifier = GitHubActionsBackupV2OidcVerifier(jwk_client=object(), decoder=lambda *_: data,
                                                audience=auto.AUTO_AUDIENCE)
    with pytest.raises(CaptureChannelError, match="oidc_denied"):
        await verifier.verify_request(request())
    assert_safe(caplog, "decoded_claims", fields=field)


@pytest.mark.asyncio
async def test_valid_token_and_policy_remain_accepted_without_diagnostics(caplog, signing_key):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    data = signed_claims()
    token = jwt.encode(data, signing_key, algorithm="RS256")
    verifier = GitHubActionsBackupV2OidcVerifier(audience=auto.AUTO_AUDIENCE,
        jwk_client=SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=signing_key.public_key())))
    verified = await verifier.verify_request(request(token))
    assert auto.StrictBackupAutoOidcPolicy().verify(verified) == {"run_id": "123", "run_attempt": "1"}
    assert not [record for record in caplog.records if record.name == LOGGER]


def test_metadata_http_denial_stays_generic_and_redacted(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    verifier = GitHubActionsBackupV2OidcVerifier(jwk_client=object(), audience=auto.AUTO_AUDIENCE,
        decoder=lambda *_: {**claims(), "repository_id": SECRET})
    async def metadata(req):
        try:
            auto.StrictBackupAutoOidcPolicy().verify(await verifier.verify_request(req))
        except Exception as exc:
            return _route_error(exc)
        pytest.fail("invalid identity accepted")
    with TestClient(Starlette(routes=[Route(auto.PREFIX + "/metadata", metadata)])) as client:
        response = client.get(auto.PREFIX + "/metadata", headers={"Authorization": "Bearer " + SECRET})
    assert response.status_code == 400 and response.json() == {"status": "oidc_denied"}
    assert SECRET not in response.text
    assert_safe(caplog, "auto_policy", fields="repository_id")


@pytest.mark.parametrize("event", [[], {}])
def test_diagnostics_preserve_short_circuit_denial_for_multiple_invalid_claims(caplog, event):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    data = {**claims(), "repository_id": SECRET, "event_name": event}
    with pytest.raises(CaptureChannelError, match="oidc_denied") as denied:
        auto.StrictBackupAutoOidcPolicy().verify(data)
    assert _route_error(denied.value).status_code == 400
    assert_safe(caplog, "auto_policy", fields="repository_id,event_name")
