#!/usr/bin/env bash
# 把实时版改回仓库：拷贝、脱敏、泄露扫描、语法自检、本地提交。
# 绝不 push。推送要人工看过 diff 再执行。
#
# 用法：
#   bash tools/sync-repo.sh [--dry-run] [--live-plugin-dir DIR]
#
# 实时路径默认从环境变量推：
#   DSH_HOME          DSH 根，默认 $HOME/.dsh
#   CORE_HOME         账本根，默认 $DSH_HOME/agent-core
#   LIVE_PLUGIN_DIR   实时插件根，默认自动找 $DSH_HOME 下的 guard-plugin
set -euo pipefail

trap 'rc=$?; printf "同步失败（exit %s）：第 %s 行，命令：%s\n" "$rc" "$LINENO" "$BASH_COMMAND" >&2; exit "$rc"' ERR

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
CORE_HOME="${CORE_HOME:-$DSH_HOME/agent-core}"
LIVE_PLUGIN_DIR="${LIVE_PLUGIN_DIR:-}"
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --live-plugin-dir) LIVE_PLUGIN_DIR="${2:?--live-plugin-dir 需要一个目录}"; shift 2 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [ -z "$LIVE_PLUGIN_DIR" ]; then
  if [ -d "$DSH_HOME/plugins/guard-plugin" ]; then
    LIVE_PLUGIN_DIR="$DSH_HOME/plugins"
  else
    GUARD_DIR=$(find "$DSH_HOME" -maxdepth 2 -type d -name guard-plugin 2>/dev/null | head -1 || true)
    if [ -n "$GUARD_DIR" ]; then
      LIVE_PLUGIN_DIR=$(dirname "$GUARD_DIR")
    fi
  fi
fi
[ -n "$LIVE_PLUGIN_DIR" ] || {
  echo "找不到实时插件目录。用 --live-plugin-dir 指定，或设 DSH_HOME。" >&2
  exit 1
}

# 泄露扫描名单。拆成片段拼，避免脚本自己命中自己。
SCAN_RE=$(
  printf '%s' \
    '赖' '敏|' \
    '老' '丁|' \
    '丁' '一' '舟|' \
    '小' '敏|' \
    '孟' '姐|' \
    '113' '\.' '46|' \
    'tiang' 'ong|' \
    '天' '宫|' \
    '/root' '/\.dsh|' \
    '/sd' 'card|' \
    '地' '坛|' \
    '荣' '耀|' \
    'ghp' '_'
)

echo "仓库    $ROOT"
echo "实时    $CORE_HOME"
echo "插件    $LIVE_PLUGIN_DIR"
[ "$DRY_RUN" -eq 1 ] && echo "模式    dry-run（不写仓库、不提交）"
echo

python3 - "$ROOT" "$CORE_HOME" "$LIVE_PLUGIN_DIR" "$DRY_RUN" "$SCAN_RE" <<'PY'
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
CORE = Path(sys.argv[2])
PLUG = Path(sys.argv[3])
DRY = sys.argv[4] == '1'
SCAN_RE = sys.argv[5]

# 敏感字面量拆开拼，避免本脚本被自己的扫描命中。
L = '/root' + '/.dsh'
W = '/root' + '/workspace'
S = '/sdc' + 'ard'
T = 'tiang' + 'ong'
TC = '天' + '宫'
D = '地' + '坛'
N = '老' + '丁'
P = 'dit' + 'ang'
X = 'xia' + 'omin'
H = '~' + '/.dsh'
I = '113' + '.' + '46' + '.190.14'

TOKENS = {'@L@': L, '@W@': W, '@S@': S, '@T@': T, '@TC@': TC,
          '@D@': D, '@N@': N, '@P@': P, '@X@': X, '@H@': H, '@IP@': I}


def x(s):
    for k, v in TOKENS.items():
        s = s.replace(k, v)
    return s


