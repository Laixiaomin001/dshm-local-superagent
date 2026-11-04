# End-to-end walkthrough

One task, from a hand-written inbox file to the result message in the main session.
Every step has a command and the output to expect. The plugin code this follows is
[plugins/delegate/index.js](../plugins/delegate/index.js).

## 0. Prerequisites

- The host is running with `dshm-local-delegate` installed and `delegate` +
  `delegate_status` in the tool list.
- The plugin reads its environment once, at load time. Set these **before** starting
  the host, or accept the defaults (`~/.local-superagent/delegate`):

```bash
export LOCAL_SUPERAGENT_HOME="$HOME/.local-superagent"
export LOCAL_SUPERAGENT_QUEUE="$LOCAL_SUPERAGENT_HOME/delegate"
export LOCAL_SUPERAGENT_POLL_MS=10000
Q="$LOCAL_SUPERAGENT_QUEUE"
mkdir -p "$Q/inbox" "$Q/done"
```

Expected: two empty directories.

## 1. Get a live session id

The report goes to `sessionId`, and the plugin resolves it with `agents.get()`, so it
has to be a session that is alive right now. The easy way to learn one is to let the
plugin write a ticket for you.

In the main session, call the `delegate` tool:

```
delegate(title="probe", task="Reply with the single word: pong")
```

Expected tool output:

```
Queued d-<base36 time>-<8 hex> (probe). Polls every 10s; the result will come back to this session.
```

Within one poll interval the worker runs and the file lands in `done/`:

```bash
sleep 15
ls -1t "$Q/done" | head -3
```

Expected: the newest file is `d-<base36 time>-<8 hex>.json`. Read it:

```bash
cat "$Q/done/$(ls -1t "$Q/done" | head -1)"
```

Expected shape (values differ):

```json
{
  "id": "d-mf3k2p0-7c1b9e4a",
  "title": "probe",
  "task": "Reply with the single word: pong",
  "cwd": "",
  "sessionId": "<the main session id>",
  "createdAt": "2026-01-01T12:00:00.000Z",
  "ok": true,
  "childId": "<child session id>",
  "stopReason": "end_turn",
  "output": "pong",
  "finishedAt": "2026-01-01T12:00:09.120Z",
  "notified": true
}
```

Copy `sessionId`. A ticket you write yourself needs that exact value; anything else
fails with `session-not-live` (see step 7).

## 2. Write the ticket by hand

The ticket fields are exactly what the plugin reads: `id`, `title`, `task`, `cwd`,
`sessionId`, `createdAt`. `id` decides the output file name, so keep it equal to the
file name stem. `cwd` is the worker's working directory; empty means
`LOCAL_SUPERAGENT_WORKSPACE` or `$HOME`.

```bash
ID=d-e2e0001-demo0001
cat > "$Q/inbox/$ID.json" <<EOF
{
  "id": "$ID",
  "title": "count markdown files",
  "task": "Count the .md files under the current directory, three levels deep at most. Reply with the count and the largest file name in one line.",
  "cwd": "$PWD",
  "sessionId": "<sessionId copied in step 1>",
  "createdAt": "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
}
```

Expected: `"$Q/inbox/$ID.json"` exists, queue count is 1.

```bash
ls -1 "$Q/inbox"
```

Expected:

```
d-e2e0001-demo0001.json
```

## 3. The plugin polls and claims the ticket

Within `LOCAL_SUPERAGENT_POLL_MS` (10 s by default) the poller parses the file and
moves it to `$ID.json.running` — an atomic `rename`, so two pollers cannot both take
the same ticket.

```bash
sleep 11; ls -1 "$Q/inbox"
```

Expected:

```
d-e2e0001-demo0001.json.running
```

If the file is still `d-e2e0001-demo0001.json`, the host is not running the plugin
or the poll interval has not elapsed yet. A file that fails to parse never blocks
anything: it is renamed to `done/d-e2e0001-demo0001.json.bad`.

## 4. The worker runs as a child session

`runOne()` calls `ctx.subagents.startActivation()` with `delivery: 'parent'` and the
task text as the prompt. The child session cannot see the main session; only what is
in `task` is available to it. Duration is capped by
`LOCAL_SUPERAGENT_WORKER_TIMEOUT_MS` (30 min by default).

You can watch it through the tool surface:

```
delegate_status()
```

Expected `recent:` to gain a line for this task only after it finishes; while it runs,
`queued:` is 0 because the ticket already left the inbox.

## 5. The result file appears

When the child session ends, the plugin writes `done/$ID.json` and moves the claim
marker to `done/$ID.json.taken`.

```bash
for i in $(seq 1 60); do
  [ -f "$Q/done/$ID.json" ] && break
  sleep 5
done
ls -1 "$Q/done" | grep "$ID"
```

Expected (two lines):

```
d-e2e0001-demo0001.json
d-e2e0001-demo0001.json.taken
```

Read the result:

```bash
cat "$Q/done/$ID.json"
```

Expected: the ticket fields come back unchanged, plus

```json
  "ok": true,
  "childId": "<child session id>",
  "stopReason": "end_turn",
  "output": "12\nREADME.en.md",
  "finishedAt": "2026-01-01T12:00:00.000Z",
  "notified": false
```

`output` is the worker's reply, capped at 200000 characters. `notified` starts as
`false` and flips to `true` once the notice has been delivered.

## 6. The report arrives in the main session

The next poll calls `notifyPending()`, which sends `agent.followup()` to the session in
`sessionId` — only when that session is `idle`. The message looks like:

```
[delegate] finished: count markdown files
12
README.en.md

Full record: <queue>/done/d-e2e0001-demo0001.json
```

Expected: this message appears as a user turn in the main session, and the record's
`notified` becomes `true`.

If the main session happens to be busy, the notice waits and goes out on a later
poll.

## 7. Check the failure path

A ticket whose `sessionId` is not live goes straight to `done/` with `ok: false`:

```bash
ID=d-e2e0002-badsess
cat > "$Q/inbox/$ID.json" <<EOF
{
  "id": "$ID",
  "title": "missing session",
  "task": "this should not run",
  "cwd": "",
  "sessionId": "no-such-session",
  "createdAt": "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
}
EOF
sleep 12; cat "$Q/done/$ID.json"
```

Expected:

```json
{
  "id": "d-e2e0002-badsess",
  "title": "missing session",
  "task": "this should not run",
  "cwd": "",
  "sessionId": "no-such-session",
  "createdAt": "2026-01-01T12:00:00.000Z",
  "ok": false,
  "reason": "session-not-live",
  "finishedAt": "2026-01-01T12:00:12.000Z"
}
```

`start-failed: …` and `run-failed: …` mean the child session could not start or threw;
the reason string carries the first 400 characters of the error.

## 8. Clean up

```bash
rm -f "$Q"/done/d-e2e0* "$Q"/inbox/d-e2e0*
```

`done/` keeps the newest 200 records and prunes the rest on each write.
