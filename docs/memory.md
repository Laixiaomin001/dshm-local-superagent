# 记忆系统

长期记忆放在本机：一个 SQLite 索引加两层 markdown 文件。没有服务、没有网络、没有第三方库，四个工具只是适配层，后端是 `tools/mem.py`。

## 1. 为什么是 SQLite + FTS5

记忆条目本身就是文件，直接 `ls`、`cp`、`grep` 都能用；需要的是「按词找回来」，不是「把整目录读进上下文」。SQLite 自带 FTS5，建一个虚拟表就能做全文检索，编译进 Python 标准库的 `sqlite3`，不需要装任何东西。

用 `trigram` 分词器，一条记录被切成三字片段。中文没有空格，按词切要先分词，按三字切不用；三字及以上的查询走索引，两字查询退到标题和件名的 `LIKE`。检索上限写在协议里：一次最多 5 条、2 KB，取原文最多 8 KB。

不用向量库的理由很直接：单机、几千件、离线，FTS5 的召回够用，而向量库要模型、要常驻内存、要下载权重。记忆是账，不是语义搜索产品。

## 2. 四个工具

| 工具 | 后端子命令 | 作用 | 上限 |
|---|---|---|---|
| `mem_search` | `search QUERY [--limit N]` | 按词检索，返回条目摘要 | 5 条 / 2 KB |
| `mem_get` | `get NAME [--max N]` | 取一件原文 | 8 KB，超出截断 |
| `mem_store` | `store --title T ...` | 追加一件，索引同事务更新 | — |
| `mem_status` | `status` | 件数、漂移、最后写入、库大小 | — |

命令行等价：

```bash
python3 mem.py store --title "记录标题" --said "他说了什么" --heart "我的备注" --who alice --domain general
python3 mem.py search "标题关键词" --limit 5
python3 mem.py get 20260101_1200_记录标题.md --max 4000
python3 mem.py status
python3 mem.py reindex
```

`store` 先写 `flow/<时间戳>_<标题>.md.tmp`，`os.replace` 成正式文件，再在同一个 SQLite 事务里更新索引。写到一半失败会把文件删掉，不留下「磁盘有、索引没有」的半件。

## 3. 流水与正本

两层职责不同：

`flow/` 是流水。一次对话、一件事、一个续点，各写一件，只追加不改写。文件名是 `20260101_1200_标题.md`，件内固定几栏：时间、来源、人物、他说了啥、我心里、域。这是「发生过什么」的原始账，也是检索的主要来源。

`canon/` 是正本。经过整理、会反复引用的文档：领域说明、操作手册、长期结论。可以放子目录，可以改，件名带路径。数量少，但每件更重。

流水只增，正本可改。要纠正旧账，不回头改流水，补一件新的并在里面说明；正本直接改，改完跑 `reindex`。

## 4. 索引口径

索引里的件名和磁盘上的相对路径一一对应：

- 流水件名就是文件名，`20260101_1200_标题.md`。
- 正本件名带前缀和子目录，`canon/notes/某主题.md`。

`reindex` 走的路径和 `status` 数的路径是同一套：`flow/*.md` 加 `canon/**/*.md`。所以 `status` 能直接对账：

```
index  flow 128 / canon 12 / total 140
disk   flow 128 / canon 12 / total 140 / missing 0 / extra 0
aligned: yes
```

`missing` 是磁盘有、索引没有；`extra` 是索引有、磁盘没有。两边相等且都为零才算对齐。单件里也有 `bytes` 和 `mtime`，用来发现内容被手改过。

## 5. reindex 怎么跑

手改了文件、批量导入、换机器、库损坏，都跑一次：

```bash
python3 "$LOCAL_MEMORY_PY" reindex
```

它清空 `docs` 和 `docs_fts`，重新遍历两层目录，读每件的标题、时间、域，写回索引，最后 `VACUUM`。几百件是毫秒到秒级；默认 120 秒硬超时，可用 `MEM_REINDEX_TIMEOUT` 放宽。

注意：`reindex` 不会删除磁盘文件，只重建索引。反过来，删了文件不跑 `reindex`，`status` 会报 `extra`。

## 6. 数据不出机器

根目录默认 `~/.local-superagent/memory/`，可用 `LOCAL_SUPERAGENT_HOME` 改父目录，或用 `MEM_HOME` 直接指到别处。里面有 `flow/`、`canon/`、`index.db`、`.mem.lock`。

没有网络调用，没有遥测，没有云端同步。备份就是拷目录；迁移就是连同目录一起搬。权限靠文件系统，能读这个目录就能读全部记忆。

并发靠 `flock` 串行化写入，单次动作有硬超时；插件那一层还有 15 秒 `SIGKILL` 兜底。

另：`tools/persona.py` 从本地人格草稿渲染 `AGENTS.md`，草稿不进仓库，默认在 `~/.local-superagent/persona/draft.md`。
