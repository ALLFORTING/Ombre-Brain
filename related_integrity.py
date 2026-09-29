"""Canonical undirected relations and narrowly scoped durable roll-forward.

Readers never initialize storage. Journals contain relation intent, identities
and fingerprints, never bodies or copies of unrelated metadata. A crash can
leave an intermediate graph; a recorded intent, not a cross-file transaction,
is what makes that graph recoverable.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

import yaml
from bucket_write_lock import bucket_write_scope
from maintenance_write_gate import DEFAULT_WRITE_COORDINATOR, guarded_mutation

LOCATIONS = ("permanent", "dynamic", "archive", "feel")
TABLE = "ob_related_operations"
_UNSET = object()


class RelatedError(ValueError):
    def __init__(self, code, operation_id=None):
        self.code, self.operation_id = code, operation_id
        super().__init__(code + (f"; operation_id={operation_id}" if operation_id else ""))


class RelatedAdmissionDeferred(RelatedError):
    """An injected resolver deferred execution without changing journal state."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class RelatedParseResult:
    ids: tuple[str, ...]
    duplicates: tuple[str, ...]
    diagnostics: tuple[str, ...]
    representation: str
    fingerprint: str

    def require_safe(self):
        if self.diagnostics:
            raise RelatedError("related_metadata_invalid")
        return list(self.ids)


def _valid_token(value):
    return isinstance(value, str) and bool(value.strip()) and not any(
        ord(c) < 32 or c == ',' for c in value)


