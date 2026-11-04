#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily.py — run a fixed sequence of local maintenance steps once.

The sequence lives in a JSON config (LOCAL_SUPERAGENT_ROUTINE, default
$LOCAL_SUPERAGENT_HOME/routine.json):

  {
    "steps": [
      {"name": "check",  "cmd": ["python3", "/path/check.py"],  "timeout": 240, "okExit": [0, 1]},
      {"name": "report", "cmd": ["python3", "/path/report.py"], "timeout": 180, "at": "22:30"}
    ]
  }

A step without "at" runs on every invocation. A step with "at" (HH:MM, local
time) runs once a day after that time, tracked in state/daily.json.

Usage:
  python3 daily.py                 # all steps; the "at" step only when due
  python3 daily.py --dry-run       # skip every "at" step
  python3 daily.py --force-daily [YYYYMMDD]   # force the "at" step now

Exit codes: 0 all ok; 1 one or more steps returned a declared okExit code
other than 0 (a warning, not a failure); 2 a step failed or timed out.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

HOME = os.environ.get('LOCAL_SUPERAGENT_HOME') or os.path.expanduser('~/.local-superagent')
CONFIG = os.environ.get('LOCAL_SUPERAGENT_ROUTINE') or os.path.join(HOME, 'routine.json')
STATE = os.path.join(HOME, 'state')
DONE = os.path.join(STATE, 'daily.json')
LOG = os.path.join(STATE, 'daily.jsonl')


def tail(text, n=300):
    text = (text or '').strip()
    if not text:
        return ''
    return ' | '.join(text.splitlines()[-3:])[:n]


def load_config(path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except OSError as e:
        sys.exit('cannot read %s: %s' % (path, e))
    except ValueError as e:
        sys.exit('invalid JSON in %s: %s' % (path, e))
    steps = data.get('steps') if isinstance(data, dict) else None
    if not isinstance(steps, list) or not steps:
        sys.exit('config %s needs a non-empty "steps" array' % path)
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or not isinstance(s.get('cmd'), list) or not s['cmd']:
            sys.exit('step %d must have a non-empty "cmd" array' % i)
        s.setdefault('name', 'step%d' % i)
        s.setdefault('timeout', 240)
        s.setdefault('okExit', [0])
        if 'at' in s:
            try:
                hh, mm = (int(x) for x in str(s['at']).split(':')[:2])
                assert 0 <= hh < 24 and 0 <= mm < 60
            except (ValueError, AssertionError):
                sys.exit('step %r has an invalid "at" value (expected HH:MM)' % s['name'])
    return steps


def run_step(step):
    cmd = [str(x) for x in step['cmd']]
    timeout = max(1, int(step['timeout']))
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except Exception as e:  # could not start at all
        return {'name': step['name'], 'rc': 125, 'ms': 0, 'tail': str(e)}
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, err = proc.communicate()
        rc, err = 124, 'timeout after %ss' % timeout
    return {'name': step['name'], 'rc': rc,
            'ms': int((time.time() - t0) * 1000), 'tail': tail(out or err)}


def done_day():
    try:
        with open(DONE, encoding='utf-8') as f:
            return (json.load(f) or {}).get('last')
    except (OSError, ValueError):
        return None


def mark_daily(day):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(DONE, 'w', encoding='utf-8') as f:
            json.dump({'last': day, 'at': time.strftime('%Y-%m-%d %H:%M:%S')}, f,
                      ensure_ascii=False)
    except OSError:
        pass


def gated_due(step, now, force_daily, dry_run, today):
    if 'at' not in step:
        return True
    if dry_run:
        return False
    if force_daily is not None:
        return True
    hh, mm = (int(x) for x in str(step['at']).split(':')[:2])
    return (now.hour, now.minute) >= (hh, mm) and done_day() != today


def main():
    ap = argparse.ArgumentParser(description='Run the configured daily routine steps once.')
    ap.add_argument('--config', default=CONFIG, help='routine JSON (default %s)' % CONFIG)
    ap.add_argument('--dry-run', action='store_true', help='skip every step that has "at"')
    ap.add_argument('--force-daily', nargs='?', const='', default=None,
                    help='force the "at" step now, optionally for YYYYMMDD')
    a = ap.parse_args()

    steps = load_config(a.config)
    now = datetime.now()
    today = now.strftime('%Y%m%d')

    ran = []
    for step in steps:
        if not gated_due(step, now, a.force_daily, a.dry_run, today):
            continue
        ran.append((step, run_step(step)))
        step_result = ran[-1][1]
        print('%-10s rc=%-3d %6dms  %s' % (step_result['name'], step_result['rc'],
                                           step_result['ms'], step_result['tail']))

    failed = []
    warned = []
    for s, r in ran:
        if r['rc'] not in s['okExit']:
            failed.append((s, r))
        elif r['rc'] != 0:
            warned.append((s, r))

    if failed:
        code = 2
    elif warned:
        code = 1
    else:
        code = 0

    # Mark the day only when every gated step that ran finished with a declared
    # okExit code, so a retry later in the day still picks up a failed night run.
    gated = [(s, r) for s, r in ran if 'at' in s]
    if gated and all(r['rc'] in s['okExit'] for s, r in gated):
        mark_daily(a.force_daily or today)

    print('daily: %d step(s), %d failed, %d warning(s) -> exit %d'
          % (len(ran), len(failed), len(warned), code))

    try:
        os.makedirs(STATE, exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                'steps': [r for _, r in ran], 'exit': code,
            }, ensure_ascii=False) + '\n')
    except OSError:
        pass
    return code


if __name__ == '__main__':
    sys.exit(main())
