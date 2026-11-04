/**
 * How a tool call reads in one line, and what a run has cost.
 *
 * Data, not layout, which is why it is testable here: a row truncates in CSS, and the
 * decisions worth pinning are which name a call goes by and which of its arguments says what
 * it was pointed at.
 */
import { describe, expect, it } from 'vitest'

import { oneLine, toolName, toolSubject } from '@/lib/activity'
import { formatCost } from '@/lib/status'

const message = (overrides: Partial<Parameters<typeof toolName>[0]> = {}) => ({
  role: 'assistant' as const,
  kind: 'text',
  text: '',
  data: {} as Record<string, unknown>,
  ...overrides,
})

describe('a tool call in one line', () => {
  it('names the tool and what it was pointed at', () => {
    const call = message({
      kind: 'tool_use',
      text: 'Bash',
      data: { name: 'Bash', input: { command: 'npm test' } },
    })
    expect(toolName(call)).toBe('Bash')
    expect(toolSubject(call)).toBe('npm test')
  })

  it('reads an MCP tool as the server it is talking to', () => {
    expect(toolName(message({ text: 'mcp__voxarium_studio__create_task' }))).toBe(
      'mcp · voxarium_studio · create_task',
    )
    // The server name may itself contain an underscore, so the split is on `__`.
    expect(toolName(message({ text: 'mcp__my_server__do__it' }))).toBe('mcp · my_server · do__it')
    expect(toolName(message({ text: 'mcp__bare' }))).toBe('mcp · bare')
  })

  it('flattens a value onto one line and clips it', () => {
    expect(oneLine('  Reading the\n  spec  ', 160)).toBe('Reading the spec')
    expect(oneLine('abcdef', 3)).toBe('abc…')
  })

  it('takes the first argument that identifies a call, whatever the tool', () => {
    expect(toolSubject(message({ data: { input: { unknown_key: 'still useful' } } }))).toBe(
      'still useful',
    )
    // Preferred keys win over whatever else was sent.
    expect(toolSubject(message({ data: { input: { extra: 'no', file_path: 'a.ts' } } }))).toBe(
      'a.ts',
    )
    expect(toolSubject(message({ data: {} }))).toBe('')
  })
})

describe('formatCost', () => {
  it('keeps a sub-cent run from reading as free', () => {
    expect(formatCost(0.004)).toBe('$0.004')
    expect(formatCost(0.01)).toBe('$0.01')
    expect(formatCost(1.239)).toBe('$1.24')
    expect(formatCost(0)).toBe('$0')
  })

  it('groups the digits of a total long enough to need it', () => {
    expect(formatCost(6933.06)).toBe('$6,933.06')
    expect(formatCost(999.5)).toBe('$999.50')
  })
})
