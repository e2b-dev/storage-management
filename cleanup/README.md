# Streamed paused-sandbox cleanup

Delete currently paused sandboxes by time since their latest pause or latest start/resume.
The script streams ascending API pages and starts deleting before the full inventory is read.
The default is a dry run. `--apply` deletes immediately and permanently without prompts.

Both `cleanup_paused_sandboxes.py` and `cleanup_paused_sandboxes_cli.sh` use the same HTTP client.
The shell entry point needs Python and the repository dependencies, but does not need the E2B CLI.
Both read `E2B_API_KEY`, optional `E2B_DOMAIN`, and optional `E2B_API_URL` from the environment.

## Setup and run

From the repository root, install Python 3.11 or newer and the dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export E2B_API_KEY=your_key

# Preview paused sandboxes last started/resumed more than 72 hours ago.
.venv/bin/python cleanup/cleanup_paused_sandboxes.py --days 3 --age-from started

# Delete directly; no prior dry run or resume-exclusion assertion is required.
.venv/bin/python cleanup/cleanup_paused_sandboxes.py --days 3 --age-from started --apply

# Same operation through the shell entry point, limited to one workload.
bash cleanup/cleanup_paused_sandboxes_cli.sh --days 3 --age-from started --metadata env=dev --apply
```

`--age-from started` measures from the latest start/resume, including initial creation for a
sandbox that has never resumed. It can select a sandbox paused recently after a long run.
`--age-from paused` measures continuous pause age. Running sandboxes are excluded in both modes.
The cutoff is fixed when the run starts and the comparison is strictly older than that cutoff.

## Request budgets and streaming

Every GET and DELETE attempt, including retries, shares one process-wide pacing budget.
The default is **20 total requests per second**, with requests spaced apart rather than sent in
bursts. This keeps this cleanup process's GET traffic within a 20 GET/second limit and reserves
part of that same budget for deletes. It does not reserve capacity against other applications,
other API keys, or jobs on other machines. Set a lower `--rate` when sharing a budget, and respect
any lower account-specific delete limit.

There are no hidden SDK or CLI retries. HTTP 429, 500, 502, 503, 504 and transport failures retry
up to `--retries` times. A valid `Retry-After` is honored by all workers; otherwise retries use
exponential backoff with jitter. A requested wait longer than 60 seconds stops the run for
a later invocation instead of retrying early or holding a cron indefinitely. Authentication and
permission failures stop further requests. Exhausted per-resource failures are logged and the
run continues with other resources; listing failures stop discovery and return a nonzero exit.

The normal path holds one page and at most `--concurrency` deletes in flight. It uses the listed
state and timestamps and makes **no per-sandbox GETs**. With `--age-from started`, it stops listing
at the first timestamp at or beyond the cutoff. Pause-based retention must scan every page
because the API orders by start time, not pause time. `--limit` bounds the number of matching
sandboxes processed per run. A rerun discovers the remaining resources anew.

## Cron

An hourly job can delete up to 10,000 qualifying sandboxes per invocation. Save a wrapper at
`/opt/storage-management/cleanup-cron.sh`, adapting paths and region settings to your installation:

```bash
#!/usr/bin/env bash
set -euo pipefail
set -a
source /opt/storage-management/cleanup.env
set +a
exec /opt/storage-management/.venv/bin/python \
  /opt/storage-management/cleanup/cleanup_paused_sandboxes.py \
  --days 3 --age-from started --apply --rate 15 --limit 10000
```

Keep the API key in `cleanup.env`, restrict that file to its owner, and schedule:

```cron
0 * * * * /bin/bash /opt/storage-management/cleanup-cron.sh >> /opt/storage-management/cleanup.log 2>&1
```

Apply runs acquire a nonblocking local lock for the API key and endpoint. A second overlapping
run exits nonzero rather than starting another worker pool. `--lock-file` overrides its path.
Keep a shared lock path when using multiple installations on one host. This is a local process
lock, not a distributed lock; separate hosts or credentials need external coordination.
Monitor nonzero exits and rotate the log. An hourly job enforces retention on an hourly cadence.

## Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--days N` | `90` | Select age strictly greater than N days; fractions allowed. |
| `--age-from paused\|started` | `paused` | Latest pause or latest start/resume timestamp. |
| `--metadata k=v,k2=v2` | none | Match every supplied pair. |
| `--apply` | off | Permanently delete matches. |
| `--rate N` | `20` | Combined GET and DELETE attempts per second, including retries. |
| `--concurrency N` | `10` | Up to N sandbox deletes in flight; 1–64. |
| `--page-size N` | `100` | Resources per list request; 1–100. |
| `--retries N` | `5` | Additional attempts after transient errors; 0–10. |
| `--limit N` | none | Process at most N matching/pending sandboxes this run. |
| `--lock-file PATH` | automatic | Local overlap lock for apply runs. |
| `--recheck` | off | Fresh state, age and metadata lookup immediately before a normal sandbox delete. |
| `--delete-snapshots` | off | Also delete named snapshots associated with selected sandboxes, regardless of snapshot age. |
| `--journal PATH` | automatic | Named-snapshot recovery journal. |
| `--retry-pending` | off | With `--delete-snapshots`, process existing journal entries only. |

## Deletion semantics

The default cron path acts on the page's observed state. A sandbox resumed after it was listed
can be killed. `--recheck` narrows that interval but cannot make checking and deletion atomic.
Use application-level resume coordination if that race is unacceptable. The legacy
`--resumes-stopped` flag remains accepted for existing commands and does not create a lock.

Missing resources are treated as already gone. Failed resources are eligible on the next run.
Exit 0 means no failures, exit 1 indicates a failed request or local operation, exit 2 indicates
invalid arguments, and exit 130 indicates interruption. Completed deletions stand after an
interruption; requests already in flight can finish.

## Optional named-snapshot cleanup

A named snapshot is a separate resource that outlives its source sandbox. It is touched only with
`--delete-snapshots`. This mode processes parents sequentially, rechecking state, age, metadata
and timestamps around snapshot enumeration. It uses the same request budget as normal cleanup.

Before killing a parent, the tool durably records its timestamps and snapshot IDs. Reruns can
finish snapshot deletions after a lost kill response, partial failure or interruption. Changed
parent eligibility cancels its pending entry. The journal is private, locked and written
atomically; it is bound to credentials, endpoint and selection. Keep those consistent and retain
the journal until no entries remain. Dry runs do not create journals. Existing pending journal
entries are loaded into memory; the normal sandbox-only path needs no recovery inventory.

Snapshots created after enumeration and snapshots whose parents were already gone before the
run are not discovered. Snapshot retention is a separate policy from sandbox retention.

## Verification

Run offline tests from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`tests/cleanup/validate_e2e.py` creates uniquely tagged fixtures in the connected account, exercises
both entry points with one-resource pages, and deletes only its own fixtures. It validates age
selection, a recent resume, dry runs, bounded apply, named-snapshot cleanup and running-sandbox
preservation. It is an opt-in live test, not a million-resource benchmark.
