"""Read-only durable admission for bucket writers. Call under the root mutex.

This is policy, not an executor: it never initializes storage or resumes children.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from related_integrity import RelatedAdmissionDeferred, scan_relation_store, digest


class DeleteAdmissionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class DurableDeleteAdmission:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def rows(self, bucket_id=None):
        path = self.root / 'bucket_history.sqlite3'
        if not path.exists():
            return []
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='ob_confirmed_delete_operations'").fetchone():
                return []
            sql = 'SELECT delete_id,bucket_id,status,plan_json FROM ob_confirmed_delete_operations WHERE root_binding=?'
            args = [str(self.root)]
            if bucket_id is not None:
                sql += ' AND bucket_id=?'; args.append(bucket_id)
            return [dict(row) for row in conn.execute(sql,args)]

    def active(self, bucket_id, references=()):
        protected = {row['bucket_id'] for row in self.rows() if row['status'] != 'completed'}
        if bucket_id in protected or protected.intersection(references):
            raise DeleteAdmissionError('confirmed_delete_source_pending')

    def capture(self, bucket_id):
        inventory = scan_relation_store(self.root); inventory.require_complete()
        endpoint = inventory.endpoints.get(bucket_id)
        if endpoint is None:
            raise DeleteAdmissionError('confirmed_delete_source_missing')
        path = self.root / endpoint.path
        stat = path.stat()
        return {'path':endpoint.path, 'file_hash':hashlib.sha256(path.read_bytes()).hexdigest(),
                'incarnation':[stat.st_dev,stat.st_ino,stat.st_ctime_ns],
                'body_hash':hashlib.sha256(endpoint.body).hexdigest(),
                'classification_base_hash':digest([endpoint.body.hex(),{k:v for k,v in endpoint.metadata.items()
                    if k not in ('todos','todo_provenance','last_active','updated_at')}]),
                'non_relation_hash':digest([endpoint.body.hex(),{k:v for k,v in endpoint.metadata.items()
                    if k not in ('related_buckets','last_active','updated_at')}]),
                'completed_delete_ids':[r['delete_id'] for r in self.rows(bucket_id) if r['status']=='completed']}

    def capture_path(self, path):
        relative = Path(path).resolve().relative_to(self.root).as_posix()
        inventory = scan_relation_store(self.root); inventory.require_complete()
        for identity,endpoint in inventory.endpoints.items():
            if endpoint.path == relative:
                return identity,self.capture(identity)
        raise DeleteAdmissionError('confirmed_delete_source_missing')

    def admit(self, bucket_id, *, root_binding=None, expected_source=None, kind='mutation',
              references=(), allow_missing=False):
        if root_binding is not None and Path(root_binding).resolve() != self.root:
            raise DeleteAdmissionError('confirmed_delete_root_conflict')
        self.active(bucket_id,references)
        completed_rows = [r for r in self.rows(bucket_id) if r['status']=='completed']
        completed = [json.loads(row['plan_json'])['source_guard'] for row in completed_rows]
        # A delayed caller without an incarnation cannot distinguish old work
        # from work for a replacement. In particular, an old archive publish
        # must not resurrect its file after its publication receipt was lost.
        if completed and (not expected_source or 'incarnation' not in expected_source):
            raise DeleteAdmissionError('confirmed_delete_source_identity_required')
        if expected_source and any(g['incarnation'] == expected_source.get('incarnation') for g in completed):
            raise DeleteAdmissionError('confirmed_delete_incarnation_deleted')
        if expected_source and 'completed_delete_ids' in expected_source and any(
                r['delete_id'] not in expected_source['completed_delete_ids'] for r in completed_rows):
            raise DeleteAdmissionError('confirmed_delete_incarnation_deleted')
        if allow_missing and expected_source is None and not completed:
            return None  # Legacy non-bucket session IDs, after durable active admission.
        try:
            current = self.capture(bucket_id)
        except DeleteAdmissionError:
            if allow_missing and not completed and not (expected_source or {}).get('incarnation'):
                return None
            raise
        # A receipt event describes an already durably published mutation; a
        # subsequent ordinary update does not invalidate that event. It still
        # requires an extant source and no deletion since its captured evidence.
        if kind == 'receipt_event' and expected_source:
            if any(r['delete_id'] not in expected_source.get('completed_delete_ids',[]) for r in completed_rows):
                raise DeleteAdmissionError('confirmed_delete_incarnation_deleted')
            return current
        # The API classifier rereads current metadata after its provider await.
        # Todo metadata may be atomically replaced during the provider call.
        # The identical non-todo preimage binds its body and source identity;
        # inode/ctime changes alone do not mean that this incarnation was deleted.
        # The completed-delete ledger above still forbids any delete/recreation.
        if kind == 'reclassify' and expected_source and (
                expected_source.get('classification_base_hash') == current['classification_base_hash']
                and 'completed_delete_ids' in expected_source
                and expected_source.get('path') == current['path']):
            return current
        if expected_source and any(current.get(key) != value for key,value in expected_source.items()
                                   if key in ('path','file_hash','incarnation')):
            raise DeleteAdmissionError('confirmed_delete_source_changed')
        return current

    def relation(self, operation, capability, boundary, step=None):
        if operation.get('execution_guard') is not None:
            raise RelatedAdmissionDeferred('related_execution_deferred',operation.get('id'))
        plan = operation['plan']; request = plan['request']
        touched = {item['id'] for item in plan['steps']}
        refs = {i for item in plan['steps'] for i in item.get('desired',[])}
        refs.update(request.get('add',[])); refs.update(request.get('remove',[]))
        if request.get('source'): touched.add(request['source'])
        if request.get('target'): refs.add(request['target'])
        if any(row['status'] != 'completed' for row in self.rows()):
            inventory = scan_relation_store(self.root); inventory.require_complete()
            for identity in touched:
                if identity in inventory.endpoints:
                    refs.update(inventory.endpoints[identity].related.ids)
        try:
            for identity in touched: self.active(identity,refs)
        except DeleteAdmissionError as exc:
            raise RelatedAdmissionDeferred(exc.code,operation.get('id')) from exc
