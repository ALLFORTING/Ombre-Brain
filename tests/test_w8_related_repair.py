import json
from pathlib import Path
import subprocess
import sys

import pytest
from tests.test_w8_related_integrity import store, write_bucket, graph
from related_integrity import RelatedError, RelationStore, plan_repair, scan_relation_store


def snapshot(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in Path(root).rglob('*') if p.is_file()}


def dirty(store):
    write_bucket(store.base_dir,'A',['A','B','B','missing'])
    write_bucket(store.base_dir,'B',None)
    return plan_repair(scan_relation_store(store.base_dir))


def test_scan_deterministic_policies_and_no_writes(store):
    dirty(store)
    before=snapshot(store.base_dir)
    plan=plan_repair(scan_relation_store(store.base_dir))
    assert plan==plan_repair(scan_relation_store(store.base_dir))
    assert {f['kind'] for f in plan['findings']}=={'self_edge','dangling_edge','duplicate_edge','one_way_edge','null'}
    assert snapshot(store.base_dir)==before
    assert store.relation_store.operations()==[]
    store.relation_store.apply_repair(plan)
    assert graph(store)=={'A':['B'],'B':['A'],'C':[]}
    after=snapshot(store.base_dir)
    assert not store.relation_store.apply_repair(plan)['changed']
    assert snapshot(store.base_dir)==after


def test_legacy_list_alone_is_not_repair(store):
    write_bucket(store.base_dir,'A',['B'])
    write_bucket(store.base_dir,'B',['A'])
    plan=plan_repair(scan_relation_store(store.base_dir))
    assert not plan['steps'] and not plan['findings']
    before=snapshot(store.base_dir)
    assert not store.relation_store.apply_repair(plan)['changed']
    assert snapshot(store.base_dir)==before


@pytest.mark.parametrize('alteration',['relation','identity','added','malformed','tampered','root'])
def test_stale_unsafe_or_tampered_plan_zero_writes(store,alteration):
    plan=dirty(store)
    if alteration=='relation':write_bucket(store.base_dir,'A','C')
    elif alteration=='identity':write_bucket(store.base_dir,'wrong',id='B')
    elif alteration=='added':write_bucket(store.base_dir,'D')
    elif alteration=='malformed':write_bucket(store.base_dir,'C',42)
    elif alteration=='tampered':plan['steps'][0]['desired']=['C']
    else:plan['root']='wrong'
    before=snapshot(store.base_dir)
    with pytest.raises(RelatedError):store.relation_store.apply_repair(plan)
    assert snapshot(store.base_dir)==before


def test_unreadable_conflicts_and_malformed_classified(store):
    root=Path(store.base_dir)
    (root/'dynamic/broken.md').write_text('not YAML')
    write_bucket(root,'B',location='archive')
    write_bucket(root,'C',{'complex':'bad'})
    inventory=scan_relation_store(root)
    assert {f['kind'] for f in inventory.blockers}=={'unreadable_bucket','duplicate_identity','malformed_related'}
    plan=plan_repair(inventory)
    before=snapshot(root)
    with pytest.raises(RelatedError):store.relation_store.apply_repair(plan)
    assert snapshot(root)==before


def test_cli_cold_scan_dry_run_and_explicit_apply(tmp_path):
    root=tmp_path/'isolated';root.mkdir()
    write_bucket(root,'A','A,B,B,missing')
    write_bucket(root,'B',None)
    cli=Path(__file__).resolve().parents[1]/'scripts/related_integrity.py'
    def call(*args):return subprocess.run([sys.executable,str(cli),*map(str,args)],capture_output=True,text=True)
    before=snapshot(root)
    scan=call('scan','--buckets-dir',root)
    assert scan.returncode==0,scan.stderr
    assert snapshot(root)==before and not (root/'bucket_history.sqlite3').exists()
    out=tmp_path/'plan.json'
    preview=call('dry-run','--buckets-dir',root,'--plan-out',out)
    assert preview.returncode==0 and snapshot(root)==before
    assert json.loads(scan.stdout)==json.loads(out.read_text())
    assert call('dry-run','--buckets-dir',root,'--plan-out',out).returncode!=0
    assert call('scan').returncode!=0
    assert call('apply','--buckets-dir',root,'--plan',out).returncode==0
    after=snapshot(root)
    retry=call('apply','--buckets-dir',root,'--plan',out)
    assert retry.returncode==0 and json.loads(retry.stdout)['changed'] is False
    assert snapshot(root)==after


def test_completed_plan_never_overwrites_new_graph(store):
    plan=dirty(store);store.relation_store.apply_repair(plan)
    store.mutate_related('A',remove=['B'])
    before=snapshot(store.base_dir)
    assert not store.relation_store.apply_repair(plan)['changed']
    assert snapshot(store.base_dir)==before


def test_empty_cold_cli_apply_does_not_initialize_journal_or_lock(tmp_path):
    root=tmp_path/'empty';root.mkdir()
    write_bucket(root,'A')
    cli=Path(__file__).resolve().parents[1]/'scripts/related_integrity.py'
    plan=tmp_path/'noop.json'
    plan.write_text(json.dumps(plan_repair(scan_relation_store(root))))
    before=snapshot(root)
    result=subprocess.run([sys.executable,str(cli),'apply','--buckets-dir',str(root),'--plan',str(plan)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert snapshot(root)==before