def parse_related(metadata):
    """Missing/null are empty; CSV and list[str] preserve first occurrence order."""
    present = "related_buckets" in metadata
    raw = metadata.get("related_buckets")
    representation = "missing" if not present else (
        "null" if raw is None else type(raw).__name__)
    problems, tokens = [], []
    if raw is None:
        pass
    elif isinstance(raw, str):
        tokens = [part.strip() for part in raw.split(',') if part.strip()]
    elif isinstance(raw, list):
        if any(not isinstance(part, str) for part in raw):
            problems.append("non_string_member")
        else:
            tokens = [part.strip() for part in raw if part.strip()]
    else:
        problems.append("complex_or_scalar_type")
    if any(not _valid_token(part) for part in tokens):
        problems.append("invalid_id_token")
    ids, duplicates = [], []
    for part in tokens:
        if part in ids:
            duplicates.append(part)
        else:
            ids.append(part)
    return RelatedParseResult(tuple(ids), tuple(duplicates), tuple(problems),
                              representation, digest([present, representation, raw]))


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError("duplicate_frontmatter_key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
_HEADER = re.compile(br'\A(?:\xef\xbb\xbf)?---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)', re.S)


@dataclass
class Endpoint:
    id: str
    path: str
    metadata: dict
    related: RelatedParseResult
    body: bytes
    file_hash: str

    def anchor(self):
        return {"id": self.id, "path": self.path}


def _read_endpoint(root, relative):
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("unsafe_bucket_path")
    raw = path.read_bytes()
    raw.decode('utf-8')  # unreadable bucket cannot be a verified endpoint
    match = _HEADER.match(raw)
    if not match:
        raise ValueError("unreadable_frontmatter")
    meta = yaml.load(match.group(1).decode('utf-8'), Loader=_UniqueLoader)
    if not isinstance(meta, dict):
        raise ValueError("unreadable_frontmatter")
    identity = meta.get("id", path.stem)
    if not _valid_token(identity) or identity != identity.strip():
        raise ValueError("identity_invalid")
    if not (path.stem == identity or path.stem.endswith('_' + identity)):
        raise ValueError("identity_conflict")
    return Endpoint(identity, relative, meta, parse_related(meta), raw[match.end():],
                    hashlib.sha256(raw).hexdigest())


@dataclass
class RelationInventory:
    root: str
    endpoints: dict[str, Endpoint]
    blockers: list[dict]
    order: list[str]

    def endpoint(self, identity):
        endpoint = self.endpoints.get(identity)
        if endpoint is None:
            if self.blockers:
                raise RelatedError("related_endpoint_ambiguous")
            raise RelatedError("related_target_missing")
        if any(b['kind'] in ('unreadable_bucket', 'identity_conflict') for b in self.blockers):
            raise RelatedError('related_endpoint_ambiguous')
        endpoint.related.require_safe()
        return endpoint

    def require_complete(self):
        if self.blockers:
            raise RelatedError("related_scan_incomplete")

    def fingerprint(self):
        return digest({"root": self.root, "blockers": self.blockers,
                       "endpoints": [{**e.anchor(), "related": e.related.fingerprint}
                                     for _, e in sorted(self.endpoints.items())]})


def scan_relation_store(root):
    """Only formal bucket locations; never .bak, history, vectors or runtime."""
    root = Path(root).resolve()
    endpoints, blockers, order, conflicted = {}, [], [], set()
    for location in LOCATIONS:
        directory = root / location
        if not directory.exists() and not directory.is_symlink():
            continue
        if directory.is_symlink() or not directory.is_dir():
            blockers.append({'kind': 'unreadable_bucket', 'path': location})
            continue
        def unreadable_directory(exc):
            relative = Path(exc.filename).relative_to(root).as_posix()
            blockers.append({'kind': 'unreadable_bucket', 'path': relative})
        for parent, dirs, files in os.walk(directory, followlinks=False, onerror=unreadable_directory):
            for name in dirs:
                if (Path(parent) / name).is_symlink():
                    blockers.append({'kind': 'unreadable_bucket',
                                     'path': (Path(parent) / name).relative_to(root).as_posix()})
            dirs[:] = [d for d in dirs if not (Path(parent) / d).is_symlink()]
            for filename in files:
                if not filename.endswith('.md'):
                    continue
                relative = (Path(parent) / filename).relative_to(root).as_posix()
                try:
                    endpoint = _read_endpoint(root, relative)
                except Exception as exc:
                    blockers.append({"kind": "identity_conflict" if 'identity' in str(exc)
                                     else "unreadable_bucket", "path": relative})
                    continue
                if endpoint.id in endpoints or endpoint.id in conflicted:
                    first = endpoints.pop(endpoint.id, None)
                    if first:
                        blockers.append({"kind": "duplicate_identity", "path": first.path})
                    blockers.append({"kind": "duplicate_identity", "path": relative})
                    conflicted.add(endpoint.id)
                    continue
                endpoints[endpoint.id] = endpoint
                order.append(endpoint.id)
                if endpoint.related.diagnostics:
                    blockers.append({"kind": "malformed_related", "path": relative,
                                     "diagnostics": list(endpoint.related.diagnostics)})
    return RelationInventory(str(root), endpoints, sorted(blockers, key=lambda x: x['path']),
                             [i for i in order if i in endpoints])


def automatic_eligible(inventory, identity):
    endpoint = inventory.endpoints.get(identity)
    if not endpoint or endpoint.related.diagnostics:
        return False
    meta = endpoint.metadata
    successor = meta.get('superseded_by')
    try:
        sealed = int(meta.get('sealed', 0) or 0) == 1
    except (ValueError, TypeError):
        return False
    return not (sealed or bool(meta.get('dormant'))
                or endpoint.path.startswith('archive/') or meta.get('type') == 'archived'
                or (isinstance(successor, str) and successor.strip() not in ('', 'none')
                    and successor.strip() in inventory.endpoints))


def _step(endpoint, desired, *, activity=False):
    desired = list(dict.fromkeys(desired))
    if desired == list(endpoint.related.ids) and not endpoint.related.duplicates:
        return None
    return {"kind": "set_related", **endpoint.anchor(),
            "before": endpoint.related.fingerprint,
            "after": parse_related({"related_buckets": ','.join(desired)}).fingerprint,
            "desired": desired, "activity": activity}


def _anchors(inventory, identities, steps):
    after = {step['id']: step['after'] for step in steps if step and step['kind'] == 'set_related'}
    return [{**inventory.endpoints[i].anchor(),
             'before': inventory.endpoints[i].related.fingerprint,
             'after': after.get(i, inventory.endpoints[i].related.fingerprint)}
            for i in sorted(set(identities)) if i in inventory.endpoints]


def _plan(graph, kind, steps, request, **extra):
    return {"version": 1, "root": graph.root, "kind": kind,
            "request": request, "steps": sorted([s for s in steps if s], key=lambda s: s['id']),
            **extra}


def plan_mutation(inventory, source_id, *, add=(), remove=(), replace=_UNSET, origin='explicit'):
    source = inventory.endpoint(source_id)
    add, remove = list(dict.fromkeys(add)), list(dict.fromkeys(remove))
    if replace is not _UNSET:
        add = parse_related({"related_buckets": replace}).require_safe()
        remove = [i for i in source.related.ids if i not in add]
    if source_id in add:
        raise RelatedError('related_self')
    if set(add) & set(remove):
        raise RelatedError('related_request_conflict')
    for identity in add:
        inventory.endpoint(identity)
    for identity in remove:
        if identity in inventory.endpoints:
            inventory.endpoint(identity)
        elif inventory.blockers:
            raise RelatedError('related_endpoint_ambiguous')
    if origin == 'inferred' and any(not automatic_eligible(inventory, i)
                                    for i in [source_id, *add]):
        raise RelatedError('related_plan_stale')
    if replace is not _UNSET:
        desired = list(add)
    else:
        desired = [i for i in source.related.ids if i not in remove]
        desired += [i for i in add if i not in desired]
    # A replace is a set contract; retained one-way edges must also be completed.
    changes = [_step(source, desired, activity=origin == 'explicit'
                     and set(desired) != set(source.related.ids))]
    for identity in dict.fromkeys([*add, *remove]):
        if identity == source_id or identity not in inventory.endpoints:
            continue
        endpoint = inventory.endpoint(identity)
        ids = list(endpoint.related.ids)
        if identity in remove:
            ids = [i for i in ids if i != source_id]
        elif source_id not in ids:
            ids.append(source_id)
        changes.append(_step(endpoint, ids))
    return _plan(inventory, 'relation', changes,
                 {"source": source_id, "add": add, "remove": remove, "origin": origin,
                  "replace": None if replace is _UNSET else replace,
                  "replacement": replace is not _UNSET},
                 anchors=_anchors(inventory, [source_id, *add, *remove], changes))


def plan_delete(inventory, identity, *, target_id=None):
    inventory.require_complete()
    source = inventory.endpoint(identity)
    desired = {i: list(e.related.ids) for i, e in inventory.endpoints.items()}
    affected = {i for i, e in inventory.endpoints.items() if identity in e.related.ids}
    if target_id is not None:
        target = inventory.endpoint(target_id)
        if target_id == identity:
            raise RelatedError('related_self')
        neighbors = list(dict.fromkeys([
            *[i for i in source.related.ids if i in inventory.endpoints and i not in (identity, target_id)],
            *sorted(i for i, e in inventory.endpoints.items() if i not in (identity, target_id)
                    and identity in e.related.ids)]))
        desired[target_id] = list(dict.fromkeys([
            *[i for i in target.related.ids if i not in (identity, target_id)],
            *sorted(i for i, e in inventory.endpoints.items() if i not in (identity, target_id)
                    and target_id in e.related.ids)]))
        affected.add(target_id)
        affected.update(neighbors)
        affected.update(i for i in desired[target_id] if i in inventory.endpoints)
        for neighbor in neighbors:
            if neighbor not in desired[target_id]:
                desired[target_id].append(neighbor)
            if target_id not in desired[neighbor]:
                desired[neighbor].append(target_id)
        # Also restore target's already declared legitimate relationships.
        for neighbor in desired[target_id]:
            if neighbor not in inventory.endpoints:
                continue
            if target_id not in desired[neighbor]:
                desired[neighbor].append(target_id)
    steps = []
    for i, endpoint in inventory.endpoints.items():
        if i == identity or i not in affected:
            continue
        ids = [v for v in desired[i] if v != identity]
        if i == target_id:
            ids = [v for v in ids if v != target_id and v in inventory.endpoints]
        steps.append(_step(endpoint, ids))
    plan = _plan(inventory, 'merge' if target_id else 'delete', steps,
                 {"source": identity, "target": target_id}, inventory=inventory.fingerprint(),
                 anchors=_anchors(inventory, inventory.endpoints, steps))
    plan['steps'].append({"kind": "delete", **source.anchor(), "file_hash": source.file_hash})
    return plan


def plan_repair(inventory):
    findings, desired = [], {i: list(e.related.ids) for i, e in inventory.endpoints.items()}
    for i, endpoint in sorted(inventory.endpoints.items()):
        parsed = endpoint.related
        if parsed.diagnostics:
            continue
        if parsed.representation == 'null':
            findings.append({"kind": "null", "source": i})
        for duplicate in parsed.duplicates:
            findings.append({"kind": "duplicate_edge", "source": i, "target": duplicate})
        for neighbor in parsed.ids:
            if neighbor == i:
                findings.append({"kind": "self_edge", "source": i, "target": i})
                desired[i].remove(neighbor)
            elif neighbor not in inventory.endpoints:
                uncertain = any(b['kind'] != 'malformed_related' for b in inventory.blockers)
                findings.append({"kind": "unverified_edge" if uncertain else "dangling_edge",
                                 "source": i, "target": neighbor})
                if not uncertain:
                    desired[i].remove(neighbor)
            elif not inventory.endpoints[neighbor].related.diagnostics and i not in inventory.endpoints[neighbor].related.ids:
                findings.append({"kind": "one_way_edge", "source": i, "target": neighbor})
                if i not in desired[neighbor]:
                    desired[neighbor].append(i)
    steps = []
    for i, endpoint in sorted(inventory.endpoints.items()):
        if endpoint.related.diagnostics:
            continue
        step = _step(endpoint, desired[i])
        if not step and endpoint.related.representation == 'null':
            step = {"kind": "set_related", **endpoint.anchor(), "before": endpoint.related.fingerprint,
                    "after": parse_related({'related_buckets': ''}).fingerprint,
                    "desired": [], "activity": False}
        steps.append(step)
    plan = _plan(inventory, 'repair', steps, {"policy": 'preserve_half_edge_fill_reverse'},
                 inventory=inventory.fingerprint(), findings=findings, blockers=inventory.blockers,
                 anchors=_anchors(inventory, inventory.endpoints, steps),
                 policy_note="Preserve the existing half-edge and restore the undirected invariant; "
                             "cannot determine whether history contained a partial add or a partial remove.")
    plan['plan_id'] = digest(plan)
    return plan


def _readonly_connection(path):
    # immutable avoids sidecar creation and hot-journal recovery. A hot database
    # is unsafe to inspect this way; refuse it rather than silently ignoring it.
    if not path.exists():
        return None
    if any(Path(str(path) + suffix).exists() for suffix in ('-journal', '-wal')):
        raise RelatedError('related_journal_unavailable')
    return sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)


