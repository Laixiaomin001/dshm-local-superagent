# queue.py 接入清单

现在不用改 queue.py 也能用：`validate.py` 独立，命令行和 import 都行。想让它真在写入前拦单，改下面四处，每处一行。`tools/queue.py` 我没动，等你点头。

## 文件头（一次）

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'protocol'))
from validate import validate_order, validate_status_transition
```

`queue.py` 里已经有 `Path`，`sys` 也在，只多一行 import 路径。

## 一个小 helper（三行，放在 `_write` 旁边）

```python
def _guard(rec, where, old=None):
    ok, errs = validate_order(rec, where=where)
    tok, why = validate_status_transition(old, rec.get('status')) if old else (True, '')
    if not tok:
        errs.append(why)
    for e in errs:
        print(f'[error] {rec.get("id")} 不合协议：{e}')
    return not errs
```

`where` 是这张单要落进哪个目录，`old` 是改之前的 status。返回 False 就直接 `return 1`，别写盘。

## 必须接的四处

| # | 位置 | 一行 | 为什么在这 |
| --- | --- | --- | --- |
| 1 | `cmd_add`，构造好 `rec` 之后、`write_text` 之前（约 144–148 行） | `if not _guard(rec, 'inbox'): return 1` | 入口就把缺 `title`/`task`、`id` 格式错的单挡掉，不落一个坏文件 |
| 2 | `cmd_claim`，`json.loads` 之后、`os.rename` 之前（约 191 行） | `if not _guard({**rec, 'status': 'running', 'started': iso()}, 'running', old=rec.get('status')): return 1` | 先验候选态；验不过时单还在 inbox，不用回滚 rename |
| 3 | `finish()`，`rec['finished'] = iso()` 之后、写 `DONE` 之前（约 229–230 行） | `if not _guard(rec, 'done', old='running'): return 1` | 没写 `ok`/`result` 的单不落 done；失败时单还留在 running，能再救 |
| 4 | `cmd_fail`，`rec.update(...)` 之前记 `old`、之后写 `STUCK` 之前（约 248–251 行） | `old = rec.get('status'); rec.update(...); if not _guard(rec, 'failed', old=old): return 1` | fail 能从 inbox（queued）也能从 running 进来，`old` 得在改之前抓 |

第 2 处传 `{**rec, ...}` 是为了不提前改内存里的 rec；真写盘时 `started` 会重新取一次 `iso()`，差几十毫秒，不影响。

## 可选的三处

- `cmd_sweep`（约 282–285 行）：`if not _guard(rec, 'stale', old='running'): continue`。心跳超时的单落 stuck 前顺手验一遍。
- `cmd_reject --retry`（约 390–401 行）：清完上一轮字段后 `if not _guard(rec, 'inbox', old=where): return 1`。假设 `where` 是目录名，中文名得先映射回英文。
- `cmd_resume --task` 造新单（约 426–437 行）：新单必然合规（`created`/`status` 都齐），验不验都行；要验就 `_guard(new, 'inbox')`。

`cmd_accept` 只打两个标记，不产生新状态，不用接。

## 接上之后会怎样

- `tests/test_queue.py` 的回归用例都是正规流程，四处校验全过，不该变红。跑一遍再收工：`python3 tests/test_queue.py`。
- 唯一会变的是「手写坏单」：以前能塞进 inbox，现在 `add`/`claim` 就报错。这是要的效果。
- `status` 停在 `running` 却躺在 `done` 的单（现在 `d-toayqu-qtb0.json` 就是）queue.py 不产出，接不接都拦不到它；要治得改 `dispatch.py` 的 `finish()`，不在这次范围。
