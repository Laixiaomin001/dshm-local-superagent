# lib — 公共路径、时间、文件读写

`tools/lib/` 是脚本的公共层：路径、时间、原子写各一份实现，别在每个脚本开头再抄一遍。三个模块只用标准库，不 import 引擎或第三方包。

## 怎么用

`tools/` 是脚本目录，本来就在 `sys.path` 里，脚本里直接：

```python
from lib.paths import HOME, FLOW, QUEUE
from lib.timeutil import local_iso, parse_iso
from lib.io import atomic_write, read_text, read_json, write_json
```

从别的目录调脚本也行（`sys.path[0]` 是脚本所在目录）：

```bash
cd /tmp && python3 "$CORE_HOME/bin/ctx.py"
```

## paths.py

所有账本路径从 `CORE_HOME` 展开；引擎的会话目录从 `DSH_HOME` 展开。两个变量分开，别互相推。

| 变量 | 值 | 说明 |
|---|---|---|
| `HOME` | `CORE_HOME` 或 `~/.dsh/agent-core` | 引擎根 |
| `DSH_HOME` | `DSH_HOME` 或 `~/.dsh` | 引擎自己的家 |
| `MEMORY` / `MEM` | `HOME/memory` | `MEM` 是旧名字 |
| `FLOW` | `MEMORY/流水` | |
| `PENDING` | `MEMORY/待记` | |
| `DIGEST` | `MEMORY/日报` | |
| `STATE` | `MEMORY/状态.json` | |
| `FACTS` | `MEMORY/事实.jsonl` | |
| `DB` | `MEMORY/索引.db` | |
| `OUT` | `MEMORY/常驻.md` | |
| `LOCK` | `MEMORY/.core.lock` | |
| `CONSUMED` | `PENDING/.consumed.json` | |
| `RUNTIME` / `RUN` | `HOME/运行` | `RUN` 是旧名字 |
| `REVIEW` | `RUNTIME/待审` | |
| `PLANS` | `RUNTIME/计划` | |
| `LESSONS` | `RUNTIME/教训.md` | |
| `LEDGER` | `RUNTIME/派单台账` | |
| `LEDGER_ARCHIVE` | `LEDGER/归档` | |
| `HEALTH` | `HOME/health.json` | |
| `QUEUE` / `Q` | `HOME/queue` | `Q` 是 queue/dispatch 的旧名字 |
| `INBOX` `RUNNING` `DONE` `STUCK` | `QUEUE/*` | |
| `SESSIONS` | `DSH_HOME/sessions` | |
| `EXPORT_ROOT` | `EXPORT_ROOT` 或 `CORE_EXPORT` 或 `~/.dsh/exports` | |

只定义常量，import 时不建目录、不读文件。

## timeutil.py

| 函数 | 返回 | 说明 |
|---|---|---|
| `now()` | 带本机时区的 `datetime` | |
| `local_iso(dt=None)` | `2026-11-13T22:45:11+08:00` | 不传就是现在，精确到秒 |
| `iso(dt=None)` | 同 `local_iso` | 旧名字，给没迁完的脚本 |
| `now_iso()` | 同 `local_iso()` | |
| `parse_iso(s)` | 带时区的 `datetime` | 认结尾 `Z`；不带时区按本机时区算；解析不了抛 `ValueError`，不吞 |
| `day_stamp(dt=None)` | `20261113` | 按本机时区 |

口径一致：都是本机时区、精确到秒、带偏移。

## io.py

| 函数 | 说明 |
|---|---|
| `atomic_write(path, text, mode=0o600)` | 同目录临时文件 → fsync → chmod → `os.replace`；中途被杀不会留半截正文 |
| `read_text(path, default='')` | 读不到返回 default |
| `read_json(path, default=None)` | 文件不在或 JSON 坏了返回 default |
| `write_json(path, obj, indent=1)` | `ensure_ascii=False`，字节形态跟 `json.dumps` 一致（不加换行） |

`atomic_write` 会自动建父目录。

## 谁在用

`ctx.py`、`recall.py`、`usage.py` 已经走 `lib`；其余脚本的路径常量还各自定义，行为不变。新脚本一律从 `lib` 取，别再自己拼。