class RelationStore:
    def __init__(self, root, write_coordinator=None, *, admission_resolver=None):
        self.admission_resolver = admission_resolver
        self.root = Path(root).resolve()
        self.db = self.root / 'bucket_history.sqlite3'
        self.write_coordinator = write_coordinator or DEFAULT_WRITE_COORDINATOR

    def operations(self):
        if not self.db.exists():
            return []
        # Runtime journal reads use SQLite's native read-only protocol, so
        # committed WAL intents are visible. Cold vector readers separately
        # refuse any database requiring sidecars or crash recovery.
        try:
            conn = sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)
        except sqlite3.Error as exc:
            raise RelatedError('related_journal_unavailable') from exc
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ob_related_operations'").fetchone():
                return []
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM ob_related_operations ORDER BY rowid")]
        except sqlite3.Error as exc:
            raise RelatedError('related_journal_unavailable') from exc
        finally:
            conn.close()

    def lookup(self, key):
        return next((op for op in self.operations() if op['key'] == key), None)

    def _admit(self, operation, capability, boundary, step=None):
        """Optional synchronous owner admission. No business-store knowledge here."""
        guard = operation.get('execution_guard')
        if guard is not None:
            expected = digest({'request_digest': operation['request_digest'],
                               'plan': operation['plan'], 'execution_guard': guard})
            if operation.get('execution_digest') != expected:
                raise RelatedError('related_execution_guard_conflict', operation.get('id'))
            if self.admission_resolver is None or capability is None:
                raise RelatedAdmissionDeferred('related_execution_deferred', operation.get('id'))
        if self.admission_resolver is not None:
            self.admission_resolver(operation, capability, boundary, step)

    def checkpoint(self, boundary, operation, step=None):
        """Synchronous failure-injection seam; production does no work here."""

    @guarded_mutation('related_journal_write')
    def _save(self, operation):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute("CREATE TABLE IF NOT EXISTS ob_related_operations (operation_id TEXT PRIMARY KEY, operation_key TEXT UNIQUE, payload TEXT NOT NULL)")
            conn.execute("INSERT INTO ob_related_operations(operation_id,operation_key,payload) VALUES(?,?,?) ON CONFLICT(operation_id) DO UPDATE SET payload=excluded.payload",
                         (operation['id'], operation['key'], json.dumps(operation, ensure_ascii=False)))
            conn.commit()

    def _sync_directory(self, directory):
        if os.name != 'nt':
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @guarded_mutation('related_publish')
    def _publish(self, endpoint, step, *, recovering, admission=None):
        before = dict(endpoint.metadata)
        after = dict(before)
        after['related_buckets'] = ','.join(step['desired'])
        if step['activity'] and not recovering:
            after['last_active'] = datetime.now().isoformat(timespec='seconds')
            after['updated_at'] = datetime.now().date().isoformat()
        expected_untouched = {k: v for k, v in before.items() if k != 'related_buckets'
                              and not (step['activity'] and not recovering and k in ('last_active', 'updated_at'))}
        header = yaml.safe_dump(after, allow_unicode=True, sort_keys=True).encode('utf-8')
        payload = b'---\n' + header + b'---\n' + endpoint.body
        parsed = yaml.load(header.decode(), Loader=_UniqueLoader)
        if {k: v for k, v in parsed.items() if k in expected_untouched} != expected_untouched:
            raise RelatedError('related_metadata_invalid')
        path = self.root / endpoint.path
        fd, temporary = tempfile.mkstemp(prefix='.related-', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if admission is not None:
                admission()
            if _read_endpoint(self.root,step['path']).file_hash != endpoint.file_hash:
                raise RelatedError('related_recovery_conflict')
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @guarded_mutation('related_roll_forward')
    def _execute(self, operation, *, recovering, capability=None):
        try:
            self._admit(operation, capability, 'execute')
            if operation.get('version') != 1 or operation['plan']['root'] != str(self.root):
                raise RelatedError('related_recovery_conflict')
            inventory = scan_relation_store(self.root)
            deleting = operation['plan']['request'].get('source') if operation['plan']['kind'] in ('delete', 'merge') else None
            for anchor in operation['plan'].get('anchors', []):
                if anchor['id'] == deleting and anchor['id'] not in inventory.endpoints:
                    continue
                try:
                    endpoint = inventory.endpoint(anchor['id'])
                except RelatedError as exc:
                    raise RelatedError('related_recovery_conflict') from exc
                if (endpoint.path != anchor['path']
                        or endpoint.related.fingerprint not in (anchor['before'], anchor['after'])):
                    raise RelatedError('related_recovery_conflict')
            if operation['plan']['kind'] in ('delete', 'merge', 'repair'):
                inventory.require_complete()
            # Validate every deletion guard before touching any neighbor.
            recorded_progress = operation['progress']
            for index, step in enumerate(operation['plan']['steps']):
                if step['kind'] == 'delete' and (self.root / step['path']).exists():
                    if index < recorded_progress:
                        raise RelatedError('related_recovery_conflict')
                    if _read_endpoint(self.root, step['path']).file_hash != step['file_hash']:
                        raise RelatedError('related_recovery_conflict')
            for index, step in enumerate(operation['plan']['steps']):
                path = self.root / step['path']
                if index < recorded_progress:
                    # A durably finished effect is verified, never executed again.
                    if step['kind'] == 'delete':
                        if path.exists():
                            raise RelatedError('related_recovery_conflict')
                    else:
                        endpoint = _read_endpoint(self.root, step['path'])
                        if endpoint.id != step['id'] or endpoint.related.fingerprint != step['after']:
                            raise RelatedError('related_recovery_conflict')
                    continue
                if step['kind'] == 'delete' and not path.exists():
                    pass
                else:
                    try:
                        endpoint = _read_endpoint(self.root, step['path'])
                    except Exception as exc:
                        raise RelatedError('related_recovery_conflict') from exc
                    if endpoint.id != step['id']:
                        raise RelatedError('related_recovery_conflict')
                    if step['kind'] == 'delete':
                        inventory = scan_relation_store(self.root)
                        inventory.require_complete()
                        if any(step['id'] in e.related.ids for i, e in inventory.endpoints.items() if i != step['id']):
                            raise RelatedError('related_recovery_conflict')
                        if endpoint.file_hash != step['file_hash']:
                            raise RelatedError('related_recovery_conflict')
                        self.checkpoint('before_delete', operation, step)
                        self._admit(operation, capability, 'unlink', step)
                        os.unlink(path)
                        self._sync_directory(path.parent)
                        self.checkpoint('after_delete', operation, step)
                    elif endpoint.related.fingerprint == step['after']:
                        pass
                    elif endpoint.related.fingerprint == step['before']:
                        self.checkpoint('before_publish', operation, step)
                        self._admit(operation, capability, 'publish', step)
                        self._publish(endpoint, step, recovering=recovering,
                            admission=lambda:self._admit(operation,capability,'publish',step))
                        self.checkpoint('after_publish', operation, step)
                    else:
                        raise RelatedError('related_recovery_conflict')
                self._admit(operation, capability, 'progress', step)
                operation['progress'] = index + 1
                self._save(operation)
                self.checkpoint('after_progress', operation, step)
            # Recheck all terminal states, including steps whose progress was
            # persisted before another process interrupted this operation.
            for step in operation['plan']['steps']:
                if step['kind'] == 'delete':
                    if (self.root / step['path']).exists():
                        raise RelatedError('related_recovery_conflict')
                else:
                    endpoint = _read_endpoint(self.root, step['path'])
                    if endpoint.id != step['id'] or endpoint.related.fingerprint != step['after']:
                        raise RelatedError('related_recovery_conflict')
            inventory = scan_relation_store(self.root)
            request = operation['plan']['request']
            if operation['plan']['kind'] == 'relation':
                source = inventory.endpoint(request['source'])
                for identity in request['add']:
                    target = inventory.endpoint(identity)
                    if identity not in source.related.ids or source.id not in target.related.ids:
                        raise RelatedError('related_recovery_conflict')
                for identity in request['remove']:
                    target = inventory.endpoints.get(identity)
                    if identity in source.related.ids or (target and source.id in target.related.ids):
                        raise RelatedError('related_recovery_conflict')
            elif operation['plan']['kind'] in ('delete', 'merge'):
                inventory.require_complete()
                if request['source'] in inventory.endpoints or any(request['source'] in e.related.ids
                                                                for e in inventory.endpoints.values()):
                    raise RelatedError('related_recovery_conflict')
            operation['status'] = 'complete'
            operation['completed_at'] = datetime.now(timezone.utc).isoformat()
            self.checkpoint('before_complete', operation)
            self._admit(operation, capability, 'complete')
            self._save(operation)
            self.checkpoint('after_complete', operation)
            return {'changed': True, 'operation_id': operation['id'], 'status': 'complete'}
        except BaseException as exc:
            if isinstance(exc, RelatedAdmissionDeferred):
                raise
            if isinstance(exc, RelatedError) and exc.code in ('related_recovery_conflict', 'related_scan_incomplete'):
                operation['status'] = 'blocked'
                operation['error'] = exc.code
                # A stale capability must not checkpoint even a failure.
                self._admit(operation, capability, 'blocked')
                self._save(operation)
            if isinstance(exc, Exception):
                raise RelatedError(getattr(exc, 'code', 'related_operation_pending'), operation['id']) from exc
            raise

    def _recover(self):
        for operation in self.operations():
            if operation['status'] != 'complete' and operation.get('execution_guard') is None:
                try:
                    self._execute(operation, recovering=True)
                except RelatedAdmissionDeferred:
                    continue

    @guarded_mutation('related_recovery')
    def recover(self):
        # Pure first-use check: no mutex initialization or schema creation.
        if not any(op['status'] != 'complete' and op.get('execution_guard') is None
                   for op in self.operations()):
            return
        with bucket_write_scope(self.root):
            self._recover()

    @guarded_mutation('related_commit')
    def commit(self, planner, *, operation_key=None, request_digest=None, repair=False,
               execution_guard=None, capability=None):
        with bucket_write_scope(self.root):
            existing = self.lookup(operation_key) if operation_key else None
            if existing:
                if (request_digest != existing['request_digest']
                        or execution_guard != existing.get('execution_guard')):
                    raise RelatedError('related_operation_key_conflict')
                if existing.get('execution_guard') is not None:
                    self._admit(existing, capability, 'replay')
                if existing['status'] == 'complete':
                    return {'changed': False, 'operation_id': existing['id'], 'status': 'unchanged'}
                return self._execute(existing, recovering=True, capability=capability)
            if repair and any(op['status'] != 'complete' for op in self.operations()):
                raise RelatedError('related_operation_pending')
            self._recover()
            inventory = scan_relation_store(self.root)
            plan = planner(inventory)
            if not plan['steps']:
                return {'changed': False, 'status': 'unchanged', 'operation_id': None}
            operation = {'id': uuid.uuid4().hex, 'key': operation_key, 'version': 1,
                         'request_digest': request_digest or digest(plan['request']),
                         'plan': plan, 'status': 'pending', 'progress': 0,
                         'created_at': datetime.now(timezone.utc).isoformat()}
            if execution_guard is not None:
                operation['execution_guard'] = execution_guard
                operation['execution_digest'] = digest({'request_digest': operation['request_digest'],
                    'plan': plan, 'execution_guard': execution_guard})
            self._admit(operation, capability, 'intent')
            self.checkpoint('before_intent', operation)
            self._admit(operation, capability, 'intent')
            try:
                self._save(operation)
            except Exception as exc:
                # SQLite may have committed before reporting an I/O/close
                # failure. Never claim absence or roll back other cleanup on
                # that uncertainty; no bucket publication has started yet.
                raise RelatedError('related_intent_unconfirmed', operation['id']) from exc
            try:
                self.checkpoint('after_intent', operation)
            except Exception as exc:
                raise RelatedError('related_operation_pending', operation['id']) from exc
            return self._execute(operation, recovering=False, capability=capability)

    def preview(self, source_id, **kwargs):
        return plan_mutation(scan_relation_store(self.root), source_id, **kwargs)

    @guarded_mutation('related_mutation')
    def mutate(self, source_id, **kwargs):
        return self.commit(lambda inv: plan_mutation(inv, source_id, **kwargs))

    @guarded_mutation('related_repair_apply')
    def apply_repair(self, plan):
        supplied = dict(plan)
        plan_id = supplied.pop('plan_id', None)
        if digest(supplied) != plan_id or str(self.root) != plan.get('root'):
            raise RelatedError('related_plan_stale')
        def validate(inventory):
            inventory.require_complete()
            if plan.get('blockers') or plan != plan_repair(inventory):
                raise RelatedError('related_plan_stale')
            return plan
        return self.commit(validate, operation_key='repair:' + plan_id,
                           request_digest=plan_id, repair=True)


def read_vectors(path, model):
    """Read existing vectors without schema creation, provider or sidecars."""
    conn = _readonly_connection(Path(path).resolve())
    if conn is None:
        return {}
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='embeddings'").fetchone():
            return {}
        return {identity: json.loads(vector) for identity, vector in conn.execute(
            "SELECT bucket_id, embedding FROM embeddings WHERE model=?", (model,))}
    finally:
        conn.close()
