/**
 * dsh-ctx-guard: 顶层会话上下文到 60% 时，往下一步插一条「先做完整记忆」的提醒。
 * 另外两条轻检查：磁盘可用低于 10%、今天新开的子会话超过 20 个。
 * 三条同在一个 agent/turn-stopping 里，按顺序查，一次最多插三条。
 * 轮末另做一次口吻自检：只看最后一条 assistant 正文，不合格记日志，不插消息。
 */
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { readdirSync, statfsSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { checkTone, lastAssistantText, recordTone } from './tone.js'

export const name = 'dsh-ctx-guard'
export const inject = ['tokenMeter']

const RATIO = 0.6
const DISK_MIN_PCT = 10
const SUBS_MAX = 20
const REPEAT_MS = 6 * 60 * 60 * 1000
const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')
const SESSIONS_ROOT = process.env.DSH_SESSIONS || join(DSH_HOME, 'sessions')
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const MAX_MESSAGES = 3

const TEXT_CTX = (pct) => `<ctx_guard>上下文已用到 ${pct}%。按规矩：先做一次完整记忆——流水（这一页新增，不重复本体）、事实卡（新增和失效）、心声（标题前缀「心声·」）、向量（dream 会补，不用管）。做完再写续点换页。别只写续点。</ctx_guard>`

const TEXT_DISK = (pct) => `<ctx_guard>磁盘只剩 ${pct}% 了。先清缓存和大文件，别等到写不进账本。</ctx_guard>`

const TEXT_SUBS = (n) => `<ctx_guard>今天已经开了 ${n} 个子会话，攒着会拖慢列表。抽空清一批。</ctx_guard>`

/** 根分区可用比例（%）；取不到返回 null。 */
export function readDiskPct() {
  try {
    const s = statfsSync('/')
    if (!s || !s.blocks) return null
    return (s.bavail / s.blocks) * 100
  } catch {
    return null
  }
}

/** 今天 mtime 变过、且名字是裸 uuid 的会话目录数量；取不到返回 null。 */
export function countSubsToday(now = Date.now()) {
  try {
    const start = new Date(now)
    start.setHours(0, 0, 0, 0)
    const since = start.getTime()
    let n = 0
    // 会话根下按 workspace 分一层，再往下才是会话目录
    for (const ws of readdirSync(SESSIONS_ROOT, { withFileTypes: true })) {
      if (!ws.isDirectory()) continue
      let entries = []
      try {
        entries = readdirSync(join(SESSIONS_ROOT, ws.name), { withFileTypes: true })
      } catch {
        continue
      }
      for (const entry of entries) {
        if (!entry.isDirectory() || !UUID_RE.test(entry.name)) continue
        try {
          if (statSync(join(SESSIONS_ROOT, ws.name, entry.name)).mtimeMs >= since) n += 1
        } catch {
          // 目录刚好被清掉，跳过
        }
      }
    }
    return n
  } catch {
    return null
  }
}

/**
 * 纯逻辑：给读数，返回这一轮该插的消息（最多三条），并就地更新去重状态。
 * state: { lastDiskAt, lastSubsAt }
 */
export function plan({ diskPct, subsToday, ctxRatio, ctxAlreadyWarned }, now, state) {
  const out = []
  if (diskPct != null && diskPct < DISK_MIN_PCT && now - state.lastDiskAt >= REPEAT_MS) {
    state.lastDiskAt = now
    out.push({ kind: 'disk', text: TEXT_DISK(Math.round(diskPct)) })
  }
  if (subsToday != null && subsToday > SUBS_MAX && now - state.lastSubsAt >= REPEAT_MS) {
    state.lastSubsAt = now
    out.push({ kind: 'subs', text: TEXT_SUBS(subsToday) })
  }
  if (ctxRatio != null && ctxRatio >= RATIO && !ctxAlreadyWarned) {
    out.push({ kind: 'ctx', text: TEXT_CTX(Math.round(ctxRatio * 100)) })
  }
  return out.slice(0, MAX_MESSAGES)
}

export function apply(ctx) {
  const warned = new WeakSet()
  const state = { lastDiskAt: 0, lastSubsAt: 0 }

  ctx.on('agent/turn-stopping', async ({ agent, turn }) => {
    const session = agent && agent.session

    let ctxRatio = null
    if (session) {
      try {
        const pressure = ctx.tokenMeter.contextPressure(session)
        const contextWindow = pressure && pressure.contextWindow
        if (contextWindow) {
          const used = ctx.tokenMeter.measure(session).totalTokens
          ctxRatio = used / contextWindow
        }
      } catch {
        ctxRatio = null
      }
      if (ctxRatio != null && ctxRatio < RATIO) warned.delete(session)
    }

    const messages = plan({
      diskPct: readDiskPct(),
      subsToday: countSubsToday(),
      ctxRatio,
      ctxAlreadyWarned: session ? warned.has(session) : true,
    }, Date.now(), state)

    for (const m of messages) {
      if (m.kind === 'ctx' && session) warned.add(session)
      await agent.inject(createUserMessage({
        content: [{ type: 'text', text: m.text }],
        source: { kind: 'ctx-guard' },
      }))
    }

    // 口吻自检：只写日志，不插消息（插消息要花 token）。
    try {
      const text = lastAssistantText(session)
      if (text) {
        const sessionId = session && typeof session.id === 'string' ? session.id : 'unknown'
        recordTone(sessionId, turn, checkTone(text), text)
      }
    } catch {
      // 自检失败就当没这回事，不能拖垮回合
    }
  })
}
