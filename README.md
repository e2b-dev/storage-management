# E2B storage management

Scripts for E2B customers to understand and manage what their sandboxes keep in storage:
paused sandboxes, their snapshots, and how those are used over time. Each tool lives in its
own folder with a README and Python and shell entry points that produce the same output.
Tools are read-only or dry-run by default.

## Tools

| Folder | Question it answers | Mutates? | Docs |
|---|---|---|---|
| [`lifecycle_insights/`](lifecycle_insights/) | After a sandbox is paused, how often and how long after does anyone resume it? Use it to size the impact of a retention change. | No, read-only | [README](lifecycle_insights/README.md) |
| [`cleanup/`](cleanup/) | Stream paused sandboxes older than N days and delete them under a shared request budget; suitable for cron. Named snapshots are optional. | Dry run by default; `--apply` deletes | [README](cleanup/README.md) |

Supporting folders:

| Folder | Contents |
|---|---|
| [`tests/`](tests/) | Mirrors the tool folders: `tests/lifecycle_insights/`, `tests/cleanup/`. Offline unit tests (`test_*.py`, no account access) and live harnesses (`validate_*.py`, create and clean up tagged sandboxes). |

## Setup

Python 3.11 or newer. The lifecycle-analysis shell entry point also needs `curl`, `jq`,
and the `e2b` CLI. Cleanup uses a shared HTTP client in both entry points.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export E2B_API_KEY=your_key
```

The lifecycle tools support E2B SDK 2.50.0 or newer. See each tool's README for its runtime and verification requirements.

## Quick start

```bash
# How often do paused sandboxes get resumed, and how long after? (read-only)
.venv/bin/python lifecycle_insights/analyze_sandbox_reuse.py --since-days 30 --max-events 0 --windows 1,2,3,7

# Which sandboxes have been paused for more than 30 days? (dry run)
.venv/bin/python cleanup/cleanup_paused_sandboxes.py --days 30
```

## Tests

```bash
.venv/bin/python -m unittest discover -s tests      # offline, no account calls
shellcheck lifecycle_insights/*.sh cleanup/*.sh
.venv/bin/python tests/lifecycle_insights/validate_lifecycle_e2e.py   # live: one tagged sandbox, ~2 min
.venv/bin/python tests/cleanup/validate_e2e.py                        # live: cleanup fixtures and a named snapshot
```

Live harnesses write evidence under `verification/`, which is gitignored because it contains
account data.

## Packaging a tool for a customer

Each tool folder is self-contained. To hand one over as a zip, include the folder's scripts
and README plus the root `requirements.txt` and `LICENSE`:

```bash
rm -rf /tmp/lifecycle_insights && mkdir /tmp/lifecycle_insights \
  && cp lifecycle_insights/*.py lifecycle_insights/*.sh lifecycle_insights/README.md requirements.txt LICENSE /tmp/lifecycle_insights/ \
  && (cd /tmp && zip -r "$OLDPWD/sandbox_lifecycle_insights.zip" lifecycle_insights -x '*.DS_Store')
```

`tests/lifecycle_insights/validate_customer_package_e2e.py` builds and checks that package end to end against
a live tagged sandbox (set `LIFECYCLE_ZIP` to test a prebuilt zip instead).

## Adding a tool

1. Create a folder named after the question it answers, with a `README.md` covering run
   commands, a flag table, and how to read the output.
2. Provide Python and shell entry points with identical output. Keep the
   Python script as the behaviour owner and make the shell script a thin launcher, as
   `cleanup/` does, or a parallel implementation with a parity test, as
   `lifecycle_insights/` does.
3. Default to read-only or dry run. Anything that deletes must require an explicit flag.
4. Add a matching `tests/<tool>/` folder with an empty `__init__.py`, offline `test_*.py` files,
   and, if it touches the account, a `validate_*_e2e.py` that creates only tagged fixtures and
   cleans them up.
5. Add a row to the Tools table above.

## License

[Apache License 2.0](LICENSE), matching the E2B SDKs.
