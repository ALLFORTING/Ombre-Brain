#!/usr/bin/env python3
"""Operator-only relation scan / deterministic plan / explicit apply.

No server import and no runtime startup. Always supply the isolated store root.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bucket_write_lock import initialize_bucket_write_lock
from related_integrity import RelatedError, RelationStore, plan_repair, scan_relation_store
from confirmed_delete_admission import DurableDeleteAdmission


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('scan', 'dry-run', 'apply'):
        child = sub.add_parser(command)
        child.add_argument('--buckets-dir', required=True, type=Path)
        if command == 'dry-run':
            child.add_argument('--plan-out', required=True, type=Path)
        if command == 'apply':
            child.add_argument('--plan', required=True, type=Path)
    args = parser.parse_args()
    root = args.buckets_dir.resolve()
    if not root.is_dir():
        parser.error('buckets-dir must be an existing explicit directory')
    try:
        if args.command == 'apply':
            plan = json.loads(args.plan.read_text(encoding='utf-8'))
            store = RelationStore(root,admission_resolver=DurableDeleteAdmission(root).relation)
            receipt = store.lookup('repair:' + str(plan.get('plan_id')))
            if not receipt:
                expected = plan_repair(scan_relation_store(root))
                if expected != plan or plan.get('blockers'):
                    raise RelatedError('related_plan_stale')
            if not plan['steps'] and not receipt:
                print(json.dumps({'changed': False, 'status': 'unchanged', 'operation_id': None}))
                return 0
            # This is the only CLI mutation branch; scanning never creates it.
            if not (root / '.bucket-write.lock').exists():
                initialize_bucket_write_lock(root)
            result = store.apply_repair(plan)
        else:
            result = plan_repair(scan_relation_store(root))
            if args.command == 'dry-run':
                destination = args.plan_out.resolve()
                if destination.is_relative_to(root):
                    parser.error('plan-out must be outside the bucket store')
                with destination.open('x', encoding='utf-8') as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RelatedError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
