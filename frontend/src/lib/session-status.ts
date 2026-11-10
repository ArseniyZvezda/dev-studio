/**
 * What a session was equipped with — its skills and its MCP servers — folded out of the
 * transcript for the Status column (spec §10.4, §10.7, §10.10).
 *
 * The same answer for every runtime. The lists themselves come from the Studio's own records
 * at the start of each run (`kind: "skills"`, `kind: "mcp"`), since the Studio is what hands
 * a session its skills and servers whichever backend runs it. What only a runtime knows is
 * layered on top where it says it: Claude Code and Qwen Code report each server's state in
 * their `init` frame, and every runtime's skill loads are `Skill` calls in the feed (Codex's
 * backend names a shell read of a known `SKILL.md` as one). Codex reports no server states,
 * so its servers read as configured, with the calls made to each as the evidence they work.
 *
 * Plain data over the message list, like `lib/subagents.ts`, so it is tested without a DOM.
 */

import type { TranscriptMessage } from '@/lib/transcript'

/**
 * How far a skill got into the session. `loaded` — its `SKILL.md` was handed over at start;
 * `used` — the agent loaded it during the run; `offered` — named to the agent, not opened;
 * `missing` — given to the member, but no longer in the skills folder.
 */
export type SkillState = 'used' | 'loaded' | 'offered' | 'missing'

export interface SessionSkill {
  name: string
  label: string
  state: SkillState
}

export interface SessionMcpServer {
  name: string
  /** The runtime's word for it (`connected`, `failed`, `pending`, …); null when it gave none. */
  status: string | null
  /** Calls the session made to this server, from the feed. */
  calls: number
}

const SKILL_ORDER: Record<SkillState, number> = { used: 0, loaded: 1, offered: 2, missing: 3 }

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item !== '') : []
}

function lastIndexOf(messages: TranscriptMessage[], test: (message: TranscriptMessage) => boolean): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) if (test(messages[index]!)) return index
  return -1
}

/** The runtime's start-up frame — Claude Code's `init`, and Qwen Code's in the same shape. */
function isInit(message: TranscriptMessage): boolean {
  return message.kind === 'system' && (message.text === 'init' || message.data?.subtype === 'init')
}

/**
 * The newest `init` frame from the current run: one before the run's own record belongs to
 * an earlier run, which may have been another runtime with other servers.
 */
function currentInit(messages: TranscriptMessage[], recordKind: string): TranscriptMessage | null {
  const record = lastIndexOf(messages, (message) => message.kind === recordKind)
  const init = lastIndexOf(messages, isInit)
  return init >= 0 && init > record ? messages[init]! : null
}

function isCall(message: TranscriptMessage): boolean {
  return message.kind === 'tool_use'
}

function rawToolName(message: TranscriptMessage): string {
  return typeof message.data?.name === 'string' ? message.data.name : message.text
}

/** Which skill a `Skill` call loaded. Claude Code spells the argument `skill` (`command` on older CLIs). */
function calledSkill(message: TranscriptMessage): string {
  if (!isCall(message) || rawToolName(message) !== 'Skill') return ''
  const input = message.data?.input
  if (!input || typeof input !== 'object') return ''
  const values = input as Record<string, unknown>
  for (const key of ['skill', 'command', 'name']) {
    if (typeof values[key] === 'string' && values[key]) return values[key] as string
  }
  return ''
}

/**
 * Whether a name the runtime used is this skill. A plugin's skill is called by its
 * namespaced name (`voxarium_studio:studio`) where the folder may know it bare, and a native
 * installation is stored under an opaque id with the name people read as its label.
 */
function sameSkill(skill: SessionSkill, name: string): boolean {
  return (
    skill.name === name ||
    skill.label === name ||
    name.endsWith(`:${skill.name}`) ||
    skill.name.endsWith(`:${name}`)
  )
}

/** The skills of the session, the ones it actually loaded first. */
export function sessionSkills(messages: TranscriptMessage[]): SessionSkill[] {
  const skills: SessionSkill[] = []
  const find = (name: string) => skills.find((skill) => sameSkill(skill, name))

  const recordIndex = lastIndexOf(messages, (message) => message.kind === 'skills')
  const record = recordIndex >= 0 ? messages[recordIndex]! : null
  if (record) {
    const labels = record.data?.labels && typeof record.data.labels === 'object'
      ? (record.data.labels as Record<string, unknown>)
      : {}
    const loaded = new Set(strings(record.data?.loaded))
    for (const name of strings(record.data?.skills)) {
      const label = typeof labels[name] === 'string' && labels[name] ? (labels[name] as string) : name
      skills.push({ name, label, state: loaded.has(name) ? 'loaded' : 'offered' })
    }
    for (const name of strings(record.data?.missing)) skills.push({ name, label: name, state: 'missing' })
  }

  // Claude Code lists the skills it found on its own — user-level and plugin skills
  // included — in its start-up frame.
  const init = currentInit(messages, 'skills')
  const reported = init?.data?.skills
  if (Array.isArray(reported)) {
    for (const item of reported) {
      const name = typeof item === 'string'
        ? item
        : item && typeof item === 'object' && typeof (item as Record<string, unknown>).name === 'string'
          ? ((item as Record<string, unknown>).name as string)
          : ''
      if (name && !find(name)) skills.push({ name, label: name, state: 'offered' })
    }
  }

  for (const message of messages) {
    const name = calledSkill(message)
    if (!name) continue
    const known = find(name)
    if (known) known.state = 'used'
    else skills.push({ name, label: name, state: 'used' })
  }

  return skills.sort((a, b) => SKILL_ORDER[a.state] - SKILL_ORDER[b.state] || a.label.localeCompare(b.label))
}

/** `mcp__<server>__<tool>` → `<server>`, as the runtimes name an MCP call. */
function mcpServerOf(message: TranscriptMessage): string {
  if (!isCall(message)) return ''
  const raw = rawToolName(message)
  if (!raw.startsWith('mcp__')) return ''
  return raw.slice(5).split('__')[0] ?? ''
}

/** The MCP servers of the session, in the order the Studio handed them over. */
export function sessionMcpServers(messages: TranscriptMessage[]): SessionMcpServer[] {
  const servers = new Map<string, SessionMcpServer>()
  const add = (name: string) => {
    let server = servers.get(name)
    if (!server) {
      server = { name, status: null, calls: 0 }
      servers.set(name, server)
    }
    return server
  }

  const recordIndex = lastIndexOf(messages, (message) => message.kind === 'mcp')
  if (recordIndex >= 0) for (const name of strings(messages[recordIndex]!.data?.servers)) add(name)

  const reported = currentInit(messages, 'mcp')?.data?.mcp_servers
  if (Array.isArray(reported)) {
    for (const item of reported) {
      if (!item || typeof item !== 'object') continue
      const { name, status } = item as Record<string, unknown>
      if (typeof name !== 'string' || !name) continue
      add(name).status = typeof status === 'string' && status ? status : null
    }
  }

  // Counted over the whole feed: a resumed session still holds what it did before.
  for (const message of messages) {
    const name = mcpServerOf(message)
    if (name) add(name).calls += 1
  }
  return [...servers.values()]
}
