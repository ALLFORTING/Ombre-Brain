import importlib
from pathlib import Path
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import frontmatter
import pytest
import yaml
from bucket_manager import BucketManager
from related_integrity import (RelatedError, RelationStore, parse_related, scan_relation_store,
                               plan_delete, automatic_eligible)


def write_bucket(root, identity, related='', location='dynamic', **metadata):
    path = Path(root) / location / (identity + '.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = dict(id=identity, related_buckets=related, type=location,
                last_active='2001-01-01T00:00:00', updated_at='2001-01-01',
                activation=7, dormant=False, tags=['fixture'])
    meta.update(metadata)
    path.write_bytes(b'---\n' + yaml.safe_dump(meta, allow_unicode=True).encode() +
                     b'---\n\nfixture body  \r\nwith trailing bytes\n')
    return path


@pytest.fixture
def store(tmp_path):
    root = tmp_path / 'buckets'
    root.mkdir()
    manager = BucketManager({'buckets_dir': str(root)})
    for identity in ('A', 'B', 'C'):
        write_bucket(root, identity)
    return manager


def graph(store):
    return {i: list(e.related.ids) for i, e in scan_relation_store(store.base_dir).endpoints.items()}


@pytest.mark.parametrize('metadata,ids,representation', [({}, [], 'missing'),
    ({'related_buckets': None}, [], 'null'), ({'related_buckets': ''}, [], 'str'),
    ({'related_buckets': ' B, A,B,,C '}, ['B','A','C'], 'str'),
    ({'related_buckets': [' B ', 'A', 'B', '']}, ['B','A'], 'list'),
    ({'related_buckets': 'None'}, ['None'], 'str')])
def test_parser(metadata, ids, representation):
    parsed = parse_related(metadata)
    assert parsed.require_safe() == ids
    assert parsed.representation == representation


@pytest.mark.parametrize('raw', [123, True, {}, ['A', 3], ['A,B'], ['A\nB']])
def test_parser_rejects_complex_values(raw):
    with pytest.raises(RelatedError, match='metadata_invalid'):
        parse_related({'related_buckets': raw}).require_safe()


@pytest.mark.parametrize('target,code', [('A','related_self'), ('missing','related_target_missing')])
@pytest.mark.asyncio
async def test_bad_add_zero_mixed_writes(store, target, code):
    files = {p: p.read_bytes() for p in Path(store.base_dir).rglob('*.md')}
    db = Path(store.history_db_path).read_bytes()
    with pytest.raises(RelatedError, match=code):
        await store.update('A', content='must not write', related_buckets=target)
    assert all(p.read_bytes() == value for p, value in files.items())
    assert Path(store.history_db_path).read_bytes() == db
    assert store.relation_store.operations() == []


def test_add_remove_half_edges_noops_and_order(store):
    source = Path(store.base_dir)/'dynamic/A.md'
    reverse = Path(store.base_dir)/'dynamic/B.md'
    old = scan_relation_store(store.base_dir).endpoints
    assert store.mutate_related('A', add=['B'])['changed']
    after = scan_relation_store(store.base_dir).endpoints
    assert graph(store)['A'] == ['B'] and graph(store)['B'] == ['A']
    assert after['A'].metadata['last_active'] != old['A'].metadata['last_active']
    assert after['B'].metadata == {**old['B'].metadata, 'related_buckets': 'A'}
    assert after['B'].body == old['B'].body
    snapshots = {p: p.read_bytes() for p in (source, reverse, Path(store.history_db_path))}
    assert not store.mutate_related('A', add=['B'])['changed']
    assert all(p.read_bytes() == v for p,v in snapshots.items())
    write_bucket(store.base_dir, 'A', 'B,B,missing')
    write_bucket(store.base_dir, 'B', '')
    store.mutate_related('A', add=['B'])
    assert graph(store)['A'] == ['B','missing']
    assert graph(store)['B'] == ['A']
    store.mutate_related('A', remove=['B','missing'])
    assert graph(store)['A'] == graph(store)['B'] == []
    snapshots = {p: p.read_bytes() for p in (source, reverse, Path(store.history_db_path))}
    assert not store.mutate_related('A', remove=['B','missing'])['changed']
    assert all(p.read_bytes() == v for p,v in snapshots.items())
    write_bucket(store.base_dir,'B','A,A')
    store.mutate_related('A',remove=['B'])
    assert graph(store)['B'] == []


@pytest.mark.parametrize('state', [{'sealed':1},{'dormant':True},{'superseded_by':'C'},
    {'resolved':True},{'pinned':True},{'protected':True},{'digested':True}])
def test_explicit_lifecycle_allowed(store, state):
    write_bucket(store.base_dir,'B',**state)
    store.mutate_related('A',add=['B'])
    assert graph(store)['A'] == ['B'] and graph(store)['B'] == ['A']


@pytest.mark.parametrize('location', ['archive','feel','permanent'])
def test_explicit_locations_allowed(store, location):
    (Path(store.base_dir)/'dynamic/B.md').unlink()
    write_bucket(store.base_dir,'B',location=location)
    store.mutate_related('A',add=['B'])
    assert graph(store)['B'] == ['A']


