#!/usr/bin/env bash
# 一键安装：建目录、拷工具和插件、检查 profile、跑语法与体检自检。
# 只写 $DSH_HOME / $CORE_HOME / $DSH_PLUGIN_DIR 下的东西，不改 profile。
#
# 用法：
#   bash install.sh [--dsh-home DIR] [--core-home DIR] [--plugin-dir DIR]
#                   [--profile NAME] [--workspace DIR]
#
# 也可以先用环境变量给默认值，命令行选项优先。
set -euo pipefail

trap 'rc=$?; printf "安装失败（exit %s）：第 %s 行，命令：%s\n" "$rc" "$LINENO" "$BASH_COMMAND" >&2; exit "$rc"' ERR

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

ARG_DSH_HOME=""
ARG_CORE_HOME=""
ARG_PLUGIN_DIR=""
ARG_PROFILE=""
ARG_WORKSPACE=""

usage() {
  cat <<'EOF'
用法：bash install.sh [选项]

  --dsh-home DIR    DSH 根目录（默认 $DSH_HOME，再默认 $HOME/.dsh）
  --core-home DIR   账本根（默认 $CORE_HOME，再默认 $DSH_HOME/agent-core）
  --plugin-dir DIR  插件安装根（默认 $DSH_PLUGIN_DIR，再默认 $DSH_HOME/plugins）
  --profile NAME    profile 名（默认 $DSH_PROFILE，再取 profiles/ 下第一个，最后 web）
  --workspace DIR   工作目录（默认 $DSH_WORKSPACE，再默认当前目录）
  -h, --help        显示这段
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dsh-home)    ARG_DSH_HOME="${2:?--dsh-home 需要一个目录}"; shift 2 ;;
    --core-home)   ARG_CORE_HOME="${2:?--core-home 需要一个目录}"; shift 2 ;;
    --plugin-dir)  ARG_PLUGIN_DIR="${2:?--plugin-dir 需要一个目录}"; shift 2 ;;
    --profile)     ARG_PROFILE="${2:?--profile 需要一个名字}"; shift 2 ;;
    --workspace)   ARG_WORKSPACE="${2:?--workspace 需要一个目录}"; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

DSH_HOME="${ARG_DSH_HOME:-${DSH_HOME:-$HOME/.dsh}}"
CORE_HOME="${ARG_CORE_HOME:-${CORE_HOME:-$DSH_HOME/agent-core}}"
DSH_PLUGIN_DIR="${ARG_PLUGIN_DIR:-${DSH_PLUGIN_DIR:-$DSH_HOME/plugins}}"
WORKSPACE="${ARG_WORKSPACE:-${DSH_WORKSPACE:-${WORKSPACE:-$PWD}}}"

if [ -z "$ARG_PROFILE" ]; then
  if [ -n "${DSH_PROFILE:-}" ]; then
    PROFILE="$DSH_PROFILE"
  elif [ -d "$DSH_HOME/profiles" ]; then
    PROFILE=$(find "$DSH_HOME/profiles" -mindepth 1 -maxdepth 1 -type d 2>/dev/null \
      | sort | head -1 | xargs -r basename)
    PROFILE="${PROFILE:-web}"
  else
    PROFILE=web
  fi
else
  PROFILE="$ARG_PROFILE"
fi

export DSH_HOME CORE_HOME DSH_PLUGIN_DIR
export DSH_WORKSPACE="$WORKSPACE"

echo "仓库    $ROOT"
echo "DSH     $DSH_HOME"
echo "账本    $CORE_HOME"
echo "插件    $DSH_PLUGIN_DIR"
echo "profile $PROFILE"
echo "工作区  $WORKSPACE"
echo

# ---------------------------------------------------------------- 前提
command -v python3 >/dev/null 2>&1 || { echo "缺 python3（需要 3.10 以上）" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' || {
  echo "Python 版本太低：$(python3 -V 2>&1)，需要 3.10 以上" >&2
  exit 1
}
python3 -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")' || {
  echo "Python 的 sqlite3 没有 FTS5，索引建不起来" >&2
  exit 1
}

command -v node >/dev/null 2>&1 || { echo "缺 node（需要 Node 24+）" >&2; exit 1; }
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
if [ "$NODE_MAJOR" -lt 24 ]; then
  echo "Node 版本太低：$(node -v)，需要 Node 24+" >&2
  exit 1
