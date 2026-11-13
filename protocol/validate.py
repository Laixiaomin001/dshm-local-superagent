#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate.py — 队列单字段与状态校验器。只依赖标准库（json/re/sys），不装 jsonschema。

  from validate import validate_order, validate_status_transition

  validate_order(obj)                 -> (bool, [错误])
  validate_order(obj, where='done')   顺带核对单里的 status 和所在目录是否一致
  validate_status_transition(old, new) -> (bool, 说明)

字段、必填、允许值、状态机全部从 queue.schema.json 读；这里只实现 draft-07 的
一个子集（type/required/properties/enum/const/pattern/minLength/maxLength/
minimum/maximum/allOf/if/then/else/additionalProperties），够校验这张单。

命令行：
  python3 validate.py <json文件>             校验一张单，退出码 0/1
  python3 validate.py <json文件> --from done 顺带校验 done -> 单里的 status
  python3 validate.py --schema               打印从 schema 读到的状态机
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / 'queue.schema.json'

# 找不到 schema 时的兜底，必须和 schema 一致；正常路径不读这里。
FALLBACK_STATUS = ('queued', 'running', 'done', 'failed', 'stale')
FALLBACK_TRANSITIONS = {
    'queued': ('running', 'failed'),
    'running': ('done', 'failed', 'stale', 'queued'),
    'done': ('queued',),
    'failed': ('queued',),
    'stale': ('queued',),
}

# 目录 -> 该目录里允许出现的 status（stuck 同时放 failed 和 stale）
DIR_ALLOWED = {
    'inbox': ('queued',),
    'running': ('running',),
    'done': ('done',),
    'stuck': ('failed', 'stale'),
}
DIR_INFER = {'inbox': 'queued', 'running': 'running', 'done': 'done', 'stuck': 'failed'}
DIR_MODES = tuple(DIR_ALLOWED)

# 合并前的旧字段名 -> 现名。读时兼容，写端只写现名。
ALIASES = {'sessionId': 'session', 'createdAt': 'created',
           'finishedAt': 'finished', 'output': 'result'}

TIME_FIELDS = ('created', 'started', 'heartbeat', 'finished',
               'accepted_at', 'rejected_at', 'createdAt', 'finishedAt')


def _load_schema() -> dict:
    try:
        return json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


SCHEMA = _load_schema()
_x = SCHEMA.get('x-state-machine') or {}
STATUS_VALUES = tuple((SCHEMA.get('properties', {}).get('status', {}) or {}).get('enum')
                      or FALLBACK_STATUS)
TRANSITIONS = ({k: tuple(v) for k, v in (_x.get('transitions') or {}).items()}
               or FALLBACK_TRANSITIONS)
INITIAL_STATUS = _x.get('initial') or 'queued'
KNOWN_FIELDS = tuple(SCHEMA.get('properties') or {})


# ---------- 基础判定 ----------

def _type_ok(t: str, v) -> bool:
    if t == 'object':
        return isinstance(v, dict)
    if t == 'array':
        return isinstance(v, list)
    if t == 'string':
        return isinstance(v, str)
    if t == 'boolean':
        return isinstance(v, bool)
    # bool 是 int 的子类，得排掉，否则 true 能混过 integer
    if t == 'integer':
        return isinstance(v, int) and not isinstance(v, bool)
    if t == 'number':
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == 'null':
        return v is None
    return True


def _join(path: str, key: str) -> str:
    return f'{path}.{key}' if path else key


