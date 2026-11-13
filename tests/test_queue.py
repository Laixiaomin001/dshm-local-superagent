#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_queue.py — 队列协议回归测试。

每个用例在独立的临时 CORE_HOME 里跑，不碰真队列。
  python3 tests/test_queue.py

定位 queue.py：默认取本文件同级的 ../bin/ 或 ../tools/，可用 QUEUE_PY 覆盖。
全部通过时打印 PASS n/n 并退出 0；失败打印 FAIL 名称: 原因，退出 1。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 发布仓里脚本在 tools/；装到引擎后在同级的 bin/。两处都认，也能用 QUEUE_PY 直接指。
_QUEUE = HERE.parent / 'bin' / 'queue.py'
if not _QUEUE.exists():
    _QUEUE = HERE.parent / 'tools' / 'queue.py'
QUEUE = Path(os.environ.get('QUEUE_PY') or _QUEUE)
TIMEOUT = 60


def env_for(core):
    env = os.environ.copy()
    env['CORE_HOME'] = str(core)
    return env


def call(core, *args):
    return subprocess.run(
        [sys.executable, str(QUEUE), *args],
        capture_output=True, text=True, encoding='utf-8', timeout=TIMEOUT,
        env=env_for(core))


def qadd(core, title, key=''):
    args = ['add', '--title', title, '--task', 't']
    if key:
        args += ['--key', key]
    return call(core, *args)


def parse_qid(stdout):
    m = re.search(r'^queued (\S+)', stdout, re.M)
    if not m:
        raise AssertionError(f'add 没打出 queued id：{stdout!r}')
    return m.group(1)


def load(p):
    return json.loads(p.read_text(encoding='utf-8'))


def inbox(core):
    return core / 'queue' / 'inbox'


def running(core):
    return core / 'queue' / 'running'


def done(core):
    return core / 'queue' / 'done'


def stuck(core):
    return core / 'queue' / 'stuck'


def json_files(d):
    return sorted(d.glob('*.json'))


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def eq(got, want, what):
    if got != want:
        raise AssertionError(f'{what}: 期望 {want!r}，实际 {got!r}')


def test_duplicate_key(core):
    """add 两张同 key：第二张被拒，inbox 只留一张。"""
    r1 = qadd(core, '同键第一张', 'dup')
    eq(r1.returncode, 0, '第一张 add 退出码')
    qid = parse_qid(r1.stdout)

    r2 = qadd(core, '同键第二张', 'dup')
    check(r2.returncode != 0 or json_files(stuck(core)),
          f'第二张既没报错也没进 stuck（退出码 {r2.returncode}）')
    check('error' in (r2.stdout + r2.stderr).lower(), '第二张被拒却没给出 error 提示')

    files = json_files(inbox(core))
    eq(len(files), 1, 'inbox 张数')
    eq(load(files[0])['id'], qid, 'inbox 里留下的 id')


def test_claim_duplicate_key(core):
    """claim 一张后，再 claim 一张同 key 的手写单：拿不到。"""
    r = qadd(core, '正在跑的单', 'same')
    eq(r.returncode, 0, 'add 退出码')
    qid = parse_qid(r.stdout)
    eq(call(core, 'claim', qid).returncode, 0, '第一张 claim 退出码')

    seed = {'id': 'hand-1', 'title': '手写同键单', 'task': 't', 'cwd': '',
            'session': '', 'created': '2026-11-13T21:00:00+08:00',
            'status': 'queued', 'key': 'same'}
    (inbox(core) / 'hand-1.json').write_text(
        json.dumps(seed, ensure_ascii=False, indent=1), encoding='utf-8')

    r2 = call(core, 'claim', 'hand-1')
    check(r2.returncode != 0, '同 key 的第二张不该 claim 成功')
    check('已经有一单在跑' in (r2.stdout + r2.stderr),
          f'没说明为什么拿不到：{r2.stdout!r}')
    check((inbox(core) / 'hand-1.json').exists(), '第二张应留在 inbox')
    eq(len(json_files(running(core))), 1, 'running 张数')
    eq(json_files(running(core))[0].stem, qid, 'running 里那张')


def test_done(core):
    """done 后 list 显示已完成，记录 ok:true、status:done。"""
    r = qadd(core, '要完成的单')
    qid = parse_qid(r.stdout)
    eq(call(core, 'claim', qid).returncode, 0, 'claim 退出码')

    r = call(core, 'done', qid, '--result', '跑完了')
    eq(r.returncode, 0, 'done 退出码')
    rec = load(done(core) / f'{qid}.json')
    eq(rec['status'], 'done', 'done 记录 status')
    check(rec.get('ok') is True, f"done 记录 ok 应为 true，实际 {rec.get('ok')!r}")
    eq(len(json_files(running(core))), 0, 'running 张数')
    eq(len(json_files(inbox(core))), 0, 'inbox 张数')

    r = call(core, 'list')
    eq(r.returncode, 0, 'list 退出码')
    check('已完成：1' in r.stdout, f'list 没显示已完成 1：{r.stdout!r}')
    check(qid in r.stdout, 'list 的已完成列表里没有这张单')

    r = call(core, 'show', qid)
    eq(r.returncode, 0, 'show 退出码')
    check('status: done' in r.stdout, 'show 里 status 不是 done')
    check('ok: true' in r.stdout, 'show 里 ok 不是 true')