fi

if command -v dsh >/dev/null 2>&1; then
  DSH_VERSION=$(dsh --version 2>/dev/null | head -1 || true)
  echo "DSH     ${DSH_VERSION:-未知}"
  case "$DSH_VERSION" in
    0.2.*) ;;
    *) echo "提示：这套按 DSH 0.2.2-rc.1 一代写，当前版本 ${DSH_VERSION:-未知}，插件 API 可能对不上。" ;;
  esac
else
  echo "提示：没找到 dsh 命令。先把 DSH 装好，再回来挂插件。"
fi

# ---------------------------------------------------------------- 目录
mkdir -p \
  "$CORE_HOME/bin" \
  "$CORE_HOME/memory/流水" "$CORE_HOME/memory/待记" "$CORE_HOME/memory/日报" \
  "$CORE_HOME/运行/手册" "$CORE_HOME/运行/待审" "$CORE_HOME/运行/计划" \
  "$CORE_HOME/运行/报告" "$CORE_HOME/运行/派单台账" \
  "$CORE_HOME/queue/inbox" "$CORE_HOME/queue/running" \
  "$CORE_HOME/queue/done" "$CORE_HOME/queue/stuck" \
  "$DSH_HOME/exports" "$DSH_PLUGIN_DIR"

# ---------------------------------------------------------------- 工具
cp "$ROOT/tools/"*.py "$CORE_HOME/bin/"
if [ -f "$ROOT/tools/unzstd.mjs" ]; then
  cp "$ROOT/tools/unzstd.mjs" "$CORE_HOME/bin/"
fi
chmod +x "$CORE_HOME/bin/"*.py
echo "工具    $(find "$CORE_HOME/bin" -maxdepth 1 -name '*.py' | wc -l) 个脚本 -> $CORE_HOME/bin"

# ---------------------------------------------------------------- 插件
for name in guard ctx-guard delegate; do
  src="$ROOT/plugins/$name"
  dst="$DSH_PLUGIN_DIR/$name-plugin"
  [ -d "$src" ] || { echo "缺插件源目录：$src" >&2; exit 1; }
  mkdir -p "$dst"
  cp "$src/index.js" "$src/package.json" "$dst/"
  [ -f "$src/cordis.patch.yml" ] && cp "$src/cordis.patch.yml" "$dst/"
  [ -f "$src/tone.js" ] && cp "$src/tone.js" "$dst/"
  echo "插件    $name -> $dst"
done

# ---------------------------------------------------------------- profile
PROFILE_DIR="$DSH_HOME/profiles/$PROFILE"
PKG="$PROFILE_DIR/package.json"
PATCH="$PROFILE_DIR/cordis.patch.yml"
python3 - "$PKG" "$PATCH" "$DSH_PLUGIN_DIR" <<'PY'
import json
import os
import sys

pkg_path, patch_path, plugin_root = sys.argv[1], sys.argv[2], sys.argv[3]
names = ('guard', 'ctx-guard', 'delegate')
packages = {'guard': 'dsh-guard', 'ctx-guard': 'dsh-ctx-guard', 'delegate': 'dsh-delegate'}
expected = {n: os.path.join(plugin_root, n + '-plugin') for n in names}

pkg = None
if os.path.isfile(pkg_path):
    try:
        with open(pkg_path, encoding='utf-8') as f:
            pkg = json.load(f)
    except Exception as e:
        print(f'profile 读不了：{pkg_path}（{e}）', file=sys.stderr)
        sys.exit(1)

patch_text = ''
if os.path.isfile(patch_path):
    try:
        with open(patch_path, encoding='utf-8') as f:
            patch_text = f.read()
    except Exception:
        patch_text = ''

deps = (pkg or {}).get('dependencies') or {}
bundles = (((pkg or {}).get('dsh') or {}).get('profile') or {}).get('bundles') or []
missing_deps, missing_mount, doubled = [], [], []


def dep_target(val):
    """file: 依赖相对 package.json 所在目录解析，和 npm/pnpm 一致。"""
    p = val[5:]
    if not os.path.isabs(p):
        p = os.path.join(os.path.dirname(os.path.abspath(pkg_path)), p)
    return os.path.realpath(p)


