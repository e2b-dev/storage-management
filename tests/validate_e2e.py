#!/usr/bin/env python3
"""Live end-to-end validation of the v2 cleanup scripts.

Creates real sandboxes tagged with a unique metadata key, runs both scripts
against them, and asserts the observable behaviour. Every --apply call is
scoped by --metadata to the fixtures created here, so nothing else is touched.

Requires E2B_API_KEY, the Python SDK, and `e2b` for CLI validation.
"""

import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from e2b import Sandbox, SandboxState
from e2b.exceptions import NotFoundException

ROOT = Path(__file__).resolve().parents[1]
PYTHON_SCRIPT = [sys.executable, str(ROOT / "cleanup" / "cleanup_paused_sandboxes.py")]
SHELL_SCRIPT = [str(ROOT / "cleanup" / "cleanup_paused_sandboxes_cli.sh")]
MIN_OLD_AGE_S = 60  # how long the "old" sandbox runs before being paused
SANDBOX_TIMEOUT_S = 900  # running timeout; paused sandboxes and named snapshots need explicit cleanup

MATCH_LINE = re.compile(r"^(\S+)\tstarted=(\S+)\tpaused=(\S+)$", re.M)


def now() -> datetime:
    return datetime.now(timezone.utc)


def check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        raise AssertionError(message)


def run(cmd: list[str], *extra: str) -> subprocess.CompletedProcess:
    full = cmd + list(extra)
    if "--apply" in full and "--metadata" not in full:
        raise RuntimeError("refusing to run --apply without --metadata")
    if "--apply" in full:
        # These fixtures are private to this harness, which never resumes them.
        full.append("--resumes-stopped")
    proc = subprocess.run(full, capture_output=True, text=True)
    print(f"  $ {' '.join(Path(full[0]).name if i == 0 else a for i, a in enumerate(full))}")
    for line in (proc.stdout + proc.stderr).rstrip().splitlines():
        print(f"    | {line}")
    return proc


def matched_ids(stdout: str) -> set[str]:
    return {m.group(1) for m in MATCH_LINE.finditer(stdout)}


