/**
 * xiaomin-main-guard —— 决策层那道墙（2026-11-06 重写，据知识库 21 篇）。
 *
 * 顶层会话调 write/edit/bash/python/move/publish/remove 一律拒，回一句
 * 「主会话不干活，动手请派子会话」。带 parentSession / origin=subagent 的子会话放行。
 *
 * 保险丝：$DSH_HOME/main-guard.off 存在就整体放行（出问题建这个文件即可拆墙）。
 */
import { existsSync } from 'node:fs'

export const name = 'xiaomin-main-guard'
export const inject = ['tools']

const BLOCKED = new Set(['write', 'edit', 'bash', 'python', 'move', 'publish', 'remove'])
const REJECT = '主会话不干活，动手请派子会话（subagent / delegate）'
const OFF_FILE = (process.env.DSH_HOME || '/data/storage/el2/base/haps/entry/files/dsh/home') + '/main-guard.off'

function isChild(agent) {
  if (!agent || !agent.session) return false
  const s = agent.session
  const h = s.header && typeof s.header === 'object' ? s.header : s
  if (h.parentSession !== undefined && h.parentSession !== null) return true
  if (h.origin === 'subagent') return true
  if (typeof h.delegationDepth === 'number' && h.delegationDepth > 0) return true
  return false
}

export function apply(ctx) {
  ctx.on('tools/pre-execute', (exec, next) => {
    if (!exec || !BLOCKED.has(exec.name)) return next()
    if (isChild(exec.agent)) return next()
    if (existsSync(OFF_FILE)) return next()
    return { kind: 'deny', reason: REJECT }
  }, { prepend: true })
}