CORE_RULES = [
    ('header', "HOME = Path(os.environ.get('CORE_HOME', str(Path.home() / '.dsh' / 'agent-core')))",
     "DSH_HOME = Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh')))\n"
     "HOME = Path(os.environ.get('CORE_HOME', str(DSH_HOME / 'agent-core')))\n"
     "PLUGIN_HOME = Path(os.environ.get('DSH_PLUGIN_DIR', str(DSH_HOME / 'plugins')))"),
    ('export', "EXPORT_ROOT = Path(os.environ.get('CORE_EXPORT', '@S@/Download/DSH-Go'))",
     "EXPORT_ROOT = Path(os.environ.get('CORE_EXPORT', str(DSH_HOME / 'exports')))"),
    ('agents', "    ag = Path.home() / '.dsh' / 'AGENTS.md'",
     "    ag = DSH_HOME / 'AGENTS.md'"),
    ('doc', '"""把整本账导到 @S@/Download/DSH-Go/agent-core-latest，并留当天 tar.gz（最多 7 份）。"""',
     '"""把整本账导到 $DSH_HOME/exports/agent-core-latest，并留当天 tar.gz（最多 7 份）。"""'),
    ('snapcomment', "    # 只查本机 @S@ 的 tar，@TC@没有副本，不是异地备份",
     "    # 只查本机导出目录的 tar；异地副本要另配，这里不假装有"),
    ('snapdetail', "        f'{tars[-1].name} · 8 天内；@TC@无副本' if snap_ok",
     "        f'{tars[-1].name} · 8 天内；无异地副本' if snap_ok"),
    ('schedule', "        sch = json.loads((Path.home() / '.dsh' / 'storages' / 'schedule.json')\n"
                 "                         .read_text(encoding='utf-8'))",
     "        sch = json.loads((DSH_HOME / 'storages' / 'schedule.json').read_text(encoding='utf-8'))"),
    ('pluginblock', "    dt = Path.home() / '.dsh' / '@P@'\n"
                    "    for name in ('guard-plugin', 'ctx-guard-plugin', 'delegate-plugin'):\n"
                    "        if not (dt / name).is_dir():\n"
                    "            gbad.append(f'缺 {dt / name}')\n"
                    "    if not (dt / 'ctx-guard-plugin' / 'tone.js').exists():\n"
                    "        gbad.append(f'缺 {dt / \"ctx-guard-plugin\" / \"tone.js\"}')\n"
                    "    chk('图谱一致性', not gbad, '；'.join(gbad) if gbad else '图谱一致')",
     "    # 安装脚本用 <name>-plugin 目录；直接拷 plugins/ 的用 <name>，两种都认\n"
     "    for name in ('guard', 'ctx-guard', 'delegate'):\n"
     "        d = PLUGIN_HOME / name\n"
     "        if not d.is_dir():\n"
     "            d = PLUGIN_HOME / (name + '-plugin')\n"
     "        if not d.is_dir():\n"
     "            gbad.append(f'缺 {PLUGIN_HOME / name} 或 {PLUGIN_HOME / (name + \"-plugin\")}')\n"
     "    tone = PLUGIN_HOME / 'ctx-guard' / 'tone.js'\n"
     "    if not tone.exists():\n"
     "        tone = PLUGIN_HOME / 'ctx-guard-plugin' / 'tone.js'\n"
     "    if not tone.exists():\n"
     "        gbad.append(f'缺 {tone}')\n"
     "    # 图谱和手册随部署走，仓库里不带；缺了只亮黄，不阻断整轮\n"
     "    chk('图谱一致性', not gbad, '；'.join(gbad) if gbad else '图谱一致', warn=True)"),
    ('factsuffix', "FACT_SUBJ_SUFFIX = ('机器', '手机', '平板', '电脑', '引擎', '系统', '服务器', '@TC@',",
     "FACT_SUBJ_SUFFIX = ('机器', '手机', '平板', '电脑', '引擎', '系统', '服务器',"),
    ('factwho', "    if s in ('我', '你', '他', '她', '@N@'):",
     "    if s in ('我', '你', '他', '她'):"),
    ('factloc', "    return bool(re.search(r'/|里|上|中|下|盘|云|@TC@|手机|本机|机器|目录|服务器', v))",
     "    return bool(re.search(r'/|里|上|中|下|盘|云|手机|本机|机器|目录|服务器', v))"),
]

# 实时 ledger.py 还是老的单行账本根；仓库版要两行，让 DSH_HOME 可配。
LEDGER_RULES = [
    ('header', "HOME = Path(os.environ.get('CORE_HOME', str(Path.home() / '.dsh' / 'agent-core')))",
     "DSH_HOME = Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh')))\n"
     "HOME = Path(os.environ.get('CORE_HOME', str(DSH_HOME / 'agent-core')))"),
]

