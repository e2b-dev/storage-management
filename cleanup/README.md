# Paused sandbox cleanup

Preview, and with `--apply` delete, paused sandboxes selected by idle age and metadata.
Optionally include the named snapshots associated with each selected sandbox.

Two entry points with identical behaviour: `cleanup_paused_sandboxes.py` (E2B Python SDK) and
`cleanup_paused_sandboxes_cli.sh` (E2B CLI, launched through `run_python.sh`). Both read
`E2B_API_KEY`. **Default is a dry run** selecting sandboxes paused longer than 90 days.

A **paused sandbox** can be resumed. A **named snapshot** (`create_snapshot`,
`e2b sandbox snapshot create`) is a separate resource that outlives its source sandbox and is
touched only with `--delete-snapshots`.

## Run

```bash
export E2B_API_KEY=your_key

# Preview every paused sandbox
python cleanup_paused_sandboxes.py --days 0

# Preview one workload's sandboxes paused longer than 30 days
python cleanup_paused_sandboxes.py --days 30 --metadata env=dev
./cleanup_paused_sandboxes_cli.sh --days 30 --metadata env=dev

# Delete them, after stopping and draining the application's resumes
python cleanup_paused_sandboxes.py --days 30 --metadata env=dev --apply --resumes-stopped
```

Each match prints its ID, latest start/resume time, and latest pause time. All pages are
fetched, ordered by start time ascending.

| Flag | Default | Meaning |
|---|---|---|
| `--days N` | `90` | Select idle age strictly greater than N days. Fractions allowed. |
| `--metadata k=v,k2=v2` | none | Match every supplied pair. |
| `--age-from paused\|started` | `paused` | Measure from latest pause or latest start/resume. |
| `--apply` | off | Delete matches instead of previewing. |
| `--resumes-stopped` | off | Required with `--apply`: you assert application resumes are blocked and drained. |
| `--delete-snapshots` | off | Also include named snapshots of each selected sandbox, regardless of their age. |
| `--journal PATH` | automatic | Recovery file for named-snapshot cleanup. |
| `--retry-pending` | off | With `--delete-snapshots`: finish existing journal entries only. |

## Safety model

- `--resumes-stopped` is an **operator assertion, not a server lock**. The kill API has no
  state precondition, so a concurrent resume cannot be prevented by this tool. Block resumes in
  your application first, wait for in-flight ones to finish, and keep that in place for the run.
- Apply recomputes matches; a dry run is not a saved plan. State, metadata, age, and timestamps
  are rechecked immediately before each kill.
- Per-candidate errors are reported and the run continues. Confirmed-missing resources are
  skipped idempotently. Any failure gives exit code 1; invalid arguments give exit code 2.

## Named snapshot recovery

With `--delete-snapshots`, the tool records each parent's timestamps and snapshot IDs in a
journal **before** killing the parent, so a later run can finish the snapshot deletions even if
the parent is gone, the kill response was lost, or the run was interrupted. If a parent's
eligibility or timestamps change, its pending entry is cancelled and its snapshots left alone.

The default journal is `.cleanup-journal-<hash>.json` in this folder (path is printed). It is
bound to the API key, endpoint, age mode, threshold, and metadata selection; keep those the same
when retrying and keep the file until its pending count is zero. It is locked against parallel
local writers and written atomically. Dry runs never create journal or lock files. Snapshots
created after enumeration, and previously orphaned snapshots, are not discovered.

## Files

| File | Role |
|---|---|
| `cleanup_paused_sandboxes.py` | Behaviour owner: selection, recheck, apply, reporting. |
| `cleanup_paused_sandboxes_cli.sh` | Thin launcher that runs the Python with `--backend cli`. |
| `run_python.sh` | Picks the repo `.venv` interpreter, else `python3`; `E2B_TOOLS_PYTHON` overrides. |
| `sandbox_backend.py` | Normalises SDK and CLI responses into one shape. |
| `cleanup_journal.py` | Locked, atomic journal for snapshot recovery. |

Tests: `tests/cleanup/test_cleanup_safety.py`, `tests/cleanup/test_cleanup_journal.py` (offline)
and `tests/cleanup/validate_e2e.py` (live, creates tagged fixtures). See the root README.
