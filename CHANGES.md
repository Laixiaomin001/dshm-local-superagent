# 这一版改了什么

## 换掉的

旧仓库的 `plugins/` 是三个插件，新版换成七个：`memory`、`jobs`、`delegate`、`readonly-preset`、`ctx-guard`、`main-guard`、`sentinel`。目录改短名，插件名仍是 `xiaomin-*`，`cordis.patch.yml` 里写插件名。

记忆从「SQLite 单库 + 关键词/无向量」换成 FTS5 加百炼向量的混合检索：FTS 候选 40、向量候选 40，RRF(k=60) 融合，8 秒硬超时，向量不可用时静默退纯 FTS 并返回 `degraded_reason`。向量索引只有一份 `向量_v2.bailian.jsonl`，查询和索引同源，不混算。`verify` 也从「对账」扩成磁盘、索引、域三档检查，历史标签只对旧件宽容。

新增三样东西：决策层守卫 `main-guard`（顶层会话拒七个动手工具，子会话豁免）、哨兵 `sentinel`（每回合落料进 `运行/待记/`）、上下文守卫 `ctx-guard`（60% 提醒换页），只读 preset 把压缩阈值定在 70%。

不再收录：`README.en.md`，旧的 `delegate.py`、`daily.py`，以及旧的三个插件目录。

## 保留的

`LICENSE`（MIT，版权人 Laixiaomin001）。

`examples/e2e.md` 和 `examples/inbox.example.json`，原样保留。e2e 走查写的是旧版 delegate 插件的环境变量和输出格式，当历史记录看；跟当前实现对不上的地方，以 `plugins/delegate/index.js` 为准。

`docs/field-notes.md`，历史现场手记，原样保留，没有按新版事实改写。

`.gitignore` 保留原有规则，末尾追加了凭证和密钥的忽略项。