# 实时 voice.py 把远端主机、私钥、导出目录写死；仓库版改成环境变量读，TTS 流程不动。
VOICE_RULES = [
    ('title', '"""voice.py — 借@TC@的嗓子说话。',
     '"""voice.py — 调远端 TTS 服务说话。'),
    ('doc', '@TC@ dsh-tts 只监听 127.0.0.1:3122，从手机走 SSH 去要：\n'
            '  GET /tts?text=...&voice=zh-CN-XiaoxiaoNeural  -> audio/mpeg\n'
            '\n'
            '用法：\n'
            '  voice.py say "文本" [--voice zh-CN-XiaoxiaoNeural] [--out DIR]\n'
            '产物落 @S@/Download/DSH-Go/voice/，文件名带时间。\n',
     '远端 dsh-tts 只监听 127.0.0.1:3122，本机走 SSH 去要：\n'
     '  GET /tts?text=...&voice=zh-CN-XiaoxiaoNeural  -> audio/mpeg\n'
     '\n'
     '用法：\n'
     '  voice.py say "文本" [--voice zh-CN-XiaoxiaoNeural] [--out DIR]\n'
     '产物默认落 $DSH_HOME/exports/voice/，文件名带时间。\n'
     '\n'
     '远端连接从环境变量读：\n'
     '  VOICE_SSH_HOST   ssh 目标，如 user@host\n'
     '  VOICE_SSH_KEY    私钥路径（可选，不填走 ssh-agent）\n'
     '  VOICE_OUT        产物目录，默认 $DSH_HOME/exports/voice\n'),
    ('imports', "import argparse\nimport subprocess",
     "import argparse\nimport os\nimport subprocess"),
    ('config', "KEY = '@L@/private/@T@.pem'\n"
               "HOST = 'root@@IP@'\n"
               "OUT = Path('@S@/Download/DSH-Go/voice')",
     "DSH_HOME = Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh')))\n"
     "HOST = os.environ.get('VOICE_SSH_HOST', '').strip()\n"
     "KEY = os.environ.get('VOICE_SSH_KEY', '').strip()\n"
     "OUT = Path(os.environ.get('VOICE_OUT', str(DSH_HOME / 'exports' / 'voice')))"),
    ('hostcheck', "    if a.cmd != 'say':\n        return 2\n",
     "    if a.cmd != 'say':\n        return 2\n"
     "    if not HOST:\n"
     "        print('没配 VOICE_SSH_HOST（如 user@host），合成不了。')\n"
     "        return 2\n"),
    ('ssh', "    r = subprocess.run(\n"
            "        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-i', KEY, HOST,\n"
            "         f'curl -s -m 90 \"{url}\"'],\n"
            "        capture_output=True, timeout=120)",
     "    argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']\n"
     "    if KEY:\n"
     "        argv += ['-i', KEY]\n"
     "    r = subprocess.run(\n"
     "        argv + [HOST, f'curl -s -m 90 \"{url}\"'],\n"
     "        capture_output=True, timeout=120)"),
]


RECALL_RULES = [
    ('usersubj', "MAX_RESUME = 600\n",
     "MAX_RESUME = 600\n\n"
     "# 「关于用户」认这些主体名。默认只认「用户」；部署时用 USER_SUBJECTS 补人名，逗号分隔。\n"
     "USER_SUBJECTS = ('用户',) + tuple(\n"
     "    x.strip() for x in os.environ.get('USER_SUBJECTS', '').split(',') if x.strip())\n"),
    ('rename', '「@D@整理」', '「每日整理」'),
    ('subject', "        if domain != '用户' and subject not in ('用户', '@N@') and '用户' not in subject:",
     "        if domain != '用户' and subject not in USER_SUBJECTS and '用户' not in subject:"),
]

GUARD_RULES = [
    ('comment', '/** @P@-guard: 顶层会话不直接动手', '/** guard: 顶层会话不直接动手'),
    ('name', "export const name = 'dsh-@P@-guard'", "export const name = 'dsh-guard'"),
    ('homedir', "import { appendFileSync, mkdirSync, realpathSync } from 'node:fs'\n"
                "import { basename, dirname, join, resolve } from 'node:path'",
     "import { appendFileSync, mkdirSync, realpathSync } from 'node:fs'\n"
     "import { homedir } from 'node:os'\n"
     "import { basename, dirname, join, resolve } from 'node:path'"),
    ('docs', "const DOC_DIRS = ['@L@/agent-core/运行', '/tmp']",
     "/** 账本根。每次调用现读环境变量，测试可以指到临时目录，不碰真账本。 */\n"
     "function coreHome() {\n"
     "  return process.env.CORE_HOME || join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'agent-core')\n"
     "}\n\n"
     "const DOC_DIRS = [join(coreHome(), '运行'), '/tmp']"),
    ('home', "const home = process.env.CORE_HOME || '@L@/agent-core'",
     "const home = coreHome()"),
]