for n in names:
    p = packages[n]
    val = str(deps.get(p) or '')
    if not val.startswith('file:'):
        missing_deps.append((p, expected[n]))
    elif dep_target(val) != os.path.realpath(expected[n]):
        missing_deps.append((p, expected[n]))
    in_bundles = p in bundles
    in_patch = p in patch_text
    if not (in_bundles or in_patch):
        missing_mount.append(p)
    if in_bundles and in_patch and n in ('guard', 'ctx-guard'):
        doubled.append(p)

if pkg is None:
    print(f'profile 检查：没找到 {pkg_path}，装好 profile 后再挂。')
elif not missing_deps and not missing_mount and not doubled:
    print(f'profile 检查：{pkg_path} 三个插件都挂好了')
else:
    print(f'profile 检查：{pkg_path}')
    if missing_deps:
        print('  缺 file: 依赖：' + '、'.join(p for p, _ in missing_deps))
    if missing_mount:
        print('  没挂载：' + '、'.join(missing_mount))
    if doubled:
        print('  同时出现在 bundles 和 insert 里（会加载两次）：' + '、'.join(doubled))

if pkg is None or missing_deps or missing_mount or doubled:
    print()
    print('需要手工加这几行（脚本不自动改 profile）：')
    print()
    print('--- package.json：dependencies 加这三项，bundles 加前两个 ---')
    snippet = {
        'dependencies': {packages[n]: 'file:' + expected[n] for n in names},
        'dsh': {'profile': {'bundles': ['dsh-guard', 'dsh-ctx-guard']}},
    }
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    print()
    print('--- delegate 没有 bundle patch，还要在 cordis.patch.yml 里加 ---')
    print('- insert:')
    print('    - id: delegate')
    print('      name: dsh-delegate')
    print()
    print('--- 或者不用 bundles，三个都写进 cordis.patch.yml ---')
    print('- insert:')
    for n in names:
        print(f'    - id: {n}')
        print(f'      name: {packages[n]}')
    print()
    print('两种挂法选一种。guard、ctx-guard 不要同时在 bundles 和 insert 里出现。')
PY

# ---------------------------------------------------------------- 自检
echo
echo "== 语法自检 =="
python3 -m py_compile "$CORE_HOME/bin/"*.py
echo "python  py_compile $(find "$CORE_HOME/bin" -maxdepth 1 -name '*.py' | wc -l) 个脚本通过"
for js in "$DSH_PLUGIN_DIR"/*-plugin/*.js; do
  [ -f "$js" ] || continue
  node --check "$js"
  echo "node    --check ${js#"$DSH_PLUGIN_DIR"/} 通过"
done

echo
echo "== 体检 =="
python3 "$CORE_HOME/bin/core.py" init >/dev/null

if python3 "$CORE_HOME/bin/core.py" doctor; then
  DOCTOR_RC=0
else
  DOCTOR_RC=$?
fi

if [ "$DOCTOR_RC" -eq 0 ]; then
  echo "doctor 全绿。"
elif [ "$DOCTOR_RC" -eq 1 ]; then
  cat <<EOF

doctor 有红项或黄项。新装环境这是预期的：
  · 宪章/身份/规则、AGENTS 挂钩、健康表、本机快照，要等你放好 charter.md、identity.md、
    policy.json，把 docs/prompt-rules.md 追加到 \$DSH_HOME/AGENTS.md，并跑过一轮 dream。
  · 图谱一致性要 \$CORE_HOME/运行/图谱.md、\$CORE_HOME/运行/手册/整理.md（含「## dream 十三步」
    且步骤 ≥13）、\$DSH_HOME/storages/schedule.json 里至少 2 条 active 任务；缺了就是黄灯，不阻断。

装好后先建图谱和手册、建两条定时任务，再跑一次：
  python3 "\$CORE_HOME/bin/core.py" doctor
EOF
else
  echo "doctor 退出码 $DOCTOR_RC，不是 0/1，停下。" >&2
  exit "$DOCTOR_RC"
fi

echo
echo "安装完成。"
echo "  DSH_HOME       $DSH_HOME"
echo "  CORE_HOME      $CORE_HOME"
echo "  DSH_PLUGIN_DIR $DSH_PLUGIN_DIR"
echo "  profile        $PROFILE"
echo "下一步：按 docs/porting.md 挂 profile、建图谱和手册，再跑一次 doctor。"
