#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dispatch.py — 把工单投到远端执行器，本机断了它也照跑。

工单格式沿用 queue.py 的 JSON，多两个字段：
  target: "remote" | "local"
  cmd:    在远端执行的 shell 命令

远端连接从环境变量读，没配就只当本机队列用：
  DISPATCH_SSH_HOST   ssh 目标，如 user@host
  DISPATCH_SSH_KEY    私钥路径（可选，不填走 ssh-agent）
  DISPATCH_WORK       远端工作目录，默认 ~/dsh-work

用法：
  dispatch.py add --title T --task "..." --cmd "命令" [--cwd /root/work] [--timeout 秒]
  dispatch.py list
  dispatch.py run <id>      # 投到远端，nohup 起后台，记 pid
  dispatch.py poll <id>     # 查日志与 pid，完了就落 done/fail
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DSH_HOME = Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh')))
HOME = Path(os.environ.get('CORE_HOME', str(DSH_HOME / 'agent-core')))
Q = HOME / 'queue'
INBOX, RUNNING, DONE, STUCK = Q / 'inbox', Q / 'running', Q / 'done', Q / 'stuck'
HOST = os.environ.get('DISPATCH_SSH_HOST', '').strip()
KEY = os.environ.get('DISPATCH_SSH_KEY', '').strip()
WORK = os.environ.get('DISPATCH_WORK', '~/dsh-work')

# 没写 timeout 的老单按这个算；超时就杀掉并记 ok:false
DEFAULT_TIMEOUT = 3600
MARK_EXIT = '@@DISPATCH_EXIT@@'
MARK_ERR = '@@DISPATCH_ERR@@'
MARK_OUT = '@@DISPATCH_OUT@@'


def iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def ssh(cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    if not HOST:
        raise RuntimeError('没配 DISPATCH_SSH_HOST，投不了远端')
    argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
    if KEY:
        argv += ['-i', KEY]
    argv += [HOST, cmd]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def ensure() -> None:
    for d in (INBOX, RUNNING, DONE, STUCK):
        d.mkdir(parents=True, exist_ok=True)


def find(qid: str) -> Path | None:
    for d in (INBOX, RUNNING, DONE, STUCK):
        p = d / f'{qid}.json'
        if p.exists():
            return p
    return None


def log_paths(rec: dict) -> tuple[str, str, str]:
    """stdout / stderr / 退出码三个文件。老单只有 log，就按同一前缀推。"""
    log = rec.get('log') or f'{WORK}/{rec.get("id", "job")}.log'
    base = log[:-4] if log.endswith('.log') else log
    return log, rec.get('errlog') or base + '.err', rec.get('exitlog') or base + '.exit'


def sections(out: str) -> dict:
    """把一次 ssh 里用标记分段的输出拆开。"""
    sec = {'head': [], 'exit': [], 'err': [], 'out': []}
    cur = 'head'
    for line in (out or '').splitlines():
        if line == MARK_EXIT:
            cur = 'exit'
        elif line == MARK_ERR:
            cur = 'err'
        elif line == MARK_OUT:
            cur = 'out'
        else:
            sec[cur].append(line)
    return {k: '\n'.join(v).strip('\n') for k, v in sec.items()}


def elapsed(rec: dict, p: Path) -> float:
    ts = rec.get('started') or rec.get('created') or ''
    try:
        return time.time() - datetime.fromisoformat(ts).timestamp()
    except Exception:
        return time.time() - p.stat().st_mtime


def finish(rec: dict, p: Path, extra: dict) -> None:
    rec.update(extra)
    rec['finished'] = iso()
    rec['status'] = 'done' if rec.get('ok') is True else 'failed'
    dst = (DONE if rec.get('ok') is True else STUCK) / p.name
    dst.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
    p.unlink()


def fail(rec: dict, p: Path, reason: str, out_tail: str = '', exit_code: int | None = None) -> int:
    rec.update({'ok': False, 'reason': str(reason)[:2000], 'result': out_tail[-4000:]})
    if exit_code is not None:
        rec['exit'] = exit_code
    finish(rec, p, {})
    print(f'{rec["id"]} 失败：{str(reason)[:300]}')
    return 0


def cmd_add(a) -> int:
    ensure()
    import random, string, time
    ts = ''
    n = int(time.time())
    digs = string.digits + string.ascii_lowercase
    while n:
        n, r = divmod(n, 36)
        ts = digs[r] + ts
    qid = f'd-{ts}-' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
    rec = {'id': qid, 'title': a.title, 'task': a.task, 'cmd': a.cmd,
           'cwd': a.cwd or WORK, 'target': a.target, 'created': iso(), 'status': 'queued'}
    if a.timeout and a.timeout > 0:
        rec['timeout'] = a.timeout
    (INBOX / f'{qid}.json').write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'queued {qid}  {a.title}')
    return 0


def cmd_list(a) -> int:
    ensure()
    for d, label in ((INBOX, '待投'), (RUNNING, '在远端跑'), (STUCK, '失败')):
        items = sorted(d.glob('*.json'))
        if items:
            print(f'{label}：{len(items)}')
            for p in items[:10]:
                r = json.loads(p.read_text(encoding='utf-8'))
                print(f"  {r['id']}  {r.get('title','')}")
    done = sorted(DONE.glob('*.json'))
    if done:
        print(f'完成：{len(done)}')
        for p in done[-3:]:
            r = json.loads(p.read_text(encoding='utf-8'))
            print(f"  {r['id']}  {r.get('title','')}  {r.get('finished','')[:16]}")
    return 0


