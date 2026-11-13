# tests — 回归测试

两个脚本，都是纯标准库，跑起来各建一个临时 `CORE_HOME`，不碰真账本、不写 `~/.dsh`。

## 跑

```bash
python3 tests/test_queue.py     # PASS 6/6
python3 tests/test_recall.py    # PASS 5/5
```

全部通过时打印 `PASS n/n` 并退 0；失败打印 `FAIL <用例>: <原因>` 并退 1。

脚本默认从本文件同级的 `../bin/` 找被测脚本，找不到就去 `../tools/`。装在引擎里和直接在本仓跑都行，也能显式指定：

```bash
QUEUE_PY=/path/to/queue.py python3 tests/test_queue.py
RECALL_PY=/path/to/recall.py python3 tests/test_recall.py
```

## 覆盖什么

`test_queue.py`：`add` 同 key 只进一张、`claim` 抢不到第二张、`done` 后 `list` 带 `ok:true`、`fail` 落 `stuck` 不进 `done`、`sweep --older 0` 把超时单挪走、两个进程并发 `claim` 只有一个成功。

`test_recall.py`：`recall.py build` 退 0 并生成 `常驻.md`、五节标题齐全、总字数不超过 2000、待办行在、续点正文有内容。

## 依赖

Python 3.10 以上；`queue.py` 用到 SQLite 的地方需要 FTS5，一般发行版自带。不需要装任何第三方包。
