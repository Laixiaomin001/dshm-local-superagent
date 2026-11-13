/** guard: 顶层会话不直接动手；子会话放行；记账脚本白名单。 */
export const name = 'dsh-guard'
export const inject = ['tools']

const BLOCK = new Set(['write', 'edit', 'move', 'remove', 'publish'])
const ALLOW = /(?:^|[\s/])bin\/(core|fact|queue|dispatch|sentinel|style|review|vectors|voice|guard|ctx|usage)\.py/
const REJECT = '顶层不直接动手：改文件、跑命令请派子会话；记账走 $DSH_HOME/agent-core/bin/*.py'

function isChild(agent) {
  const s = agent && agent.session
  if (!s) return false
  const h = s.header && typeof s.header === 'object' ? s.header : s
  if (h.parentSession !== undefined && h.parentSession !== null) return true
  if (h.origin === 'subagent') return true
  if (typeof h.delegationDepth === 'number' && h.delegationDepth > 0) return true
  return false
}

function cmdOf(exec) {
  const a = exec && exec.arguments
  if (typeof a === 'string') return a
  if (a && typeof a === 'object') return String(a.command ?? a.cmd ?? '')
  return ''
}

export function apply(ctx) {
  ctx.on('tools/pre-execute', (exec, next) => {
    if (!exec) return next()
    const n = exec.name
    if (!BLOCK.has(n) && n !== 'bash') return next()
    if (isChild(exec.agent)) return next()
    if (n === 'bash') {
      if (ALLOW.test(cmdOf(exec))) return next()
      return { kind: 'deny', reason: REJECT }
    }
    return { kind: 'deny', reason: REJECT }
  }, { prepend: true })
}
