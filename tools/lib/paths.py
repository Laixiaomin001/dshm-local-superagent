#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paths.py — agent-core 的路径常量，全机只此一份。

所有账本路径都从 CORE_HOME 展开：
  CORE_HOME 有值就用它，否则 ~/.dsh/agent-core。
DSH_HOME 是引擎自己的家（sessions 在它下面），跟 CORE_HOME 分开：
  DSH_HOME 有值就用它，否则 ~/.dsh。

只定义常量，不在 import 时建目录、不读文件。
新脚本一律 `from lib.paths import HOME, FLOW`，别再自己拼。
"""
from __future__ import annotations

import os
from pathlib import Path

# 引擎根目录（账本、memory、运行 都在它下面）
HOME = Path(os.environ.get('CORE_HOME') or (Path.home() / '.dsh' / 'agent-core'))

# 引擎自己的家，跟 agent-core 分开；sessions 在这下面
DSH_HOME = Path(os.environ.get('DSH_HOME') or (Path.home() / '.dsh'))

# ---------------------------------------------------------------- 记忆区
MEMORY = HOME / 'memory'
MEM = MEMORY  # core.py 里的旧名字
FLOW = MEMORY / '流水'
PENDING = MEMORY / '待记'
DIGEST = MEMORY / '日报'
STATE = MEMORY / '状态.json'
FACTS = MEMORY / '事实.jsonl'
DB = MEMORY / '索引.db'
OUT = MEMORY / '常驻.md'
LOCK = MEMORY / '.core.lock'
CONSUMED = PENDING / '.consumed.json'

# ---------------------------------------------------------------- 运行区
RUNTIME = HOME / '运行'
RUN = RUNTIME  # core.py / recall.py 里的旧名字
REVIEW = RUNTIME / '待审'
PLANS = RUNTIME / '计划'
LESSONS = RUNTIME / '教训.md'
LEDGER = RUNTIME / '派单台账'
LEDGER_ARCHIVE = LEDGER / '归档'
HEALTH = HOME / 'health.json'

# ---------------------------------------------------------------- 队列
QUEUE = HOME / 'queue'
Q = QUEUE  # queue.py / dispatch.py 里的旧名字
INBOX = QUEUE / 'inbox'
RUNNING = QUEUE / 'running'
DONE = QUEUE / 'done'
STUCK = QUEUE / 'stuck'

# ---------------------------------------------------------------- 会话与导出
SESSIONS = DSH_HOME / 'sessions'
EXPORT_ROOT = Path(os.environ.get('EXPORT_ROOT') or os.environ.get('CORE_EXPORT')
                   or (Path.home() / '.dsh' / 'exports'))
