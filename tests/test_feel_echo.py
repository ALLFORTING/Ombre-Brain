import importlib
import sys
from unittest.mock import AsyncMock

import pytest


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(tmp_path / "buckets"))
    monkeypatch.setenv("OMBRE_RESPONSE_SEAL", "test-seal")
    monkeypatch.delenv("OMBRE_API_KEY", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.decay_engine.ensure_started = AsyncMock(return_value=None)
    return server


@pytest.mark.asyncio
async def test_boot_includes_one_visible_feel_echo(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    feel_id = await server.bucket_mgr.create(
        content="visible echo body needle",
        name="Visible echo",
        bucket_type="feel",
    )

    result = await server.boot()

    assert feel_id in result
    assert "visible echo body needle" in result
    assert "seal: test-seal" in result


@pytest.mark.asyncio
async def test_breath_feels_searches_feel_channel(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    feel_id = await server.bucket_mgr.create(
        content="special feel lookup needle",
        name="Feel lookup",
        bucket_type="feel",
    )
    normal_id = await server.bucket_mgr.create(
        content="special feel lookup needle",
        name="Normal lookup",
    )

    result = await server.breath(feels=True, query="special feel lookup needle")

    assert feel_id in result
    assert normal_id not in result
    assert "seal: test-seal" in result


@pytest.mark.asyncio
async def test_sealed_feel_hidden_from_echo_and_feels_search(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    sealed_id = await server.bucket_mgr.create(
        content="sealed echo lookup needle",
        name="Sealed echo",
        bucket_type="feel",
    )
    await server.trace(sealed_id, sealed=1)

    boot_result = await server.boot()
    breath_result = await server.breath(feels=True, query="sealed echo lookup needle")

    assert sealed_id not in boot_result
    assert "sealed echo lookup needle" not in boot_result
    assert sealed_id not in breath_result
    assert "Sealed echo" not in breath_result



def _echo_bucket(identity, **metadata):
    return dict(id=identity, content=f'body {identity}',
                metadata=dict(type='feel', name=identity, **metadata))


@pytest.mark.parametrize('successor', ['successor-id', 'none', '', None])
@pytest.mark.parametrize('dormant', [False, True])
def test_echo_supersession_filter_preserves_dormant(tmp_path, monkeypatch, successor, dormant):
    server = _load_server(tmp_path, monkeypatch)
    bucket = _echo_bucket('candidate', dormant=dormant)
    if successor is not None:
        bucket['metadata']['superseded_by'] = successor
    echo = server._format_feel_echo([bucket])
    if successor in ('successor-id', 'none'):
        assert echo == '=== boot: 回声 ===\n（暂无可见 feel）'
    else:
        assert '[bucket_id:candidate]' in echo and 'body candidate' in echo


def test_echo_mixed_candidates_only_select_eligible_feels(tmp_path, monkeypatch):
    server = _load_server(tmp_path, monkeypatch)
    eligible = [_echo_bucket('active'), _echo_bucket('dormant', dormant=True, superseded_by='')]
    excluded = [
        _echo_bucket('superseded', superseded_by='successor-id'),
        _echo_bucket('retired', superseded_by='none'),
        _echo_bucket('sealed', sealed=1),
        _echo_bucket('test-list', tags=['test']),
        _echo_bucket('test-csv', tags='audit, test'),
    ]
    ordinary = _echo_bucket('ordinary')
    ordinary['metadata']['type'] = 'dynamic'
    excluded.append(ordinary)
    def choose(candidates):
        assert candidates == eligible
        return candidates[1]
    monkeypatch.setattr(server.random, 'choice', choose)
    assert '[bucket_id:dormant]' in server._format_feel_echo(excluded + eligible)
    assert server._format_feel_echo(excluded) == '=== boot: 回声 ===\n（暂无可见 feel）'


@pytest.mark.asyncio
@pytest.mark.parametrize('retired', [False, True])
async def test_talk_boot_echo_excludes_superseded_feel(tmp_path, monkeypatch, retired):
    server = _load_server(tmp_path, monkeypatch)
    feel_id = await server.bucket_mgr.create('obsolete feel echo', bucket_type='feel')
    successor = 'none' if retired else await server.bucket_mgr.create('successor memory')
    await server.trace(feel_id, superseded_by=successor)
    assert (await server.bucket_mgr.get(feel_id))['metadata']['superseded_by'] == successor
    result = await server.boot(profile='talk')
    echo = result.split('=== boot: 回声 ===\n', 1)[1]
    assert '（暂无可见 feel）' in echo
    assert feel_id not in echo and 'obsolete feel echo' not in echo