@pytest.mark.asyncio
async def test_direct_update_replacement_is_bilateral(store):
    await store.update('A',related_buckets=['B','C'])
    assert graph(store) == {'A':['B','C'],'B':['A'],'C':['A']}
    await store.update('A',related_buckets='C')
    assert graph(store)['B'] == [] and graph(store)['C'] == ['A']


@pytest.mark.asyncio
async def test_delete_scans_all_locations_including_sealed(store):
    old = {}
    for i,location in [('A','archive'),('C','feel'),('S','permanent')]:
        dynamic=Path(store.base_dir)/f'dynamic/{i}.md'
        if dynamic.exists(): dynamic.unlink()
        write_bucket(store.base_dir,i,'B,B',location=location,sealed=1 if i=='S' else 0)
        old[i]=scan_relation_store(store.base_dir).endpoints[i]
    assert await store.delete('B')
    remaining=scan_relation_store(store.base_dir).endpoints
    for i,e in remaining.items():
        assert 'B' not in e.related.ids
        assert e.body == old[i].body
        assert e.metadata == {**old[i].metadata,'related_buckets':''}


@pytest.mark.parametrize('bad', ['unreadable','identity','duplicate','malformed'])
@pytest.mark.asyncio
async def test_delete_blocks_unknown_inventory(store,bad):
    root=Path(store.base_dir)
    if bad=='unreadable': (root/'dynamic/invalid.md').write_text('not frontmatter')
    elif bad=='identity': write_bucket(root,'wrong',id='B')
    elif bad=='duplicate': write_bucket(root,'B',location='archive')
    else: write_bucket(root,'C',{'not':'a relation'})
    before=(root/'dynamic/B.md').read_bytes()
    with pytest.raises(RelatedError): await store.delete('B')
    assert (root/'dynamic/B.md').read_bytes()==before
    assert store.relation_store.operations()==[]


def test_concurrent_opposite_and_third_neighbor(store):
    def left():
        for _ in range(15): store.mutate_related('A',add=['B'])
    def right():
        for _ in range(15): store.mutate_related('B',remove=['A'])
    def third(): store.mutate_related('C',add=['A'])
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(fn) for fn in (left,right,third)]
        for f in futures: f.result()
    result=graph(store)
    assert ('B' in result['A']) == ('A' in result['B'])
    assert 'C' in result['A'] and 'A' in result['C']


def load_server(tmp_path,monkeypatch):
    monkeypatch.setenv('OMBRE_BUCKETS_DIR',str(tmp_path/'runtime'))
    monkeypatch.delenv('OMBRE_API_KEY',raising=False)
    sys.modules.pop('server',None)
    server=importlib.import_module('server')
    server.decay_engine.ensure_started=AsyncMock()
    return server


@pytest.mark.asyncio
async def test_trace_validation_noop_and_delete_sealed_preview(tmp_path,monkeypatch):
    server=load_server(tmp_path,monkeypatch)
    a=await server.bucket_mgr.create('public')
    b=await server.bucket_mgr.create('sealed secret')
    await server.bucket_mgr.update(b,sealed=1)
    before=Path(server.bucket_mgr._find_bucket_file(a)).read_bytes()
    result=await server.trace(a,content='must not append',append=True,related=a)
    assert 'related_self' in result
    assert Path(server.bucket_mgr._find_bucket_file(a)).read_bytes()==before
    await server.trace(a,related=b)
    await server.trace(a,related=b)
    preview=await server._delete_with_confirmation([a],'')
    assert preview['status']=='preview'
    assert '1 个 sealed backlink' in preview['message']
    assert b not in str(preview) and 'sealed secret' not in str(preview)
    deleted=await server._delete_with_confirmation([a],preview['confirm_token'])
    assert deleted['status']=='deleted'
    assert parse_related((await server.bucket_mgr.get(b))['metadata']).ids==()


@pytest.mark.asyncio
async def test_merge_outgoing_and_incoming_migration(tmp_path,monkeypatch):
    server=load_server(tmp_path,monkeypatch)
    target=await server.bucket_mgr.create('target')
    source=await server.bucket_mgr.create('source')
    outgoing=await server.bucket_mgr.create('outgoing')
    incoming=await server.bucket_mgr.create('incoming')
    # Historical dirty graph, deliberately bypass canonical mutation in fixture.
    for identity,related in [(source,[source,target,outgoing,'missing',outgoing]),(incoming,[source]),(target,[source])]:
        path=Path(server.bucket_mgr._find_bucket_file(identity));post=frontmatter.load(path)
        post['related_buckets']=','.join(related);path.write_text(frontmatter.dumps(post))
    before={i:e for i,e in scan_relation_store(server.config['buckets_dir']).endpoints.items()}
    preview=await server.trace(target,merge=source)
    token=re.search(r'confirm_token: (\S+)',preview).group(1)
    result=await server.trace(target,merge=source,confirm_token=token)
    assert '已合并' in result, result
    inventory=scan_relation_store(server.config['buckets_dir'])
    assert source not in inventory.endpoints
    assert inventory.endpoints[target].related.ids==(outgoing,incoming)
    for i in (outgoing,incoming):
        assert inventory.endpoints[i].related.ids==(target,)
        assert inventory.endpoints[i].metadata['last_active']==before[i].metadata['last_active']
    assert all(source not in e.related.ids for e in inventory.endpoints.values())


