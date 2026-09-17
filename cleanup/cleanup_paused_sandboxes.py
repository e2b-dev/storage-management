#!/usr/bin/env python3
"""Preview or delete old paused E2B sandboxes.

Named snapshots are separate resources, included only with --delete-snapshots.
Before apply, stop application resumes, drain in-flight resumes, and pass
--resumes-stopped. This assertion does not lock the E2B account.
"""
import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import sys

from cleanup_journal import Journal
from sandbox_backend import BackendError, Missing, backend, metadata_arg, nonnegative, timestamp


def parser_for_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=nonnegative, default=90, help='age in days (default 90)')
    parser.add_argument('--metadata', type=metadata_arg, help='key=value,key2=value2')
    parser.add_argument('--age-from', choices=['paused','started'], default='paused')
    parser.add_argument('--delete-snapshots', action='store_true', help='also include all named snapshots from matching paused sandboxes, regardless of snapshot age')
    parser.add_argument('--apply', action='store_true', help='delete matches; default is a dry run')
    parser.add_argument('--resumes-stopped', action='store_true', help='assert that application resumes are blocked and in-flight resumes have drained; required for apply')
    parser.add_argument('--journal', help='named snapshot recovery file (default: account/selection-specific file beside this script)')
    parser.add_argument('--retry-pending', action='store_true', help='only process existing journal entries; use the same selection options as the original run')
    parser.add_argument('--backend', choices=['sdk','cli'], default='sdk', help=argparse.SUPPRESS)
    return parser


def run(args, api, cutoff):
    def eligible(info):
        age = info.end_at if args.age_from == 'paused' else info.started_at
        return (info.state == 'paused' and age < cutoff and
                all(info.metadata.get(k) == v for k,v in (args.metadata or {}).items()))

    scope = dict(days=float(args.days), age_from=args.age_from, metadata=args.metadata or {})
    context = Journal(args.journal, scope, args.apply) if args.delete_snapshots else nullcontext(None)
    with context as journal:
        # Load recovery records before listing so --retry-pending can work during list failures.
        pending = journal.pending if journal else {}
        candidates = {} if args.retry_pending else {s.sandbox_id:s for s in api.list_paused(args.metadata) if eligible(s)}
        ids = list(dict.fromkeys([*pending, *candidates]))
        deleted = skipped = failed = snapshots_deleted = 0
        if journal:
            print(f'Named snapshot recovery journal: {journal.path}')
        for sid in ids:
            listed = candidates.get(sid)
            record = pending.get(sid)
            if listed:
                print(f'{sid}\tstarted={listed.started_at.isoformat()}\tpaused={listed.end_at.isoformat()}')
            else:
                print(f'Pending cleanup for {sid}')
            try:
                if not args.apply:
                    if args.delete_snapshots:
                        snapshots = record['snapshots'] if record else api.snapshots(sid)
                        for snapshot in snapshots:
                            print(f'  named snapshot {snapshot}')
                    continue

                try:
                    current = api.get_info(sid)
                except Missing:
                    current = None
                if current is None and record is None:
                    print(f'Skipped {sid}: already gone')
                    skipped += 1
                    continue
                if current is not None:
                    unchanged = record is None or (current.started_at == timestamp(record['started_at']) and current.end_at == timestamp(record['paused_at']))
                    if not eligible(current) or not unchanged or (record and record['status'] == 'sandbox_deleted'):
                        print(f'Skipped {sid}: state, metadata, or pause changed')
                        skipped += 1
                        if record:
                            # Cancel snapshots of a parent whose eligibility changed; never delete them here.
                            del pending[sid]
                            journal.save()
                        continue
                    if args.delete_snapshots and record is None:
                        record = dict(status='prepared', started_at=current.started_at.isoformat(),
                                      paused_at=current.end_at.isoformat(), snapshots=api.snapshots(sid))
                        pending[sid] = record
                        journal.save()  # Must be durable before kill can run.
                    # Check again after snapshot enumeration/journaling. This narrows the race;
                    # the required application-level resume exclusion is the actual precondition.
                    try:
                        latest = api.get_info(sid)
                    except Missing:
                        latest = None
                    if latest is not None and (not eligible(latest) or latest.started_at != current.started_at or latest.end_at != current.end_at):
                        print(f'Skipped {sid}: changed immediately before deletion')
                        skipped += 1
                        if record:
                            del pending[sid]
                            journal.save()
                        continue
                    if latest is not None and api.kill(sid):
                        deleted += 1
                        print(f'Deleted paused sandbox {sid}')
                    else:
                        skipped += 1
                        print(f'Paused sandbox {sid} already gone')
                else:
                    skipped += 1
                    print(f'Paused sandbox {sid} already gone; recovering named snapshot cleanup')
                if record:
                    record['status'] = 'sandbox_deleted'
                    journal.save()
                    for snapshot in list(record['snapshots']):
                        try:
                            if api.delete_snapshot(snapshot):
                                snapshots_deleted += 1
                                print(f'  Deleted named snapshot {snapshot}')
                            else:
                                print(f'  Named snapshot {snapshot} already gone')
                        except BackendError as error:
                            failed += 1
                            print(f'Failed named snapshot {snapshot}: {error}', file=sys.stderr)
                            continue
                        record['snapshots'].remove(snapshot)
                        journal.save()
                    if not record['snapshots']:
                        del pending[sid]
                        journal.save()
            except BackendError as error:
                failed += 1
                print(f'Failed {sid}: {error}', file=sys.stderr)
        if args.apply:
            print(f'Deleted {deleted} of {len(ids)} matching/pending paused sandbox(es); skipped {skipped}, failed {failed}.')
            if args.delete_snapshots:
                print(f'Deleted {snapshots_deleted} named snapshot(s); {len(pending)} pending sandbox record(s).')
        else:
            print(f'Dry run: {len(ids)} paused sandbox(es) matched/pending; failed {failed}. Re-run with --apply --resumes-stopped after stopping resumes.')
        return 1 if failed else 0


def main(argv=None):
    parser = parser_for_cli()
    args = parser.parse_args(argv)
    if args.apply and not args.resumes_stopped:
        parser.error('--apply requires --resumes-stopped: block application resumes and drain in-flight resumes first; the API has no conditional kill')
    if args.retry_pending and not args.delete_snapshots:
        parser.error('--retry-pending requires --delete-snapshots')
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        return run(args, backend(args.backend), cutoff)
    except (BackendError, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print(f'Cleanup failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