def validate(script_name: str, script: list[str]) -> None:
    run_id = uuid.uuid4().hex[:8]
    meta_key = "e2e_run"
    metadata = {meta_key: run_id}
    metadata_arg = f"{meta_key}={run_id}"
    print(f"\n=== {script_name}: fixtures tagged {metadata_arg}")

    created: list[Sandbox] = []
    snapshot = None
    journal_dir = tempfile.TemporaryDirectory(prefix="e2b-validation-")
    script = script + ["--journal", str(Path(journal_dir.name) / "journal.json")]
    try:
        old = Sandbox.create(metadata=metadata, timeout=SANDBOX_TIMEOUT_S)
        created.append(old)
        old_started = now()
        print(f"  created old      {old.sandbox_id}")

        running = Sandbox.create(metadata=metadata, timeout=SANDBOX_TIMEOUT_S)
        created.append(running)
        print(f"  created running  {running.sandbox_id}")

        # Let "old" accumulate run time so started-based and paused-based ages diverge.
        wait = MIN_OLD_AGE_S - (now() - old_started).total_seconds()
        if wait > 0:
            print(f"  waiting {wait:.0f}s so the old sandbox has a meaningful run time")
            time.sleep(wait)

        fresh = Sandbox.create(metadata=metadata, timeout=SANDBOX_TIMEOUT_S)
        created.append(fresh)
        print(f"  created fresh    {fresh.sandbox_id}")
        snapshot = fresh.create_snapshot()
        print(f"  named snapshot   {snapshot.snapshot_id} (from fresh)")

        check(old.pause(), "old.pause() returned True")
        check(fresh.pause(), "fresh.pause() returned True")

        old_info = Sandbox.get_info(old.sandbox_id)
        fresh_info = Sandbox.get_info(fresh.sandbox_id)
        check(old_info.state == SandboxState.PAUSED, "old is paused")
        check(fresh_info.state == SandboxState.PAUSED, "fresh is paused")
        check(old_info.end_at > old_info.started_at, "paused sandbox end_at (pause time) is after started_at")

        paused_ids = {old.sandbox_id, fresh.sandbox_id}
        fixture_ids = paused_ids | {running.sandbox_id}

        # Threshold that separates old (long run) from fresh (short run) when measured
        # from start, but excludes both when measured from pause.
        age_started_old = (now() - old_info.started_at).total_seconds()
        age_started_fresh = (now() - fresh_info.started_at).total_seconds()
        check(age_started_old - age_started_fresh > 20, "old ran at least 20s longer than fresh")
        threshold_s = (age_started_old + age_started_fresh) / 2
        threshold_days = f"{threshold_s / 86400:.8f}"
        print(f"  threshold {threshold_s:.0f}s (old started {age_started_old:.0f}s ago, fresh {age_started_fresh:.0f}s ago)")

        print("\n-- dry run, nothing old enough")
        proc = run(script, "--days", "1000", "--metadata", metadata_arg)
        check(proc.returncode == 0, "exit 0")
        check(matched_ids(proc.stdout) == set(), "no matches")
        check("Dry run: 0" in proc.stdout, "dry-run summary reports 0")

        print("\n-- dry run, everything paused is old enough (--days 0), with snapshot listing")
        proc = run(script, "--days", "0", "--metadata", metadata_arg, "--delete-snapshots")
        ids = matched_ids(proc.stdout)
        check(proc.returncode == 0, "exit 0")
        check(ids == paused_ids, f"matches exactly the two paused fixtures (got {sorted(ids)})")
        check(running.sandbox_id not in ids, "running sandbox is not listed")
        check(ids <= fixture_ids, "metadata filter kept out everything else in the account")
        check(f"snapshot {snapshot.snapshot_id}" in proc.stdout, "named snapshot of fresh is listed")
        check("Dry run: 2" in proc.stdout, "dry-run summary reports 2")

        print("\n-- --age-from started: only the long-running one qualifies")
        proc = run(script, "--days", threshold_days, "--age-from", "started", "--metadata", metadata_arg)
        check(proc.returncode == 0, "exit 0")
        check(matched_ids(proc.stdout) == {old.sandbox_id}, "only old matches")

        print("\n-- --age-from paused (default): both were paused seconds ago, none qualifies")
        proc = run(script, "--days", threshold_days, "--metadata", metadata_arg)
        check(proc.returncode == 0, "exit 0")
        check(matched_ids(proc.stdout) == set(), "no matches")

        print("\n-- apply with --delete-snapshots")
        proc = run(script, "--days", "0", "--metadata", metadata_arg, "--apply", "--delete-snapshots")
        check(proc.returncode == 0, "exit 0")
        check("Deleted 2 of 2" in proc.stdout, "summary reports 2 of 2 deleted")
        check("Deleted 1 named snapshot(s)" in proc.stdout, "summary reports 1 named snapshot deleted")

        for sbx in (old, fresh):
            try:
                Sandbox.get_info(sbx.sandbox_id)
                check(False, f"{sbx.sandbox_id} still exists")
            except NotFoundException:
                check(True, f"paused sandbox {sbx.sandbox_id} is gone")
        check(Sandbox.get_info(running.sandbox_id).state == SandboxState.RUNNING, "running sandbox untouched")
        remaining = Sandbox.list_snapshots(sandbox_id=fresh.sandbox_id)
        leftover = remaining.next_items() if remaining.has_next else []
        check(leftover == [], "no named snapshots remain for fresh")
        check(Sandbox.delete_snapshot(snapshot.snapshot_id) is False, "named snapshot already deleted")

        print("\n-- apply again: nothing left to do")
        proc = run(script, "--days", "0", "--metadata", metadata_arg, "--apply")
        check(proc.returncode == 0, "exit 0")
        check("Deleted 0 of 0" in proc.stdout, "summary reports 0 of 0")
    finally:
        print("\n-- cleanup")
        cleanup_failed = False
        if snapshot is not None:
            try:
                Sandbox.delete_snapshot(snapshot.snapshot_id)
            except Exception as error:
                cleanup_failed = True
                print(f"  failed to delete named snapshot {snapshot.snapshot_id}: {error}")
        for sbx in created:
            try:
                Sandbox.kill(sbx.sandbox_id)
            except Exception as error:
                cleanup_failed = True
                print(f"  failed to delete sandbox {sbx.sandbox_id}: {error}")
        journal_dir.cleanup()
        if cleanup_failed:
            raise AssertionError(f"fixture cleanup failed for {metadata_arg}; see resource IDs above")
        print(f"  fixture cleanup complete for {metadata_arg}")


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    targets = {"python": PYTHON_SCRIPT, "shell": SHELL_SCRIPT}
    if which != "all":
        targets = {which: targets[which]}
    failures = 0
    for name, script in targets.items():
        try:
            validate(name, script)
            print(f"\n=== {name}: ALL CHECKS PASSED")
        except AssertionError as error:
            failures += 1
            print(f"\n=== {name}: FAILED: {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
