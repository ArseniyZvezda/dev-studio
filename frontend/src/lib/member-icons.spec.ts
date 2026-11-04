/**
 * The catalogue a member's icon is chosen from (§10.2).
 *
 * Two things here are contracts rather than tastes: every name must have the shape
 * `api/teams.py` accepts, or picking that icon is a 400 and the roster silently keeps the
 * icon it had; and every entry must actually resolve to a component, since a name that
 * imports nothing renders an empty box rather than failing loudly.
 */

import { describe, expect, it } from 'vitest'

import { MEMBER_ICONS, MEMBER_ICON_GROUPS, memberIcon } from './member-icons'

/** `_ICON_NAME` and `MAX_ICON_NAME` in `backend/voxarium_studio/api/teams.py`. */
const BACKEND_ICON_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/

describe('the icons a member can wear', () => {
  it('are all names the backend accepts, each listed once', () => {
    for (const icon of MEMBER_ICONS) {
      expect(icon.name).toMatch(BACKEND_ICON_NAME)
      expect(icon.name.length).toBeLessThanOrEqual(64)
      expect(icon.component).toBeTruthy()
    }
    expect(new Set(MEMBER_ICONS.map((icon) => icon.name)).size).toBe(MEMBER_ICONS.length)
  })

  it('each name a distinct kind of work, so the picker never offers the same job twice', () => {
    expect(new Set(MEMBER_ICONS.map((icon) => icon.label)).size).toBe(MEMBER_ICONS.length)
  })

  it('are grouped by department, every group named and populated', () => {
    expect(MEMBER_ICON_GROUPS.length).toBeGreaterThan(1)
    for (const group of MEMBER_ICON_GROUPS) {
      expect(group.label).not.toBe('')
      expect(group.icons.length).toBeGreaterThan(0)
    }
    expect(MEMBER_ICON_GROUPS.flatMap((group) => group.icons)).toEqual([...MEMBER_ICONS])
  })

  it('cover the disciplines the roster is written in terms of', () => {
    // The roles `voxarium_studio init` ships with, so a fresh project can name every one of them.
    for (const name of ['crown', 'gamepad-2', 'code', 'bug', 'palette', 'audio-waveform']) {
      expect(memberIcon(name)).not.toBeNull()
    }
  })

  it('draw nothing for a name this build does not know', () => {
    expect(memberIcon('palette')?.label).toBe('Artist')
    expect(memberIcon('a-name-from-a-newer-studio')).toBeNull()
    expect(memberIcon(null)).toBeNull()
    expect(memberIcon(undefined)).toBeNull()
    expect(memberIcon('')).toBeNull()
  })
})
