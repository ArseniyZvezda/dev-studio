/**
 * Every language says the same things.
 *
 * English is the source (`MessageKey` is derived from it), so what is checked here is that
 * another language neither misses a message nor invents one, fills in the same placeholders —
 * a `{count}` translated away would print a sentence with no number in it — and gives a plural
 * message exactly the forms its own plural rules ask for. None of that is visible to the type
 * checker, which only ever sees English.
 */

import { describe, expect, it } from 'vitest'

import ru from '@/locales/ru.json'

import { flattenMessages, type MessageTree, type PluralMessage } from './index'
import { en } from './messages'

const TRANSLATIONS: Record<string, MessageTree> = { ru: ru as MessageTree }

const english = flattenMessages(en as MessageTree)

function placeholders(message: string | PluralMessage): string[] {
  const texts = typeof message === 'string' ? [message] : Object.values(message)
  const names = new Set<string>()
  for (const text of texts) {
    for (const match of text.matchAll(/\{(\w+)\}/g)) names.add(match[1]!)
  }
  return [...names].sort()
}

function forms(language: string): string[] {
  return [...new Intl.PluralRules(language).resolvedOptions().pluralCategories].sort()
}

function texts(message: string | PluralMessage): string[] {
  return typeof message === 'string' ? [message] : Object.values(message)
}

describe('en', () => {
  it('holds no empty message and no half-formed plural', () => {
    for (const [key, message] of english) {
      expect(texts(message).every((text) => text.trim() !== ''), key).toBe(true)
      // A key ending in a plural category means an object was not recognised as a plural —
      // a stray key beside `one`/`other`, or a section that happens to be called `other`.
      expect(key, key).not.toMatch(/\.(zero|one|two|few|many|other)$/)
      if (typeof message !== 'string') {
        expect(Object.keys(message).sort(), key).toEqual(forms('en'))
      }
    }
  })
})

for (const [language, tree] of Object.entries(TRANSLATIONS)) {
  describe(language, () => {
    const messages = flattenMessages(tree)

    it('has a message for every English key and none English lacks', () => {
      const missing = [...english.keys()].filter((key) => !messages.has(key))
      const extra = [...messages.keys()].filter((key) => !english.has(key))
      expect(missing).toEqual([])
      expect(extra).toEqual([])
    })

    it('fills in the same placeholders as English', () => {
      const mismatched = [...english].filter(([key, message]) => {
        const own = messages.get(key)
        return own !== undefined && placeholders(own).join() !== placeholders(message).join()
      })
      expect(mismatched.map(([key]) => key)).toEqual([])
    })

    it('gives a plural message exactly the forms its plural rules use', () => {
      for (const [key, message] of english) {
        const own = messages.get(key)
        if (own === undefined) continue
        expect(typeof own, key).toBe(typeof message)
        if (typeof own !== 'string') expect(Object.keys(own).sort(), key).toEqual(forms(language))
      }
    })

    it('holds no empty message', () => {
      const empty = [...messages].filter(([, message]) =>
        texts(message).some((text) => text.trim() === ''),
      )
      expect(empty.map(([key]) => key)).toEqual([])
    })
  })
}
