#!/usr/bin/env python3
"""Offline acceptance tests for both cleanup scripts; no account calls are made.

Run with ../.venv/bin/python test_cleanup_safety.py, or from the workspace root:
v2/.venv/bin/python v2/tests/test_cleanup_safety.py

Failures identify unmet cleanup requirements. The CLI stub is this same file,
invoked via a temporary executable named e2b. All SDK calls are replaced too.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cleanup"))


def sandbox(sid, paused_days=100, started_days=120, state="paused"):
    now = datetime.now(timezone.utc)
    return dict(sandboxId=sid, state=state, metadata={"env": "test"},
                startedAt=(now - timedelta(days=started_days)).isoformat().replace("+00:00", "Z"),
                endAt=(now - timedelta(days=paused_days)).isoformat().replace("+00:00", "Z"))


class Backend:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.snapshot_lists = {}

    def call(self, operation, sid=None):
        self.calls.append([operation, sid])
        cfg = self.config
        if operation == "list":
            if cfg.get("list_fail"):
                raise RuntimeError("503 listing unavailable")
            return sorted(cfg.get("sandboxes", []), key=lambda s: s["startedAt"])
        if operation == "info":
            if sid in cfg.get("info_fail", []):
                raise RuntimeError("503 service unavailable")
            if sid in cfg.get("network_fail", []):
                raise ConnectionError("connection reset")
            if sid in cfg.get("gone", []):
                raise KeyError(sid)
            item = next((s for s in cfg["sandboxes"] if s["sandboxId"] == sid), None)
            if item is None:
                raise KeyError(sid)
            sequence = cfg.get("info_sequence", {}).get(sid)
            return sequence.pop(0) if sequence else cfg.get("current", {}).get(sid, item)
        if operation == "snapshots":
            count = self.snapshot_lists.get(sid, 0) + 1
            self.snapshot_lists[sid] = count
            if count == cfg.get("snapshot_list_fail", {}).get(sid):
                raise RuntimeError("503 snapshot listing unavailable")
            return cfg.get("snapshots", {}).get(sid, [])
        if operation == "kill":
            if sid in cfg.get("kill_fail", []):
                raise RuntimeError("503 kill failed")
            if sid in cfg.get("gone_at_kill", []):
                return False
            if sid in cfg.get("resume_at_kill", []):
                self.calls.append(["killed_running_sandbox", sid])
            cfg["sandboxes"] = [s for s in cfg["sandboxes"] if s["sandboxId"] != sid]
            if sid in cfg.get("lost_kill_response", []):
                raise ConnectionError("kill response lost")
            return True
        if operation == "delete_snapshot":
            if sid in cfg.get("snapshot_delete_fail", []):
                raise RuntimeError("503 snapshot delete failed")
            for ids in cfg.get("snapshots", {}).values():
                if sid in ids:
                    ids.remove(sid)
                    return True
            return False
        raise AssertionError(operation)


def fake_cli():
    path = Path(os.environ["CLEANUP_TEST_STATE"])
    state = json.loads(path.read_text())
    backend = Backend(state["config"])
    backend.calls = state.get("calls", [])
    backend.snapshot_lists = state.get("snapshot_lists", {})
    args = sys.argv[2:]
    operation = {"list": "list", "info": "info", "kill": "kill"}.get(args[1])
    sid = None if operation == "list" else args[2]
    if args[1] == "snapshot":
        operation = {"list": "snapshots", "delete": "delete_snapshot"}[args[2]]
        sid = args[3]
    rc = 0
    try:
        result = backend.call(operation, sid)
        if operation == "snapshots":
            print(json.dumps([{"snapshotId": s} for s in result]))
        elif operation in ("list", "info"):
            print(json.dumps(result))
        elif operation == "kill":
            print(f"Sandbox {sid} has been killed" if result else f"Sandbox {sid} wasn't found")
        elif operation == "delete_snapshot":
            print(f"Snapshot {sid} has been deleted" if result else f"Snapshot {sid} wasn't found")
    except KeyError:
        print(f"Sandbox {sid} wasn't found", file=sys.stderr)
        rc = 1
    except (RuntimeError, ConnectionError) as error:
        print(str(error), file=sys.stderr)
        rc = 1
    path.write_text(json.dumps(dict(config=backend.config, calls=backend.calls,
                                   snapshot_lists=backend.snapshot_lists)))
    return rc


class Paginator:
    def __init__(self, items, limit):
        self.items = list(items)
        self.limit = limit

    @property
    def has_next(self):
        return bool(self.items)

    def next_items(self):
        batch, self.items = self.items[:self.limit], self.items[self.limit:]
        return batch


def run_python(backend, args):
    args = test_args(backend, args)
    import e2b
    from e2b.exceptions import NotFoundException, SandboxException
    import httpx

    def call(op, sid=None):
        try:
            return backend.call(op, sid)
        except KeyError:
            raise NotFoundException("not found")
        except ConnectionError:
            raise httpx.ConnectError("connection reset")
        except RuntimeError as error:
            raise SandboxException(str(error))

    def info(item):
        return SimpleNamespace(sandbox_id=item["sandboxId"], state=e2b.SandboxState(item["state"]),
                               started_at=datetime.fromisoformat(item["startedAt"]),
                               end_at=datetime.fromisoformat(item["endAt"]), metadata=item["metadata"])

    def listing(**kwargs):
        assert kwargs["order"] == "asc"
        assert kwargs["query"].state == [e2b.SandboxState.PAUSED]
        return Paginator(map(info, call("list")), kwargs["limit"])

    fake = SimpleNamespace(
        list=listing,
        get_info=lambda sid: info(call("info", sid)),
        kill=lambda sid: call("kill", sid),
        list_snapshots=lambda sandbox_id, limit: Paginator(
            [SimpleNamespace(snapshot_id=s) for s in call("snapshots", sandbox_id)], limit),
        delete_snapshot=lambda sid: call("delete_snapshot", sid))
    stdout, stderr = io.StringIO(), io.StringIO()
    rc = 0
    with patch.dict(os.environ, {"E2B_API_KEY": "offline-test-key"}), patch.object(e2b, "Sandbox", fake), patch.object(sys, "argv", ["cleanup"] + args), \
            patch.object(httpx.Client, "send", side_effect=AssertionError("network forbidden")), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            runpy.run_path(str(ROOT / "cleanup" / "cleanup_paused_sandboxes.py"), run_name="__main__")
        except SystemExit as error:
            rc = error.code
        except Exception as error:
            rc = 1
            stderr.write(f"UNHANDLED {type(error).__name__}: {error}")
    return SimpleNamespace(returncode=rc, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def run_shell(backend, args):
    args = test_args(backend, args)
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp)
        state = path / "state.json"
        state.write_text(json.dumps(dict(config=backend.config)))
        executable = path / "e2b"
        executable.write_text(f"#!{sys.executable}\nimport runpy,sys\nsys.argv = "
                              f"[{str(Path(__file__).resolve())!r}, '--fake-cli'] + sys.argv[1:]\n"
                              f"runpy.run_path({str(Path(__file__).resolve())!r}, run_name='__main__')\n")
        executable.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith("E2B_")}
        env.update(PATH=temp + os.pathsep + os.environ["PATH"], CLEANUP_TEST_STATE=str(state), E2B_API_KEY="offline-test-key")
        proc = subprocess.run(["bash", str(ROOT / "cleanup" / "cleanup_paused_sandboxes_cli.sh"), *args],
                              capture_output=True, text=True, env=env, timeout=30)
        saved = json.loads(state.read_text())
        backend.config, backend.calls = saved["config"], saved.get("calls", [])
        return proc


def test_args(backend, args):
    args = list(args)
    if not hasattr(backend, "journal_temp"):
        backend.journal_temp = tempfile.TemporaryDirectory()
    args += ["--journal", str(Path(backend.journal_temp.name) / "journal.json")]
    if "--apply" in args and not backend.config.get("omit_resume_guard"):
        args += ["--resumes-stopped"]
    return args


class CleanupSafety(unittest.TestCase):
    def each(self, config, args, assertion):
        for name, runner in [("python", run_python), ("shell", run_shell)]:
            with self.subTest(script=name):
                backend = Backend(json.loads(json.dumps(config)))
                proc = runner(backend, args)
                try:
                    assertion(backend, proc)
                finally:
                    backend.journal_temp.cleanup()

    def test_dry_run_selects_pause_age_and_never_mutates(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("old\t", p.stdout)
            self.assertNotIn("recent-pause\t", p.stdout)
            self.assertNotIn("running\t", p.stdout)
            self.assertFalse(any(c[0] in ("kill", "delete_snapshot") for c in b.calls))
        self.each(dict(sandboxes=[sandbox("old"), sandbox("recent-pause", 1),
                                  sandbox("running", state="running")]), [], verify)

    def test_started_age_mode(self):
        self.each(dict(sandboxes=[sandbox("old-start-recent-pause", 1)]), ["--age-from", "started"],
                  lambda b, p: self.assertIn("old-start-recent-pause\t", p.stdout))

    def test_more_than_one_thousand_candidates(self):
        self.each(dict(sandboxes=[sandbox(f"s{i}") for i in range(1001)]), [],
                  lambda b, p: self.assertEqual(sum("\tstarted=" in s for s in p.stdout.splitlines()), 1001))

    def test_apply_deletes_sandbox_and_snapshots(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(b.config["sandboxes"], [])
            self.assertEqual(b.config["snapshots"]["old"], [])
        self.each(dict(sandboxes=[sandbox("old")], snapshots={"old": ["snap"]}),
                  ["--apply", "--delete-snapshots"], verify)

    def test_snapshot_pagination_in_dry_run(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(sum(s.startswith("  named snapshot ") for s in p.stdout.splitlines()), 101)
            self.assertFalse(any(c[0] in ("kill", "delete_snapshot") for c in b.calls))
        self.each(dict(sandboxes=[sandbox("old")], snapshots={"old": [f"snap{i}" for i in range(101)]}),
                  ["--delete-snapshots"], verify)

    def test_recheck_skips_resumed_and_disappeared_candidates(self):
        self.each(dict(sandboxes=[sandbox("resumed"), sandbox("gone")], gone=["gone"],
                       current={"resumed": sandbox("resumed", state="running")}), ["--apply"],
                  lambda b, p: self.assertFalse(any(c[0] == "kill" for c in b.calls)))

    def test_kill_failure_continues_and_exits_nonzero(self):
        def verify(b, p):
            self.assertNotEqual(p.returncode, 0)
            self.assertIn(["kill", "second"], b.calls)
        self.each(dict(sandboxes=[sandbox("first"), sandbox("second")], kill_fail=["first"]),
                  ["--apply"], verify)

    def test_lookup_service_failure_is_not_success(self):
        self.each(dict(sandboxes=[sandbox("old")], info_fail=["old"]), ["--apply"],
                  lambda b, p: self.assertNotEqual(p.returncode, 0, p.stdout))

    def test_network_failure_does_not_abort_remaining_sandboxes(self):
        self.each(dict(sandboxes=[sandbox("first"), sandbox("second")], network_fail=["first"]),
                  ["--apply"], lambda b, p: self.assertIn(["kill", "second"], b.calls, p.stderr))

    def test_snapshot_preview_failure_does_not_abort_other_candidates(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 1)
            self.assertIn(["snapshots", "second"], b.calls)
            self.assertFalse(any(c[0] in ("kill", "delete_snapshot") for c in b.calls))
        self.each(dict(sandboxes=[sandbox("first"), sandbox("second")],
                       snapshot_list_fail={"first": 1}), ["--delete-snapshots"], verify)

    def test_snapshot_listing_apply_failure_does_not_abort_other_candidates(self):
        self.each(dict(sandboxes=[sandbox("first"), sandbox("second")],
                       snapshot_list_fail={"first": 1}), ["--apply", "--delete-snapshots"],
                  lambda b, p: self.assertIn(["kill", "second"], b.calls, p.stderr))

    def test_snapshot_delete_failure_still_attempts_remaining_snapshots(self):
        self.each(dict(sandboxes=[sandbox("old")], snapshots={"old": ["bad", "good"]},
                       snapshot_delete_fail=["bad"]), ["--apply", "--delete-snapshots"],
                  lambda b, p: self.assertIn(["delete_snapshot", "good"], b.calls, p.stderr))

    def test_rerun_recovers_snapshot_after_partial_failure(self):
        for name, runner in [("python", run_python), ("shell", run_shell)]:
            with self.subTest(script=name):
                b = Backend(dict(sandboxes=[sandbox("old")], snapshots={"old": ["snap"]},
                                 snapshot_delete_fail=["snap"]))
                self.assertNotEqual(runner(b, ["--apply", "--delete-snapshots"]).returncode, 0)
                self.addCleanup(b.journal_temp.cleanup)
                b.config["snapshot_delete_fail"] = []
                p = runner(b, ["--apply", "--delete-snapshots"])
                self.assertEqual(b.config["snapshots"]["old"], [], p.stdout)
                self.assertEqual(p.returncode, 0, p.stderr)

    def test_lost_kill_response_recovers_without_listing(self):
        for name, runner in [("python", run_python), ("shell", run_shell)]:
            with self.subTest(script=name):
                b = Backend(dict(sandboxes=[sandbox("old")], snapshots={"old": ["snap"]},
                                 lost_kill_response=["old"]))
                first = runner(b, ["--apply", "--delete-snapshots"])
                self.addCleanup(b.journal_temp.cleanup)
                self.assertEqual(first.returncode, 1, first.stderr)
                self.assertEqual(b.config["sandboxes"], [])
                self.assertEqual(b.config["snapshots"]["old"], ["snap"])
                b.config["list_fail"] = True
                p = runner(b, ["--apply", "--delete-snapshots", "--retry-pending"])
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(b.config["snapshots"]["old"], [])
                data = json.loads((Path(b.journal_temp.name) / "journal.json").read_text())
                self.assertEqual(data["pending"], {})

    def test_recheck_after_snapshot_listing_skips_changed_parent(self):
        old = sandbox("old")
        def verify(b, p):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertFalse(any(c[0] in ("kill", "delete_snapshot") for c in b.calls))
            self.assertEqual(json.loads((Path(b.journal_temp.name) / "journal.json").read_text())["pending"], {})
        self.each(dict(sandboxes=[old], snapshots={"old": ["snap"]},
                       info_sequence={"old": [old, sandbox("old", state="running")]}),
                  ["--apply", "--delete-snapshots"], verify)

    def test_changed_metadata_is_rechecked(self):
        changed = sandbox("old")
        changed["metadata"] = {"env": "prod"}
        self.each(dict(sandboxes=[sandbox("old")], current={"old": changed}),
                  ["--metadata", "env=test", "--apply"],
                  lambda b, p: self.assertFalse(any(c[0] == "kill" for c in b.calls)))

    def test_dry_run_does_not_write_journal(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(list(Path(b.journal_temp.name).iterdir()), [])
        self.each(dict(sandboxes=[sandbox("old")], snapshots={"old": ["snap"]}),
                  ["--delete-snapshots"], verify)

    def test_invalid_days_fail_before_account_access(self):
        for value in ("-1", "nan", "inf", "garbage"):
            self.each(dict(sandboxes=[]), ["--days", value],
                      lambda b, p: (self.assertEqual(p.returncode, 2), self.assertFalse(b.calls)))

    def test_journal_write_failure_prevents_kill(self):
        from cleanup_journal import Journal
        b = Backend(dict(sandboxes=[sandbox("old")], snapshots={"old": ["snap"]}))
        with patch.object(Journal, "save", side_effect=OSError("disk full")):
            p = run_python(b, ["--apply", "--delete-snapshots"])
        self.addCleanup(b.journal_temp.cleanup)
        self.assertEqual(p.returncode, 1)
        self.assertIn("disk full", p.stderr)
        self.assertFalse(any(c[0] in ("kill", "delete_snapshot") for c in b.calls))

    def test_sdk_debug_mode_rejected_before_account_access(self):
        b = Backend(dict(sandboxes=[sandbox("old")], snapshots={"old": ["snap"]}))
        with patch.dict(os.environ, E2B_DEBUG="true"):
            result = run_python(b, ["--apply", "--delete-snapshots"])
        self.addCleanup(b.journal_temp.cleanup)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('E2B_DEBUG', result.stderr)
        self.assertFalse(b.calls)

    def test_apply_requires_resumes_stopped_before_any_kill(self):
        self.each(dict(sandboxes=[sandbox("old")], resume_at_kill=["old"], omit_resume_guard=True), ["--apply"],
                  lambda b, p: (self.assertEqual(p.returncode, 2), self.assertIn("resumes-stopped", p.stderr), self.assertFalse(b.calls)))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fake-cli"]:
        sys.exit(fake_cli())
    unittest.main(verbosity=2)