def _check(node, val, path: str, errs: list) -> None:
    """按 draft-07 子集校验一个值，错误追加到 errs。"""
    if node is False:
        errs.append(f'{path or "单"}: 不允许出现这个字段')
        return
    if not isinstance(node, dict):
        return

    t = node.get('type')
    if t:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(x, val) for x in types):
            errs.append(f'{path or "单"}: 类型应为 {"|".join(types)}，实际 {type(val).__name__}')
            return

    if 'const' in node and val != node['const']:
        errs.append(f'{path or "单"}: 应为 {node["const"]!r}，实际 {val!r}')
    if 'enum' in node and val not in node['enum']:
        errs.append(f'{path or "单"}: 只能是 {node["enum"]}，实际 {val!r}')

    if isinstance(val, str):
        if 'minLength' in node and len(val) < node['minLength']:
            errs.append(f'{path}: 不能为空（最少 {node["minLength"]} 个字符）')
        if 'maxLength' in node and len(val) > node['maxLength']:
            errs.append(f'{path}: 长度 {len(val)} 超过上限 {node["maxLength"]}')
        if 'pattern' in node and re.search(node['pattern'], val) is None:
            errs.append(f'{path}: 格式不对，应匹配 {node["pattern"]}')

    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if 'minimum' in node and val < node['minimum']:
            errs.append(f'{path}: 不能小于 {node["minimum"]}，实际 {val}')
        if 'maximum' in node and val > node['maximum']:
            errs.append(f'{path}: 不能大于 {node["maximum"]}，实际 {val}')

    if isinstance(val, dict):
        for k in node.get('required', []):
            if k not in val:
                errs.append(f'{_join(path, k)}: 缺必填字段')
        props = node.get('properties') or {}
        for k, v in val.items():
            if k in props:
                _check(props[k], v, _join(path, k), errs)
            elif node.get('additionalProperties') is False:
                errs.append(f'{_join(path, k)}: 未知字段')

    if isinstance(val, list) and isinstance(node.get('items'), dict):
        for i, v in enumerate(val):
            _check(node['items'], v, f'{path}[{i}]', errs)

    for sub in node.get('allOf') or []:
        _check(sub, val, path, errs)

    if 'if' in node:
        probe: list = []
        _check(node['if'], val, path, probe)
        if not probe and 'then' in node:
            _check(node['then'], val, path, errs)
        elif probe and 'else' in node:
            _check(node['else'], val, path, errs)


def _errors(node, val) -> list:
    errs: list = []
    _check(node, val, '', errs)
    return errs


def _parse_time(v) -> bool:
    """ISO 时间要能解析且带时区；认 ...Z 和 +08:00 两种写法。"""
    if not isinstance(v, str) or not v.strip():
        return False
    s = v.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(s).tzinfo is not None
    except ValueError:
        return False


# ---------- 对外两个函数 ----------

def validate_order(obj, where=None, notes=None):
    """校验一张单。

    obj    单的 dict（原样，不会被改；内部用副本）
    where  可选，inbox/running/done/stuck；给了就核对 status 和目录是否一致，
           单里没有 status 时按目录推断（历史单兼容），推断过会记一条 warning
    notes  可选 list，warning 追加进去；不影响返回值

    返回 (bool, [错误])。notes 里的东西不是错误。
    """
    warn = notes if notes is not None else []
    if not isinstance(obj, dict):
        return False, ['不是单：顶层必须是 JSON 对象']

    rec = dict(obj)
    used = [k for k in ALIASES if k in obj]
    for old, new in ALIASES.items():
        if old in obj and new not in obj:
            rec[new] = obj[old]
    if used:
        warn.append('用了历史字段名 ' + '、'.join(f'{k}→{ALIASES[k]}' for k in used)
                    + '；读时兼容，写端只写新名')

    errs: list = []
    if where in DIR_MODES:
        if 'status' not in rec:
            rec['status'] = DIR_INFER[where]
            warn.append(f'单里没有 status，按所在目录 {where} 推断为 {rec["status"]}')
        elif rec['status'] not in DIR_ALLOWED[where]:
            errs.append(f'status: 单里是 {rec["status"]}，却在 {where} 目录'
                        f'（{where} 只放 {"、".join(DIR_ALLOWED[where])}）')

    errs.extend(_errors(SCHEMA, rec))

    for k in TIME_FIELDS:
        v = rec.get(k)
        if isinstance(v, str) and v and not _parse_time(v):
            errs.append(f'{k}: 不是合法时间（要带时区，或 Z 结尾）')

    if KNOWN_FIELDS:
        unknown = sorted(k for k in obj if k not in KNOWN_FIELDS)
        if unknown:
            warn.append('未知字段 ' + '、'.join(unknown) + '（schema 里没有，确认是不是拼错）')

    return (not errs), errs


