"""Normalized SDK and CLI operations shared by the v2 commands."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
import re
import subprocess
from types import SimpleNamespace


class BackendError(RuntimeError):
    pass


class Missing(BackendError):
    pass


def timestamp(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError('timestamps must be ISO strings or datetime values')
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamps must include a timezone')
    return result.astimezone(timezone.utc)


def nonnegative(value):
    try:
        result = float(value)
        if not math.isfinite(result) or not 0 <= result <= 365000:
            raise ValueError()
        return result
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError('must be a finite number between 0 and 365000') from None


def metadata_arg(value):
    result = {}
    for item in value.split(','):
        key, sep, val = item.partition('=')
        if not sep or not key or key in result:
            raise argparse.ArgumentTypeError('use unique nonempty keys: key=value,key2=value2')
        result[key] = val
    return result


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value):
        raise BackendError(f'invalid resource ID: {value!r}')
    return value


def normalize(raw):
    try:
        if isinstance(raw, dict):
            sid, state = raw['sandboxId'], raw['state']
            start, end, metadata = raw['startedAt'], raw['endAt'], raw.get('metadata') or {}
        else:
            sid, state = raw.sandbox_id, raw.state.value
            start, end, metadata = raw.started_at, raw.end_at, raw.metadata or {}
        if state not in ('paused', 'running') or not isinstance(metadata, dict):
            raise ValueError('invalid state or metadata')
        start, end = timestamp(start), timestamp(end)
        # The API can legitimately report a pause snapshot older than the latest start
        # (e.g. a resume that did not produce a new pause). Keep the row; do not abort.
        return SimpleNamespace(sandbox_id=identifier(sid), state=state, started_at=start, end_at=end, metadata=metadata)
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise BackendError(f'invalid sandbox response: {error}') from error


class SDKBackend:
    def __init__(self):
        from e2b import Sandbox, SandboxQuery, SandboxState
        from e2b.exceptions import NotFoundException
        self.sandbox, self.query, self.state, self.not_found = Sandbox, SandboxQuery, SandboxState, NotFoundException

    def call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except self.not_found as error:
            raise Missing(str(error)) from error
        except Exception as error:
            # External SDK errors include HTTP/transport and unexpected-status exceptions.
            raise BackendError(str(error)) from error

    def list_paused(self, metadata=None):
        paginator = self.call(self.sandbox.list, query=self.query(state=[self.state.PAUSED], metadata=metadata), limit=100, order='asc')
        while paginator.has_next:
            for item in self.call(paginator.next_items):
                yield normalize(item)

    def get_info(self, sid):
        return normalize(self.call(self.sandbox.get_info, sid))

    def kill(self, sid):
        return self.call(self.sandbox.kill, sid)

    def snapshots(self, sid):
        paginator = self.call(self.sandbox.list_snapshots, sandbox_id=sid, limit=100)
        ids = []
        while paginator.has_next:
            ids.extend(identifier(s.snapshot_id) for s in self.call(paginator.next_items))
        return list(dict.fromkeys(ids))

    def delete_snapshot(self, sid):
        return self.call(self.sandbox.delete_snapshot, sid)


class CLIBackend:
    def command(self, *args, missing=None, success=None):
        try:
            proc = subprocess.run(['e2b', *args], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, 'NO_COLOR': '1', 'FORCE_COLOR': '0'})
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BackendError(f'e2b command failed: {error}') from error
        output = re.sub(r'\x1b\[[0-9;]*m', '', proc.stdout + '\n' + proc.stderr).strip()
        if missing and any(line.strip() == missing for line in output.splitlines()):
            raise Missing(missing)
        if proc.returncode or (success and success not in output):
            raise BackendError(output or f'e2b exited {proc.returncode} without an expected response')
        if success:
            return True
        try:
            return json.loads(proc.stdout)
        except ValueError as error:
            raise BackendError('e2b returned invalid JSON') from error

    def list_paused(self, metadata=None):
        args = ['sandbox', 'list', '--state', 'paused', '--format', 'json', '--limit', '0', '--order', 'asc']
        if metadata:
            args += ['--metadata', ','.join(f'{k}={v}' for k, v in metadata.items())]
        result = self.command(*args)
        if not isinstance(result, list):
            raise BackendError('sandbox listing is not a JSON array')
        return (normalize(item) for item in result)

    def get_info(self, sid):
        return normalize(self.command('sandbox', 'info', sid, '--format', 'json', missing=f"Sandbox {sid} wasn't found"))

    def kill(self, sid):
        try:
            return self.command('sandbox', 'kill', sid, missing=f"Sandbox {sid} wasn't found", success=f'Sandbox {sid} has been killed')
        except Missing:
            return False

    def snapshots(self, sid):
        result = self.command('sandbox', 'snapshot', 'list', sid, '--format', 'json', '--limit', '0')
        try:
            return list(dict.fromkeys(identifier(s['snapshotId']) for s in result))
        except (TypeError, KeyError) as error:
            raise BackendError('invalid named snapshot listing') from error

    def delete_snapshot(self, sid):
        try:
            return self.command('sandbox', 'snapshot', 'delete', sid, missing=f"Snapshot {sid} wasn't found", success=f'Snapshot {sid} has been deleted')
        except Missing:
            return False


def backend(name):
    if os.environ.get('E2B_DEBUG', '').lower() == 'true':
        raise BackendError('E2B_DEBUG=true is unsupported: debug mode changes endpoints and can make SDK kill a no-op; unset it before account operations')
    return CLIBackend() if name == 'cli' else SDKBackend()