@pytest.mark.asyncio
async def test_merge_unsafe_before_body_and_legacy_absent_fail_closed(tmp_path,monkeypatch):
    server=load_server(tmp_path,monkeypatch)
    target=await server.bucket_mgr.create('target')
    source=await server.bucket_mgr.create('source')
    invalid=Path(server.config['buckets_dir'])/'dynamic/invalid.md';invalid.write_text('broken')
    before=(await server.bucket_mgr.get(target))['content']
    assert 'merge blocked' in await server.trace(target,merge=source)
    assert (await server.bucket_mgr.get(target))['content']==before
    invalid.unlink()
    preview=await server.trace(target,merge=source)
    token=re.search(r'confirm_token: (\S+)',preview).group(1)
    original=server._execute_merge_operation
    server._execute_merge_operation=AsyncMock(return_value='paused')
    await server.trace(target,merge=source,confirm_token=token)
    server._execute_merge_operation=original
    record=server.bucket_mgr.read_merge_operations()[0]
    # Emulate an old unfinished operation before W8 existed.
    record['plan'].pop('related_inventory',None)
    Path(server.bucket_mgr._find_bucket_file(source)).unlink()
    result=await server._execute_merge_operation(record)
    assert 'legacy_merge_relation_unrecoverable' in result
    assert (await server.bucket_mgr.get(target))['content']==before
    assert server.bucket_mgr.read_merge_operations()[0]['status']!='complete'


@pytest.mark.asyncio
async def test_batch_trace_rejects_all_before_first_write(tmp_path,monkeypatch):
    from tests.test_w8_related_repair import snapshot
    server=load_server(tmp_path,monkeypatch)
    a=await server.bucket_mgr.create('first')
    b=await server.bucket_mgr.create('second')
    before=snapshot(Path(server.config['buckets_dir']))
    response=await server.trace(a+','+b,tags='must not write',related=b)
    assert 'related_self' in response and 'no batch changes' in response
    assert snapshot(Path(server.config['buckets_dir']))==before


@pytest.mark.asyncio
async def test_delete_does_not_repair_unrelated_history(store):
    write_bucket(store.base_dir,'A','B')
    untouched=write_bucket(store.base_dir,'C','C,C,missing')
    before=untouched.read_bytes()
    assert await store.delete('B')
    assert untouched.read_bytes()==before
    assert graph(store)['A']==[]


@pytest.mark.asyncio
async def test_merge_preserves_target_incoming_only_relation(store):
    write_bucket(store.base_dir,'C','A')  # existing target A incoming-only relation
    write_bucket(store.base_dir,'B','C')  # source B relation
    assert await store.delete('B',_relation_target='A',_relation_operation_key='fixture-merge')
    assert graph(store)=={'A':['C'],'C':['A']}


@pytest.mark.asyncio
async def test_replacement_preserves_input_order_and_null_means_empty(store):
    await store.update('A',related_buckets=['B','C'])
    old=scan_relation_store(store.base_dir).endpoints['A'].metadata
    await store.update('A',related_buckets=['C','B'])
    endpoint=scan_relation_store(store.base_dir).endpoints['A']
    assert endpoint.related.ids==('C','B')
    assert endpoint.metadata['last_active']==old['last_active']
    assert endpoint.metadata['updated_at']==old['updated_at']
    await store.update('A',related_buckets=None)
    assert graph(store)=={'A':[],'B':[],'C':[]}


def test_apply_preview_union_and_replacement_distinguish_null(store):
    store.mutate_related('A',add=['B'])
    plan=store.preview_related('A',add=['C'])
    store.apply_related_plan(plan,operation_key='planned-union')
    assert graph(store)['A']==['B','C']
    assert not store.apply_related_plan(plan,operation_key='planned-union')['changed']
    clear=store.preview_related('A',replace=None)
    store.apply_related_plan(clear,operation_key='planned-clear')
    assert graph(store)=={'A':[],'B':[],'C':[]}


def test_contradictory_internal_request_rejected_before_intent(store):
    from tests.test_w8_related_repair import snapshot
    before=snapshot(store.base_dir)
    with pytest.raises(RelatedError,match='related_request_conflict'):
        store.mutate_related('A',add=['B'],remove=['B'])
    assert snapshot(store.base_dir)==before


def test_explicit_unique_endpoints_allow_unrelated_duplicate_identity(store):
    write_bucket(store.base_dir,'D')
    write_bucket(store.base_dir,'D',location='archive')
    store.mutate_related('A',add=['B'])
    assert graph(store)['A']==['B'] and graph(store)['B']==['A']
    with pytest.raises(RelatedError,match='endpoint_ambiguous'):
        store.mutate_related('A',add=['D'])
