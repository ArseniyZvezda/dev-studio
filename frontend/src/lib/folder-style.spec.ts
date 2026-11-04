/**
 * The folder look's catalogue (block-editor spec §5.2).
 *
 * Two things here are contracts with the backend rather than tastes: every icon name must have
 * the shape `DocStore` accepts, or picking that icon is a 400; and the colours must be the ten
 * words `FOLDER_COLORS` in `teams/docs.py` lists, or picking one is a 400 as well.
 */

import { describe, expect, it } from 'vitest'

import {
  FOLDER_COLORS,
  FOLDER_ICONS,
  folderColorValue,
  folderIcon,
  isFolderColor,
} from './folder-style'

/** `_ICON_NAME` and `MAX_ICON_NAME` in `backend/voxarium_studio/teams/docs.py`. */
const BACKEND_ICON_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/

describe('the icons a folder can wear', () => {
  it('are all names the backend accepts, each listed once', () => {
    for (const icon of FOLDER_ICONS) {
      expect(icon.name).toMatch(BACKEND_ICON_NAME)
      expect(icon.name.length).toBeLessThanOrEqual(64)
      expect(icon.component).toBeTruthy()
    }
    expect(new Set(FOLDER_ICONS.map((icon) => icon.name)).size).toBe(FOLDER_ICONS.length)
  })

  it('fill the menu grid with the plain folder in front of them', () => {
    // Eight across; the first cell of the grid is "no icon".
    expect((FOLDER_ICONS.length + 1) % 8).toBe(0)
  })

  it('draw the plain folder for a name this build does not know', () => {
    expect(folderIcon('bug')?.label).toBe('Bugs')
    expect(folderIcon('a-name-from-a-newer-studio')).toBeNull()
    expect(folderIcon(null)).toBeNull()
    expect(folderIcon('')).toBeNull()
  })
})

describe('the colours a folder can wear', () => {
  it('are the ten text colours the backend accepts, and never the second spelling', () => {
    expect([...FOLDER_COLORS]).toEqual([
      'red',
      'orange',
      'yellow',
      'green',
      'cyan',
      'blue',
      'purple',
      'pink',
      'brown',
      'gray',
    ])
    expect(isFolderColor('grey')).toBe(false)
  })

  it('resolve to the theme-tuned token, and to nothing for a colour outside the list', () => {
    expect(folderColorValue('green')).toBe('var(--vxs-md-green)')
    expect(folderColorValue('grey')).toBe('var(--vxs-md-gray)')
    expect(folderColorValue('#ff0000')).toBeNull()
    expect(folderColorValue(null)).toBeNull()
  })
})