CTX_RULES = [
    ('homedir', "import { createUserMessage } from '@deepseek-ai/dsh-llm'\n"
                "import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, statfsSync, statSync } from 'node:fs'\n"
                "import { join } from 'node:path'",
     "import { createUserMessage } from '@deepseek-ai/dsh-llm'\n"
     "import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, statfsSync, statSync } from 'node:fs'\n"
     "import { homedir } from 'node:os'\n"
     "import { join } from 'node:path'"),
    ('homes', "export const inject = ['tokenMeter']\n",
     "export const inject = ['tokenMeter']\n\n"
     "const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')\n"
     "const CORE_HOME = process.env.CORE_HOME || join(DSH_HOME, 'agent-core')\n"),
    ('sessions', "const SESSIONS_DIR = '@L@/sessions/--root-workspace--'",
     "// 宿主按工作目录给 sessions 再加一层；换了工作目录就设 DSH_SESSIONS_DIR 指过去\n"
     "const SESSIONS_DIR = process.env.DSH_SESSIONS_DIR || join(DSH_HOME, 'sessions', '--root-workspace--')"),
    ('health', "const HEALTH_PATH = '@L@/agent-core/health.json'",
     "const HEALTH_PATH = join(CORE_HOME, 'health.json')"),
    ('proj', "const PROJCACHE_DIR = '@L@/storages/session_projcache/sessions'",
     "const PROJCACHE_DIR = join(DSH_HOME, 'storages', 'session_projcache', 'sessions')"),
    ('dream', 'python3 @H@/agent-core/bin/core.py dream', 'python3 $DSH_HOME/agent-core/bin/core.py dream'),
]

DELEGATE_RULES = [
    ('comment', '/** @X@-delegate:', '/** dsh-delegate:'),
    ('homedir', "import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'\n"
                "import { join } from 'node:path'",
     "import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'\n"
     "import { homedir } from 'node:os'\n"
     "import { join } from 'node:path'"),
    ('home', "const DSH_HOME = process.env.DSH_HOME || '@L@'",
     "const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')"),
    ('cwd', "const DEFAULT_CWD = '@W@'", "const DEFAULT_CWD = process.env.DSH_WORKSPACE || process.cwd()"),
    ('target1', "// dispatch.py 投给@TC@的单：target=@T@，或带 cmd。delegate 不接，留在 inbox 给 dispatch.py。",
     "// dispatch.py 投给远端的单：target=remote，或带 cmd。delegate 不接，留在 inbox 给 dispatch.py。"),
    ('target2', "if (rec.target === '@T@') return true", "if (rec.target === 'remote') return true"),
    ('target3', '// 先判归属，再 rename：@TC@的单原样留在 inbox，dispatch.py 还能捡',
     '// 先判归属，再 rename：远端的单原样留在 inbox，dispatch.py 还能捡'),
]

TONE_RULES = [
    ('imports', "import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'\n"
                "import { dirname } from 'node:path'",
     "import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'\n"
     "import { homedir } from 'node:os'\n"
     "import { dirname, join } from 'node:path'"),
    ('log', "export const TONE_LOG = '@L@/agent-core/运行/口吻自检.jsonl'",
     "const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')\n"
     "const HOME = process.env.CORE_HOME || join(DSH_HOME, 'agent-core')\n"
     "export const TONE_LOG = process.env.TONE_LOG || join(HOME, '运行', '口吻自检.jsonl')"),
]

MAP = [
    ('plugins/guard/index.js', PLUG / 'guard-plugin' / 'index.js', GUARD_RULES),
    ('plugins/ctx-guard/index.js', PLUG / 'ctx-guard-plugin' / 'index.js', CTX_RULES),
    ('plugins/ctx-guard/tone.js', PLUG / 'ctx-guard-plugin' / 'tone.js', TONE_RULES),
    ('plugins/delegate/index.js', PLUG / 'delegate-plugin' / 'index.js', DELEGATE_RULES),
    ('tools/core.py', CORE / 'bin' / 'core.py', CORE_RULES),
    ('tools/recall.py', CORE / 'bin' / 'recall.py', RECALL_RULES),
    ('tools/ledger.py', CORE / 'bin' / 'ledger.py', LEDGER_RULES),
    ('tools/voice.py', CORE / 'bin' / 'voice.py', VOICE_RULES),
]

FALLBACK = [('@TC@', '远端'), ('@T@', 'remote'), ('@D@', '每日'), ('@N@', '用户'),
            ('@P@', 'plugins'), ('@X@', 'dsh'), ('@H@', '$DSH_HOME'),
            ('@S@', '$DSH_HOME/exports'), ('@L@', '$DSH_HOME'), ('@W@', '$WORKSPACE')]


