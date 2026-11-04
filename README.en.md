# dshm-local-superagent

[中文版](README.md)

A local superagent skeleton that depends on neither GitHub nor any online repository.

The main session queues a task, a local queue picks it up, in-process polling starts a child-session worker, and the result comes back. No server, no network, no resident process: just local JSON files and a timer that lives and dies with the host.

## What it is

Three DSH plugins and four command-line scripts that keep handing work to child sessions, searching memory, and running scheduled scripts on the machine itself:

1. The main session calls `delegate`; the task becomes `inbox/<id>.json`.
2. The plugin scans the inbox every 10 seconds, claims the file, and starts an independent child session as the worker.
3. When the worker is done it writes `done/<id>.json` and the result comes back to the originating session as a message.

The queue is a directory and the state is a file. You can `ls` it, edit it by hand, back it up. No database, no message broker.

## The three plugins

| Plugin | Tools | Role |
|---|---|---|
| `dshm-local-delegate` | `delegate` / `delegate_status` | Local task queue; in-process polling, child-session worker, result report |
| `dshm-local-memory` | `mem_search` / `mem_get` / `mem_store` / `mem_status` | Four memory entry points; a short-lived python process behind them, with a hard timeout per call |
| `dshm-local-jobs` | `jobs_status` / `jobs_run` | In-process scheduler; JSON job table, hard timeouts, catch-up, jitter, backoff |

`delegate` looks only at local directories and never touches a repository. `memory` calls a local script and never opens a socket. `jobs` uses `spawn` with an argv array (no shell) and kills the whole process group when time is up.

## Install

Plugins follow DSH's profile layout: the directory name is the package name, and the host resolves it from `profiles/ondevice/node_modules`.

```bash
export DSH_HOME=<host home directory>
export CORE=<DSH install directory>

for p in delegate memory jobs; do
  dest="$DSH_HOME/profiles/ondevice/node_modules/dshm-local-$p"
  cp -r "plugins/$p" "$dest"
  mkdir -p "$dest/node_modules/@deepseek-ai"
  ln -s "$CORE/node_modules/@deepseek-ai/dsh-tools" \
        "$dest/node_modules/@deepseek-ai/dsh-tools"
done
```

Register them in `$DSH_HOME/cordis.patch.yml` (the home layer):

```yaml
- insert:
    - id: dshm-local-delegate
      name: dshm-local-delegate
    - id: dshm-local-memory
      name: dshm-local-memory
    - id: dshm-local-jobs
      name: dshm-local-jobs
```

Restart the host. `delegate`, `mem_search` and `jobs_status` showing up in the tool list means it installed. If it does not come up, delete that patch block and start again; the host itself is unaffected.

`memory` is an adapter only. The backend is `tools/mem.py` in this repository: pure standard library, subcommands `search|get|store|status|reindex`. It loads from `$LOCAL_SUPERAGENT_HOME/memory/mem.py` by default; point `LOCAL_MEMORY_PY` elsewhere to change that.

## Configuration

Everything through environment variables; defaults live under `~/.local-superagent/`:

| Variable | Default | Purpose |
|---|---|---|
| `LOCAL_SUPERAGENT_HOME` | `~/.local-superagent` | Data root |
| `LOCAL_SUPERAGENT_QUEUE` | `$HOME/delegate` | Task queue |
| `LOCAL_SUPERAGENT_WORKSPACE` | `$HOME` | Default working directory for workers |
| `LOCAL_SUPERAGENT_POLL_MS` | `10000` | Queue polling interval |
| `LOCAL_SUPERAGENT_WORKER_TIMEOUT_MS` | `1800000` | Timeout for a single worker |
| `LOCAL_SUPERAGENT_JOBS_DIR` | `$HOME/jobs` | Job table and run log |
| `LOCAL_SUPERAGENT_TICK_MS` | `60000` | Scheduler tick |
| `MEM_HOME` | none (defaults to `$HOME/memory`) | Memory data root, overrides the default location |
| `LOCAL_MEMORY_PY` | `$HOME/memory/mem.py` | Memory backend path |
| `LOCAL_SUPERAGENT_PYTHON` | `python3` | Interpreter |
| `LOCAL_SUPERAGENT_ROUTINE` | `$HOME/routine.json` | Routine step table |
| `LOCAL_SUPERAGENT_SESSION_ID` | none | Report session for CLI-queued tasks |

`tools/delegate.py` is the command-line entry to the same queue, and `tools/daily.py` runs the steps in `routine.json` in order. Arguments, config format and exit codes for both are in [docs/architecture.md](docs/architecture.md).

## Memory

Local SQLite plus FTS5: the index is `index.db`, entries are two layers of markdown (`flow/` log, `canon/` canon). Chinese is indexed in three-character slices, with a `LIKE` fallback for two-character queries. It works offline, needs no model and no vector store.

The four tools and the task queue belong together: a worker cannot see the main session's context, so if a task needs old records, the task tells it to call `mem_search`; when it is done it writes a continuation note with `mem_store`. Worker and main session read the same database, with no network in between.

To install, put the backend where the plugin expects it, then build the index once:

```bash
SUPERAGENT="${LOCAL_SUPERAGENT_HOME:-$HOME/.local-superagent}"
mkdir -p "$SUPERAGENT/memory"
cp tools/mem.py "$SUPERAGENT/memory/mem.py"
python3 "$SUPERAGENT/memory/mem.py" reindex
```

To keep it somewhere else, pass `LOCAL_MEMORY_PY=/path/to/mem.py` at install time. Data sits in `$SUPERAGENT/memory/` by default; `MEM_HOME` overrides it. Design notes are in [docs/memory.md](docs/memory.md).

## Why local

Works offline. No API, no repository, no webhook; pull the network cable and it still runs.

Data stays on the machine. Task text, worker results, memory and run logs are all local files.

Starts and stops with the host. No systemd, no daemon; when the host stops, the queue stays on disk and picks up where it left off next time.

Auditable. Every run appends to `runs.jsonl`, and task text is grep-able as-is.

## Known limits

Single machine. The queue is a local directory; machines do not share it and there is no remote copy.

No multi-tenancy. One host, one queue, no notion of who is calling.

No permission layer. Anyone who can write the queue directory can edit tasks; file permissions are the whole model.

Serial. The scheduler runs one job per tick; delegate worker depth is capped by the host's `maxActiveSubagents`.

No automatic retry. A failure records the reason; re-queueing is a human decision.

Memory is one file on one machine. It does not sync across machines, and after hand-editing or bulk-importing files you need one `reindex` to catch up.

## Layout

```
dshm-local-superagent/
├── README.md         Chinese
├── README.en.md      this file
├── LICENSE           MIT
├── examples/
│   ├── e2e.md        end-to-end walkthrough
│   └── inbox.example.json
├── plugins/
│   ├── delegate/     index.js + package.json
│   ├── memory/       index.js + package.json
│   └── jobs/         index.js + package.json
├── tools/
│   ├── delegate.py
│   ├── daily.py
│   ├── mem.py
│   └── persona.py
├── docs/
│   ├── architecture.md
│   └── memory.md
└── .gitignore
```

## License

MIT. See [LICENSE](LICENSE).
