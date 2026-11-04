#!/usr/bin/env python3
"""persona.py — render AGENTS.md from a local persona draft.

The draft is local data: it never enters this repository. Only the renderer
ships.

Mechanism:
  · the draft is markdown; its body starts at the first "## " heading
  · optional rules file (--rules FILE) is appended after the body
  · output goes to $DSH_HOME/AGENTS.md, with a .sha256 sidecar next to it
  · writes are atomic: temp file + rename, so a reader never sees half a file

Usage:
  persona.py render [--dry-run] [--out FILE] [--rules FILE]
  persona.py verify
  persona.py status

Paths: draft defaults to $LOCAL_SUPERAGENT_HOME/persona/draft.md
(override with LOCAL_PERSONA_DRAFT), output to $DSH_HOME/AGENTS.md.
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get('LOCAL_SUPERAGENT_HOME') or os.path.expanduser('~/.local-superagent'))
DRAFT = Path(os.environ.get('LOCAL_PERSONA_DRAFT') or HOME / 'persona' / 'draft.md')
DSH_HOME = Path(os.environ.get('DSH_HOME') or os.path.expanduser('~/.dsh'))
OUT = DSH_HOME / 'AGENTS.md'
SHA = DSH_HOME / 'AGENTS.md.sha256'

HEAD = '# Persona\n\n'


def body_of(core):
    start = core.find('\n## ')
    if start < 0:
        return core.strip()
    return core[start + 1:].split('\n---\n')[0].strip()


def render(dry_run=False, out=None, rules=None):
    if not DRAFT.exists():
        print(f'persona draft not found: {DRAFT}', file=sys.stderr)
        return 2
    text = HEAD + body_of(DRAFT.read_text(encoding='utf-8'))
    if rules:
        rp = Path(rules).expanduser()
        if not rp.exists():
            print(f'rules file not found: {rp}', file=sys.stderr)
            return 2
        text += '\n\n' + rp.read_text(encoding='utf-8').strip() + '\n'
    else:
        text += '\n'
    target = Path(out).expanduser() if out else OUT
    h = hashlib.sha256(text.encode()).hexdigest()
    print(f'chars: {len(text)} | sha256: {h[:16]}…')
    if dry_run:
        print(f'[dry-run] would write {target}')
        print('---- first 200 chars ----')
        print(text[:200])
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, target)
    SHA.write_text(h + '\n', encoding='utf-8')
    print(f'wrote {target} ({len(text.encode())} bytes)')
    return 0


def verify():
    if not OUT.exists():
        print(f'missing {OUT}', file=sys.stderr)
        return 2
    text = OUT.read_text(encoding='utf-8')
    h = hashlib.sha256(text.encode()).hexdigest()
    want = SHA.read_text(encoding='utf-8').strip() if SHA.exists() else ''
    ok = bool(text.strip()) and h == want
    print(f'current sha256: {h[:16]}…')
    print(f'recorded sha256: {want[:16] + "…" if want else "(none)"}')
    print(f'verdict: {"intact" if ok else "mismatch"}')
    return 0 if ok else 1


def status():
    print(f'draft : {DRAFT} ({DRAFT.stat().st_size if DRAFT.exists() else 0} bytes)')
    print(f'output: {OUT} ({OUT.stat().st_size if OUT.exists() else 0} bytes)')
    print(f'sha   : {SHA.read_text().strip()[:16] + "…" if SHA.exists() else "(none)"}')


def main():
    ap = argparse.ArgumentParser(prog='persona')
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('render')
    r.add_argument('--dry-run', action='store_true')
    r.add_argument('--out')
    r.add_argument('--rules')
    sub.add_parser('verify')
    sub.add_parser('status')
    a = ap.parse_args()
    fn = {'render': lambda: render(a.dry_run, a.out, a.rules),
          'verify': verify, 'status': status}[a.cmd]
    sys.exit(fn() or 0)


if __name__ == '__main__':
    main()
