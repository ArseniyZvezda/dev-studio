import { describe, expect, it } from 'vitest'

import type { EventPayloads } from '@/ws/events'
import { taskTempChanged } from './task-temp'

const project = 'D:\\Game'
const temp = 'D:\\Game\\.voxarium_studio\\temp\\TASK-1'

function event(path: string, root: string | undefined = project): EventPayloads['explorer.file_changed'] {
  return { root, truncated: false, changes: [{ change: 'modified', path, is_dir: false }] }
}

describe('task temp file events', () => {
  it('matches nested scratch writes in the managed project, regardless of the task checkout', () => {
    expect(taskTempChanged(temp, project, event('.voxarium_studio/temp/TASK-1/probes/log.txt'))).toBe(true)
    expect(taskTempChanged(temp, project, event('.voxarium_studio/temp/TASK-2/log.txt'))).toBe(false)
    expect(taskTempChanged(temp, project, event('.voxarium_studio/temp/TASK-10/log.txt'))).toBe(false)
    expect(taskTempChanged(temp, project, event('Source/game.py'))).toBe(false)
  })

  it('matches deletion of the task folder or its parent without relying on is_dir', () => {
    for (const path of ['.voxarium_studio/temp/TASK-1', '.voxarium_studio/temp']) {
      const deleted = event(path)
      deleted.changes[0]!.change = 'deleted'
      expect(taskTempChanged(temp, project, deleted)).toBe(true)
    }
  })

  it('ignores another project watcher while projects switch', () => {
    expect(taskTempChanged(temp, project, event('.voxarium_studio/temp/TASK-1/a', 'D:/Other'))).toBe(false)
    expect(taskTempChanged(temp, 'D:/Other', event('.voxarium_studio/temp/TASK-1/a'))).toBe(false)
  })

  it('refreshes for incomplete batches only from the current project', () => {
    expect(taskTempChanged(temp, project, { ...event('Source/a'), truncated: true })).toBe(true)
    expect(taskTempChanged(temp, project, { ...event('Source/a', 'D:/Other'), truncated: true })).toBe(false)
  })

  it('normalises Windows paths and accepts older events without a root', () => {
    expect(taskTempChanged(temp, 'd:/game/', event('.voxarium_studio/temp/task-1/a', 'D:/GAME'))).toBe(true)
    const legacy = event('.voxarium_studio/temp/TASK-1/a')
    delete legacy.root
    expect(taskTempChanged(temp, project, legacy)).toBe(true)
  })

  it('keeps POSIX paths case-sensitive and handles a project at the filesystem root', () => {
    expect(taskTempChanged('/game/.voxarium_studio/temp/TASK-1', '/game', event('.voxarium_studio/temp/task-1/a', '/game'))).toBe(false)
    expect(taskTempChanged('/.voxarium_studio/temp/TASK-1', '/', event('.voxarium_studio/temp/TASK-1/a', '/'))).toBe(true)
  })

  it('does not refresh before the directory is known or when its path is unsafe', () => {
    expect(taskTempChanged(null, project, event('.voxarium_studio/temp/TASK-1/a'))).toBe(false)
    expect(taskTempChanged(temp, null, event('.voxarium_studio/temp/TASK-1/a'))).toBe(false)
  })
})
