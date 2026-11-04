/**
 * Where each language's messages come from: one file per language in `src/locales/`.
 *
 * English is imported statically — it is the fallback and the source of `MessageKey` — and
 * every other language is a dynamic import, so its file becomes a chunk of its own that only
 * a reader who picks that language downloads. Adding a language is a copy of `en.json`
 * translated, one line here, and one entry in `LOCALES`.
 */

import en from '@/locales/en.json'

import type { Locale, MessageTree } from './index'

export { en }

const loaders: Record<Exclude<Locale, 'en'>, () => Promise<MessageTree>> = {
  ru: async () => (await import('@/locales/ru.json')).default,
}

export function loadLocaleMessages(locale: Locale): Promise<MessageTree> {
  return locale === 'en' ? Promise.resolve(en as MessageTree) : loaders[locale]()
}
