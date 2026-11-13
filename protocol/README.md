# 队列协议（protocol 层）

一张单的字段和状态，只认这里。

- 字段与允许值：`queue.schema.json`（JSON Schema draft-07，40 个字段，状态机在 `x-state-machine`）
- 校验器：`validate.py`（只用标准库，不装 jsonschema）
- queue.py 怎么接：`INTEGRATION.md`

队列目录 `queue/{inbox,running,done,stuck}` 是三个写入方共用的盘上约定：`tools/queue.py`、`plugins/delegate/index.js`、`tools/dispatch.py`，以及合并前的历史单。本层不搬单、不改单，只管「这张单合不合协议」。

## 字段

必填只有五个：`id`、`title`、`task`、`created`、`status`。其余按状态和来源出现。

| 组 | 字段 | 说明 |
| --- | --- | --- |
| 身份 | `id` `title` `task` `cwd` `session` | `id` 形如 `q-<base36时间>-<随机>` / `d-...`，文件名就是 `<id>.json` |
| 调度 | `created` `status` `key` `target` `cmd` | `key` 是幂等键，inbox/running 里同 key 只准一张；`target=tiangong` 或带 `cmd` 的单归 dispatch.py，插件不认领 |
| 运行 | `started` `heartbeat` `attempts` `worker` | 认领时落 `started`，插件每 60 秒刷 `heartbeat`；sweep 按 heartbeat→started→文件 mtime 判超时 |
| 结果 | `finished` `ok` `result` `reason` `out` `childId` `stopReason` | `result` 上限 200000 字符，`reason` 上限 2000；两者都是写入端截断后落盘 |
| 回执/验收 | `notified` `accepted` `accepted_at` `rejected` `reject_reason` `rejected_at` `parent` | `accepted=true` 必须有 `accepted_at`；`rejected=true` 必须有 `reject_reason`+`rejected_at`；`parent` 串 `resume --task` 的上一单 |
| dispatch 扩展 | `pid` `log` `errlog` `exitlog` `exit` `timeout` | 远端后台作业用，queue.py 不写 |
| 判重 | `duplicate_of` `notify` | 插件同 key 判重时作废的那张：`notify:false`，别回执打扰父会话 |
| 历史（只读兼容） | `sessionId` `createdAt` `finishedAt` `output` | 合并前的旧名。读时映射到 `session`/`created`/`finished`/`result`，写端只写新名 |

时间一律 ISO 带时区，`...Z` 和 `+08:00` 都认。布尔只写 true/false，别写字符串。

## 状态机

主干就是四条加一条 sweep：

| 迁移 | 谁做的 |
| --- | --- |
| `queued → running` | queue.py `claim` / 插件认领 |
| `queued → failed` | queue.py `fail`（inbox 里的单直接判失败） |
| `running → done` | queue.py `done` / 插件 `writeDone` / dispatch.py poll 成功 |
| `running → failed` | queue.py `fail` / 插件 `writeStuck` / dispatch.py poll 失败 |
| `running → stale` | queue.py `sweep`，心跳停更超过 `--older`（默认 1800 秒） |
| `done/failed/stale → queued` | `reject --retry` 或 `resume`，回流重跑 |

`status` 只允许 `queued` / `running` / `done` / `failed` / `stale`。`done` 必须 `ok:true` + `finished` + `result`；`failed`/`stale` 必须 `ok:false` + `reason` + `finished`；`running` 必须 `started`。这几条写在 schema 的 `allOf/if-then` 里，校验器直接执行，没有第二份规则。

目录和状态要对得上：`inbox` 只放 queued，`running` 只放 running，`done` 只放 done，`stuck` 放 failed 和 stale。单里没写 `status` 的历史单，校验器按所在目录推断，记一条 warn。

## 用法

```bash
python3 protocol/validate.py queue/done/<id>.json        # 0 合规 / 1 不合规
python3 protocol/validate.py <json> --from running       # 顺带校验状态迁移
python3 protocol/validate.py --schema                    # 打印读到的状态机
```

```python
from validate import validate_order, validate_status_transition
ok, errs = validate_order(obj)                 # (bool, [错误])
ok, errs = validate_order(obj, where='done')   # 顺带核对目录
ok, why = validate_status_transition('running', 'done')   # (bool, 说明)
```

`validate_order` 不修改传入的 obj。`notes=` 传一个 list 可以收 warn（历史字段名、未知字段、按目录推断的 status），warn 不影响返回值。

校验器不展开 `$ref`、不支持 `oneOf/anyOf`——schema 里也没用，保持够用就好。schema 是唯一出处：`type`、必填、枚举、状态机都从 `queue.schema.json` 读，Python 里不再抄一份（找不到文件才用文件里的兜底表，正常不会走到）。

## 当前盘面

合并前 dispatch.py 投过的老单里，有一种跑完了 `ok:true`、`finished` 都有，但 `status` 还停在 `running`。按协议这是状态没收尾，校验器照实报错；这类单不改，留给 dispatch.py 的 `finish()` 那边修。