def validate_status_transition(old, new):
    """校验一次状态迁移，返回 (bool, 说明)。说明为空串表示通过。

    old 为 None/空 视为首写，直接放行；old == new 是原地重写，也放行。
    """
    if new not in STATUS_VALUES:
        return False, f'未知状态 {new!r}；只允许 {"、".join(STATUS_VALUES)}'
    if not old:
        return True, ''
    if old not in STATUS_VALUES:
        return False, f'未知的旧状态 {old!r}；只允许 {"、".join(STATUS_VALUES)}'
    if old == new:
        return True, ''
    if new in TRANSITIONS.get(old, ()):
        return True, ''
    nxt = TRANSITIONS.get(old, ())
    return False, (f'{old} 不能直接变 {new}；{old} 只能变 '
                   + ('、'.join(nxt) if nxt else '（终态，没有下一步）'))


# ---------- 命令行 ----------

def _where_from_path(p: Path):
    for part in reversed(p.parts[:-1]):
        if part in DIR_ALLOWED:
            return part
    return None


def _looks_like_schema(obj) -> bool:
    return isinstance(obj, dict) and ('$schema' in obj
                                      or ('properties' in obj and 'type' in obj))


def main(argv: list) -> int:
    args = [a for a in argv[1:]]
    if not args or args[0] in ('-h', '--help'):
        print(__doc__.strip())
        return 0
    if args[0] == '--schema':
        print(f'状态：{" → ".join(STATUS_VALUES)}（起始 {INITIAL_STATUS}）')
        for k, v in TRANSITIONS.items():
            print(f'  {k} → {", ".join(v) if v else "（终态）"}')
        print(f'schema：{SCHEMA_PATH}（{len(KNOWN_FIELDS)} 个字段）')
        return 0

    path = Path(args[0])
    old = ''
    if '--from' in args:
        i = args.index('--from')
        old = args[i + 1] if i + 1 < len(args) else ''

    if not path.exists():
        print(f'✗ {path}  文件不存在')
        return 1
    try:
        raw = path.read_text(encoding='utf-8')
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f'✗ {path}  不是合法 JSON：{e}')
        return 1
    except Exception as e:
        print(f'✗ {path}  读不出来：{e}')
        return 1

    if _looks_like_schema(obj):
        print(f'✗ {path}  不是单：这是 JSON Schema（{"draft-07" if "$schema" in obj else "带 properties"}），'
              '不是队列单')
        return 1
    if not isinstance(obj, dict) or not any(k in obj for k in ('id', 'title', 'task')):
        print(f'✗ {path}  不是单：没有 id/title/task 这些单字段')
        return 1

    where = _where_from_path(path)
    notes: list = []
    ok, errs = validate_order(obj, where=where, notes=notes)
    if old:
        tok, why = validate_status_transition(old, obj.get('status'))
        if not tok:
            errs.append(f'状态迁移不合法：{why}')
        else:
            notes.append(f'状态迁移 {old} → {obj.get("status")} 允许')

    used = len(obj)
    mark = '✓' if ok and not errs else '✗'
    shown = obj.get('status')
    if shown is None and where in DIR_ALLOWED:
        shown = DIR_INFER[where] + '（按目录推断）'
    print(f'{mark} {path}  status={shown or "?"}  '
          f'字段 {used}/{len(KNOWN_FIELDS) or "?"}'
          + (f'  目录={where}' if where else ''))
    for n in notes:
        print(f'  warn: {n}')
    for e in errs:
        print(f'  - {e}')
    if errs:
        print(f'结论：不合规，{len(errs)} 个错误')
        return 1
    print('结论：合协议')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
