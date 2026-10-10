"""Write receipts remind the writer to verify quoted text is verbatim."""
import copy
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

import server
from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine

pytestmark = pytest.mark.asyncio
ANALYSIS = dict(domain=['work'], tags=['auto'], valence=.6, arousal=.4,
                suggested_name='entry', todos=[], importance=5)
QUOTED = '婷说「明天提醒我」，又说“好吧”。'
PLAIN = '婷让我明天提醒她买湿纸巾，这是一段没有引号的正文。'


def reminder(count):
    return f'正文有 {count} 处引号（「」或 “”）。请确认是不是逐字原话；不是就去掉引号。'


def items(*contents):
    return [dict(content=content, name=f'item{i}', **{k: v for k, v in ANALYSIS.items() if k != 'suggested_name'})
            for i, content in enumerate(contents)]


@pytest_asyncio.fixture
async def manager(test_config, monkeypatch):
    monkeypatch.setenv('OMBRE_BUCKETS_DIR', test_config['buckets_dir'])
    for name in ('bucket_mgr', 'embedding_engine', 'config', 'decay_engine', 'dehydrator',
                 '_similarity_doorbell', '_detect_conflict_warning'):
        monkeypatch.setattr(server, name, getattr(server, name))
    manager = BucketManager(test_config)
    engine = EmbeddingEngine(test_config)
    manager.embedding_engine = engine
    engine._generate_embedding = AsyncMock(return_value=[.25, .75])
    engine.search_similar = AsyncMock(return_value=[])
    server.bucket_mgr, server.embedding_engine, server.config = manager, engine, test_config
    server.decay_engine = Mock(ensure_started=AsyncMock())
    server.dehydrator = Mock(analyze=AsyncMock(return_value=copy.deepcopy(ANALYSIS)),
                             digest=AsyncMock(return_value=items(PLAIN)))
    server._similarity_doorbell = AsyncMock(return_value='')
    server._detect_conflict_warning = AsyncMock(return_value='')
    return manager


def assert_no_reminder(result):
    assert '正文有' not in result and '逐字原话' not in result


async def test_count_only_paired_quotes():
    assert server._quote_reminder_line(PLAIN) == ''
    assert server._quote_reminder_line(QUOTED) == reminder(2)
    assert server._quote_reminder_line('「一」「二」“三”', '“四”') == reminder(4)
    assert server._quote_reminder_line('只有半个「没闭合', '”反向“') == ''
    assert server._quote_reminder_line('「跨\n行」') == reminder(1)


@pytest.mark.parametrize('operation_id', [None, 'hold-op'])
async def test_hold_reminder(manager, operation_id):
    quoted = await server.hold(QUOTED, operation_id=operation_id)
    assert quoted.endswith('\n' + reminder(2))
    plain = await server.hold(PLAIN, operation_id=None if operation_id is None else 'hold-plain')
    assert_no_reminder(plain)
    if operation_id:
        assert await server.hold(QUOTED, operation_id=operation_id) == quoted


@pytest.mark.parametrize('kwargs', [dict(feel=True), dict(pinned=True)])
async def test_hold_feel_and_pinned_reminder(manager, kwargs):
    assert (await server.hold(QUOTED, **kwargs)).endswith('\n' + reminder(2))


async def test_hold_supersedes_reminder(manager):
    target = await manager.create(content=PLAIN, tags=[], domain=['work'])
    result = await server.hold('改成「原话」', supersedes_id=target)
    assert result == f'fact evolved in place: {target}\n' + reminder(1)


async def test_hold_rejected_has_no_reminder(manager):
    assert_no_reminder(await server.hold(QUOTED, importance=11))
    assert_no_reminder(await server.hold(QUOTED, operation_id='bad', supersedes_id='x'))


@pytest.mark.parametrize('operation_id', [None, 'grow-op'])
async def test_grow_short_reminder(manager, operation_id):
    result = await server.grow('她说「好」', operation_id=operation_id)
    assert result.endswith('\n' + reminder(1))
    assert_no_reminder(await server.grow('短句没有引号', operation_id=operation_id and 'grow-plain'))


@pytest.mark.parametrize('operation_id', [None, 'grow-digest'])
async def test_grow_digest_reminder_counts_input(manager, operation_id):
    diary = QUOTED + PLAIN + '最后她说「行」。'
    result = await server.grow(diary, operation_id=operation_id)
    assert result.endswith('\n' + reminder(3))
    if operation_id:
        assert await server.grow(diary, operation_id=operation_id) == result


async def test_grow_digest_failure_has_no_reminder(manager):
    server.dehydrator.digest = AsyncMock(return_value=[])
    assert_no_reminder(await server.grow(QUOTED + PLAIN))
    assert_no_reminder(await server.grow(QUOTED + PLAIN, operation_id='grow-fail'))


@pytest.mark.parametrize('operation_id', [None, 'trace-op'])
async def test_trace_replace_reminder(manager, operation_id):
    bucket = await manager.create(content=PLAIN, tags=[], domain=['work'])
    result = await server.trace(bucket, content=QUOTED, operation_id=operation_id)
    assert 'content=已替换' in result and result.endswith('\n' + reminder(2))
    if operation_id:
        assert await server.trace(bucket, content=QUOTED, operation_id=operation_id) == result
    assert_no_reminder(await server.trace(bucket, content=PLAIN,
                                          operation_id=operation_id and 'trace-plain'))


@pytest.mark.parametrize('operation_id', [None, 'append-op'])
async def test_trace_append_counts_only_new_segment(manager, operation_id):
    bucket = await manager.create(content='旧正文「一」「二」“三”', tags=[], domain=['work'])
    plain = await server.trace(bucket, content=PLAIN, append=True, operation_id=operation_id)
    assert 'content=已追加' in plain
    assert_no_reminder(plain)
    quoted = await server.trace(bucket, content='追加「四」', append=True,
                                operation_id=operation_id and 'append-quoted')
    assert quoted.endswith('\n' + reminder(1))


async def test_trace_without_content_or_rejected_has_no_reminder(manager):
    bucket = await manager.create(content=QUOTED, tags=[], domain=['work'])
    assert_no_reminder(await server.trace(bucket, importance=6))
    assert_no_reminder(await server.trace('missing-bucket', content=QUOTED))
    protected = await manager.create(content=PLAIN, tags=[], domain=['work'], sealed=True)
    assert_no_reminder(await server.trace(protected, content=QUOTED))


@pytest.mark.parametrize('operation_id', [None, 'archive-op'])
async def test_archive_session_reminder(manager, operation_id):
    result = await server.archive_session(QUOTED, highlights='「亮点」', mood='平静',
                                          letter='给下一个我：“记得”', operation_id=operation_id)
    assert result.startswith('已归档本次对话:') and result.endswith('\n' + reminder(4))
    if operation_id:
        assert await server.archive_session(QUOTED, highlights='「亮点」', mood='平静',
                                            letter='给下一个我：“记得”', operation_id=operation_id) == result
    plain = await server.archive_session(PLAIN, operation_id=operation_id and 'archive-plain')
    assert plain.startswith('已归档本次对话:')
    assert_no_reminder(plain)


async def test_archive_session_rejected_has_no_reminder(manager):
    assert_no_reminder(await server.archive_session('   ', highlights=QUOTED))
