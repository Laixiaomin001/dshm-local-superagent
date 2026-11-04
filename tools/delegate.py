#!/usr/bin/env python3
"""delegate.py — queue one task for a local delegate worker.

Writes a single JSON file into the inbox; the delegate plugin polls that inbox,
starts a child agent session for it, and reports the result back to the session
that queued it.

Usage:
  python3 delegate.py "title" "full task text" [--cwd DIR] [--session SESSION_ID]
  printf 'full task text' | python3 delegate.py "title"

The queue root comes from LOCAL_SUPERAGENT_QUEUE, default
$LOCAL_SUPERAGENT_HOME/delegate (usually ~/.local-superagent/delegate).
"""
import argparse
import json
import os
import sys
import time
import uuid

HOME = os.environ.get('LOCAL_SUPERAGENT_HOME') or os.path.expanduser('~/.local-superagent')
ROOT = os.environ.get('LOCAL_SUPERAGENT_QUEUE') or os.path.join(HOME, 'delegate')
INBOX = os.path.join(ROOT, 'inbox')


def main():
    ap = argparse.ArgumentParser(description='Queue one task for the local delegate plugin.')
    ap.add_argument('title')
    ap.add_argument('task', nargs='?', help='task text; read from stdin when omitted')
    ap.add_argument('--cwd', default=os.environ.get('LOCAL_SUPERAGENT_WORKSPACE') or os.getcwd())
    ap.add_argument('--session', default=os.environ.get('LOCAL_SUPERAGENT_SESSION_ID'),
                    help='session id that should receive the report')
    a = ap.parse_args()

    task = a.task if a.task is not None else sys.stdin.read()
    task = (task or '').strip()
    if not task:
        sys.exit('task is empty')

    sid = (a.session or '').strip()
    if not sid:
        sys.exit('no session id; pass --session or set LOCAL_SUPERAGENT_SESSION_ID')

    os.makedirs(INBOX, exist_ok=True)
    rid = 'd-%x-%s' % (int(time.time()), uuid.uuid4().hex[:8])
    rec = {
        'id': rid,
        'title': a.title[:120],
        'task': task,
        'cwd': os.path.abspath(a.cwd),
        'sessionId': sid,
        'createdAt': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    path = os.path.join(INBOX, rid + '.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    print(json.dumps({'id': rid, 'queued': True, 'session': sid, 'path': path}, ensure_ascii=False))


if __name__ == '__main__':
    main()
