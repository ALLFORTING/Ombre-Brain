"""Upload-boundary contract tests for O5B."""

from raw_evidence_import import parse_capture_option
from tests._server_source import effective_server_source


def test_upload_route_keeps_additive_capture_contract():
    source = effective_server_source()
    route = source[source.index('"/api/import/upload"'):]
    assert "raw_evidence_capture" in route
    assert "start_raw_evidence" in route
    assert 'raw_bytes.decode("utf-8", errors="replace")' in route
    assert "len(raw_bytes) if raw_evidence_capture" in route


def test_capture_option_absent_and_explicit_zero_are_off():
    assert parse_capture_option(None) is False
    assert parse_capture_option("0") is False
    assert parse_capture_option("1") is True