def sanitize(rel, text, misses):
    for label, old, new in next(r for r in MAP if r[0] == rel)[2]:
        old, new = x(old), x(new)
        if old not in text:
            misses.append(f'{rel}: {label}')
            continue
        text = text.replace(old, new)
    for token, repl in FALLBACK:
        text = text.replace(x(token), repl)
    return text


def leak_hits(base):
    hits = []
    rx = re.compile(SCAN_RE)
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', '__pycache__')]
        for fn in files:
            p = Path(root) / fn
            try:
                data = p.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(data.splitlines(), 1):
                if rx.search(line):
                    hits.append(f'{p.relative_to(base)}:{i}: {line.strip()[:160]}')
    return hits


def syntax_ok(base, rels):
    bad = []
    for rel in rels:
        p = base / rel
        if p.suffix == '.py':
            r = subprocess.run([sys.executable, '-m', 'py_compile', str(p)],
                               capture_output=True, text=True)
        elif p.suffix == '.js':
            node = shutil.which('node')
            if not node:
                return ['找不到 node，没法做 --check']
            r = subprocess.run([node, '--check', str(p)], capture_output=True, text=True)
        else:
            continue
        if r.returncode != 0:
            bad.append(f'{rel}: {(r.stderr or r.stdout).strip()[:300]}')
    return bad


missing_src = [str(src) for _, src, _ in MAP if not src.is_file()]
if missing_src:
    print('实时文件不存在，先确认路径：')
    for p in missing_src:
        print('  ' + p)
    sys.exit(1)

tmp = Path(tempfile.mkdtemp(prefix='sync-repo.'))
misses = []
changed = []
for rel, src, _ in MAP:
    text = sanitize(rel, src.read_text(encoding='utf-8'), misses)
    out = tmp / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding='utf-8')
    target = ROOT / rel
    if not target.exists() or target.read_text(encoding='utf-8') != text:
        changed.append(rel)

print('== 映射（实时 -> 仓库）==')
for rel, src, _ in MAP:
    mark = '有改动' if rel in changed else '无变化'
    print(f'  {src}  ->  {rel}  [{mark}]')

print()
print('== 脱敏 ==')
print(f'  规则命中检查：{len(misses)} 条未命中')
for m in misses:
    print('  ! ' + m)

print()
print('== 泄露扫描 ==')
for label, base in (('仓库', ROOT), ('待写入', tmp)):
    hits = leak_hits(base)
    print(f'  {label}：{len(hits)} 命中')
    for h in hits:
        print('  ! ' + h)
    if hits:
        print('有命中，停止；没有写仓库，也没有提交。', file=sys.stderr)
        sys.exit(2)

print()
print('== 语法自检 ==')
bad = syntax_ok(tmp, changed)
print(f'  {len(changed)} 个改动文件：{"通过" if not bad else "失败"}')
for b in bad:
    print('  ! ' + b)
if bad:
    print('语法不过，停止；没有写仓库，也没有提交。', file=sys.stderr)
    sys.exit(3)

if not changed:
    print()
    print('仓库和实时版一致，没有要提交的东西。')
    sys.exit(0)

print()
if DRY:
    print('[dry-run] 未写仓库、未提交。去掉 --dry-run 才会落盘。')
    sys.exit(0)

originals = {}
for rel in changed:
    target = ROOT / rel
    originals[rel] = target.read_bytes() if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tmp / rel, target)

# 落盘后再扫一遍，命中就回滚，不提交
hits = leak_hits(ROOT)
if hits:
    print('落盘后扫描有命中，回滚：', file=sys.stderr)
    for h in hits:
        print('  ! ' + h, file=sys.stderr)
    for rel, data in originals.items():
        target = ROOT / rel
        if data is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(data)
    print('已回滚，未提交。', file=sys.stderr)
    sys.exit(2)

subprocess.run(['git', '-C', str(ROOT), 'add', '-A'], check=True)
r = subprocess.run(['git', '-C', str(ROOT), 'diff', '--cached', '--quiet'])
if r.returncode == 0:
    print('没有暂存到改动，跳过提交。')
    sys.exit(0)

import datetime
day = datetime.date.today().isoformat()
msg = f'sync: {day} 回填 ' + '、'.join(changed)
subprocess.run(['git', '-C', str(ROOT), 'commit', '-m', msg], check=True)
print()
print('已本地提交，推送需人工确认后执行 git push')
PY
