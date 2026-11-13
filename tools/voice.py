#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voice.py — 借远端主机的嗓子说话。

远端 TTS 只监听 127.0.0.1:3122，本机走 SSH 去要：
  GET /tts?text=...&voice=zh-CN-XiaoxiaoNeural  -> audio/mpeg

连接信息从环境变量读：VOICE_SSH_HOST（必填）、VOICE_SSH_KEY（默认 ~/.ssh/id_ed25519）。

用法：
  voice.py say "文本" [--voice zh-CN-XiaoxiaoNeural] [--out DIR]
产物落 $EXPORT_ROOT/voice/，文件名带时间。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

KEY = os.environ.get('VOICE_SSH_KEY') or str(Path.home() / '.ssh' / 'id_ed25519')
HOST = os.environ.get('VOICE_SSH_HOST', '')
EXPORT_ROOT = Path(os.environ.get('EXPORT_ROOT') or os.environ.get('CORE_EXPORT')
                   or str(Path.home() / '.dsh' / 'exports'))
OUT = EXPORT_ROOT / 'voice'


def main() -> int:
    ap = argparse.ArgumentParser(prog='voice')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('say')
    s.add_argument('text')
    s.add_argument('--voice', default='zh-CN-XiaoxiaoNeural')
    s.add_argument('--out', default=str(OUT))
    a = ap.parse_args()

    if a.cmd != 'say':
        return 2
    if not HOST:
        print('没配 VOICE_SSH_HOST，语音没启用')
        return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d-%H%M%S}.mp3"
    dest = out / name

    from urllib.parse import quote
    url = (f'http://127.0.0.1:3122/tts?text={quote(a.text)}'
           f'&voice={quote(a.voice)}')
    r = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-i', KEY, HOST,
         f'curl -s -m 90 "{url}"'],
        capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        print(f'合成失败：{(r.stderr or b"").decode("utf-8", "ignore")[:300]}')
        return 1
    dest.write_bytes(r.stdout)
    print(f'{dest}  {len(r.stdout)} 字节')
    return 0


if __name__ == '__main__':
    sys.exit(main())