def cmd_run(a) -> int:
    p = INBOX / f'{a.id}.json'
    if not p.exists():
        print(f'inbox 里没有 {a.id}')
        return 1
    rec = json.loads(p.read_text(encoding='utf-8'))
    if rec.get('target') != 'remote':
        print('这张单不是远端的，本机派给 subagent')
        return 2
    qid = rec['id']
    log, errlog, exitlog = log_paths(rec)
    # 子进程退出后把 $? 落进 exit 文件；stdout/stderr 分开存，失败时 stderr 尾部进 reason
    wrapped = f"bash -lc {shlex.quote(rec['cmd'])}; echo $? > {shlex.quote(exitlog)}"
    inner = (
        f"mkdir -p {shlex.quote(WORK)} && cd {shlex.quote(rec.get('cwd') or WORK)} && "
        f"rm -f {shlex.quote(exitlog)} {shlex.quote(errlog)} && "
        f"{{ setsid nohup bash -c {shlex.quote(wrapped)} > {shlex.quote(log)} 2> {shlex.quote(errlog)} & echo $!; }}"
    )
    r = ssh(inner)
    if r.returncode != 0:
        print(f'投放失败：{(r.stderr or r.stdout).strip()[:300]}')
        return 1
    pid = (r.stdout or '').strip().splitlines()[-1] if (r.stdout or '').strip() else ''
    if not pid.isdigit():
        print(f'投放失败：没拿到远端 pid（{pid[:80]}）')
        return 1
    rec.update({'status': 'running', 'pid': pid, 'log': log, 'errlog': errlog,
                'exitlog': exitlog, 'started': iso()})
    (RUNNING / f'{qid}.json').write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
    p.unlink()
    print(f'已投 {qid}  远端 pid={pid}  日志 {log}')
    return 0


def cmd_poll(a) -> int:
    p = RUNNING / f'{a.id}.json'
    if not p.exists():
        print(f'没有在跑的 {a.id}')
        return 1
    rec = json.loads(p.read_text(encoding='utf-8'))
    pid = str(rec.get('pid') or '')
    log, errlog, exitlog = log_paths(rec)
    script = (
        f"if kill -0 {shlex.quote(pid)} 2>/dev/null; then echo ALIVE; else echo DEAD; fi; "
        f"echo {MARK_EXIT}; cat {shlex.quote(exitlog)} 2>/dev/null; "
        f"echo; echo {MARK_ERR}; tail -c 1200 {shlex.quote(errlog)} 2>/dev/null; "
        f"echo; echo {MARK_OUT}; tail -c 2000 {shlex.quote(log)} 2>/dev/null"
    )
    st = sections(ssh(script).stdout)
    alive = 'ALIVE' in st['head']
    err_tail = st['err'].strip()
    out_tail = st['out'].strip()
    code = None
    lines = st['exit'].strip().splitlines()
    if lines and lines[-1].strip().lstrip('-').isdigit():
        code = int(lines[-1].strip())

    # 失败最先看：还在跑但超时就杀掉，绝不当成功
    if alive:
        limit = int(rec.get('timeout') or DEFAULT_TIMEOUT)
        if limit > 0 and elapsed(rec, p) > limit:
            # sid/pgid 就是 pid，按进程组杀，别留下子进程
            ssh(f"kill -TERM -- -{shlex.quote(pid)} 2>/dev/null; sleep 1; kill -KILL -- -{shlex.quote(pid)} 2>/dev/null", timeout=30)
            return fail(rec, p, 'timeout', out_tail)
        print(f'{a.id} 还在跑（pid {pid}）。尾部：\n{out_tail[-600:]}')
        return 0

    # 进程没了：只有退出码 0 才算完成；没有退出码也不许记成功
    if code == 0:
        finish(rec, p, {'ok': True, 'exit': 0, 'result': out_tail[-4000:]})
        print(f'{a.id} 完成。尾部：\n{out_tail[-600:]}')
        return 0
    if code is None:
        return fail(rec, p, 'no-exit-code（子进程没留下退出码）', out_tail)
    return fail(rec, p, err_tail or f'exit {code}', out_tail, code)


def main() -> int:
    ap = argparse.ArgumentParser(prog='dispatch')
    sub = ap.add_subparsers(dest='cmd', required=True)
    ad = sub.add_parser('add')
    ad.add_argument('--title', required=True)
    ad.add_argument('--task', default='')
    ad.add_argument('--cmd', required=True)
    ad.add_argument('--cwd', default='')
    ad.add_argument('--timeout', type=int, default=0, help='超时秒数，默认 %d' % DEFAULT_TIMEOUT)
    ad.add_argument('--target', default='remote', choices=['remote', 'local'])
    ad.set_defaults(fn=cmd_add)
    sub.add_parser('list').set_defaults(fn=cmd_list)
    rn = sub.add_parser('run')
    rn.add_argument('id')
    rn.set_defaults(fn=cmd_run)
    pl = sub.add_parser('poll')
    pl.add_argument('id')
    pl.set_defaults(fn=cmd_poll)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