def test_fail(core):
    """fail 落 stuck，不是 done。"""
    r = qadd(core, '要失败的单')
    qid = parse_qid(r.stdout)
    eq(call(core, 'claim', qid).returncode, 0, 'claim 退出码')

    r = call(core, 'fail', qid, '--reason', '故意失败')
    eq(r.returncode, 0, 'fail 退出码')
    check((stuck(core) / f'{qid}.json').exists(), 'fail 后 stuck 里没有这张单')
    check(not (done(core) / f'{qid}.json').exists(), 'fail 后不该进 done')
    rec = load(stuck(core) / f'{qid}.json')
    eq(rec['status'], 'failed', 'stuck 记录 status')
    check(rec.get('ok') is False, f"stuck 记录 ok 应为 false，实际 {rec.get('ok')!r}")
    eq(len(json_files(running(core))), 0, 'running 张数')

    r = call(core, 'list')
    check('卡住：1' in r.stdout, f'list 没显示卡住 1：{r.stdout!r}')
    r = call(core, 'show', qid)
    check('所在目录：stuck' in r.stdout, 'show 没把单认成 stuck')


def test_sweep(core):
    """sweep --older 0 把 running 里超时的挪进 stuck。"""
    r = qadd(core, '要超时的单')
    qid = parse_qid(r.stdout)
    eq(call(core, 'claim', qid).returncode, 0, 'claim 退出码')

    time.sleep(1.05)  # heartbeat 只精确到秒，等过一秒再判 --older 0
    r = call(core, 'sweep', '--older', '0')
    eq(r.returncode, 0, 'sweep 退出码')
    check('swept 1' in r.stdout, f'sweep 没挪走超时单：{r.stdout!r}')
    eq(len(json_files(running(core))), 0, 'running 张数')
    p = stuck(core) / f'{qid}.json'
    check(p.exists(), 'sweep 后 stuck 里没有这张单')
    rec = load(p)
    eq(rec['status'], 'stale', 'sweep 记录 status')
    check(rec.get('ok') is False, f"sweep 记录 ok 应为 false，实际 {rec.get('ok')!r}")


def test_concurrent_claim(core):
    """两个进程同时 claim：只有一个成功。连跑三轮。"""
    env = env_for(core)
    for i in range(3):
        r = qadd(core, f'并发单 {i}', f'race-{i}')
        qid = parse_qid(r.stdout)
        procs = [
            subprocess.Popen(
                [sys.executable, str(QUEUE), 'claim', qid],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', env=env)
            for _ in range(2)
        ]
        for p in procs:
            p.communicate(timeout=TIMEOUT)
        wins = [p for p in procs if p.returncode == 0]
        eq(len(wins), 1, f'第 {i + 1} 轮 claim 成功数')
        p = running(core) / f'{qid}.json'
        check(p.exists(), f'第 {i + 1} 轮 winner 没进 running')
        eq(load(p)['status'], 'running', f'第 {i + 1} 轮 running 记录 status')
        check(not (inbox(core) / f'{qid}.json').exists(),
              f'第 {i + 1} 轮 inbox 里还留着这张单')


CASES = [
    ('add 同 key 两张只进一张', test_duplicate_key),
    ('claim 同 key 第二张拿不到', test_claim_duplicate_key),
    ('done 后 list 已完成且 ok:true', test_done),
    ('fail 落 stuck 不进 done', test_fail),
    ('sweep --older 0 把超时单挪进 stuck', test_sweep),
    ('两进程并发 claim 只有一个成功', test_concurrent_claim),
]


def main():
    if not QUEUE.exists():
        print(f'FAIL 找不到 queue.py：{QUEUE}（可用 QUEUE_PY 指定）')
        return 2
    print(f'queue.py: {QUEUE}')
    failed = []
    for name, fn in CASES:
        with tempfile.TemporaryDirectory(prefix='dsh-test-queue-') as tmp:
            core = Path(tmp)
            try:
                fn(core)
                print(f'PASS {name}')
            except AssertionError as e:
                failed.append(name)
                print(f'FAIL {name}: {e}')
            except Exception as e:
                failed.append(name)
                print(f'FAIL {name}: {type(e).__name__}: {e}')
                traceback.print_exc()
    n = len(CASES)
    if failed:
        print(f'FAIL {n - len(failed)}/{n}  （失败 {len(failed)} 条）')
        return 1
    print(f'PASS {n}/{n}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
