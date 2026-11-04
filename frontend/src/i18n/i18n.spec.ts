import { afterEach, describe, expect, it } from 'vitest'

import { formatBytes, formatCountdown, formatDuration, statusLabel } from '@/lib/status'

import {
  createTranslator,
  flattenMessages,
  formatNumber,
  hasMessage,
  locale,
  setLocale,
  t,
} from './index'

afterEach(async () => {
  await setLocale('en')
})

describe('t', () => {
  it('fills in the parameters it is given and leaves every other brace as text', () => {
    expect(t('settings.unsavedValue', { value: 'VS-01' })).toBe('VS-01 (unsaved)')
    // `{file}` is a slot for I18nT; without a parameter of that name it is printed as written.
    expect(t('settings.language.hint')).toContain('{file}')
  })

  it('chooses a plural form by count and groups the count like the language does', () => {
    expect(t('settings.execution.cacheCleared', { count: 1 })).toBe('Removed 1 cached node result.')
    expect(t('settings.execution.cacheCleared', { count: 1234 })).toBe(
      'Removed 1,234 cached node results.',
    )
  })

  it('answers a key it does not know with the key itself', () => {
    expect(t('no.such.key' as never)).toBe('no.such.key')
    expect(hasMessage('status.done')).toBe(true)
    expect(hasMessage('status.nonsense')).toBe(false)
  })
})

describe('setLocale', () => {
  it('switches every message, plural rules and number formats included', async () => {
    await setLocale('ru')
    expect(locale.value).toBe('ru')
    expect(t('status.done')).toBe('Готово')
    expect(t('settings.execution.cacheCleared', { count: 1 })).toBe(
      'Удалён 1 кэшированный результат узла.',
    )
    expect(t('settings.execution.cacheCleared', { count: 3 })).toContain('результата')
    expect(t('settings.execution.cacheCleared', { count: 25 })).toContain('результатов')
    expect(formatNumber(1234.5)).toBe('1 234,5')
  })

  it('carries the shared formatters with it', async () => {
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatCountdown('2026-09-08T12:05:00', Date.parse('2026-09-08T12:00:00Z'))).toBe('5m 00s')
    await setLocale('ru')
    expect(formatBytes(1536)).toBe('1,5 КБ')
    expect(formatCountdown('2026-09-08T12:05:00', Date.parse('2026-09-08T12:00:00Z'))).toBe(
      '5 мин 00 с',
    )
    expect(formatDuration('2026-09-08T12:00:00', '2026-09-08T12:00:42')).toBe('42 с')
    expect(statusLabel('needs_review')).toBe('На проверке')
    // A status neither file knows is still a word, not a key.
    expect(statusLabel('queued')).toBe('Queued')
  })

  it('lets the later of two racing choices win', async () => {
    const first = setLocale('ru')
    const second = setLocale('en')
    await Promise.all([first, second])
    expect(locale.value).toBe('en')
  })
})

describe('createTranslator', () => {
  const translate = createTranslator({
    en: { save: 'Save', files: { one: '{count} file', other: '{count} files' }, only: 'English only' },
    ru: { save: 'Сохранить', files: { one: '{count} файл', few: '{count} файла', many: '{count} файлов', other: '{count} файла' } },
  })

  it('follows the Studio language and falls back to its own English, then to the key', async () => {
    expect(translate('files', { count: 2 })).toBe('2 files')
    await setLocale('ru')
    expect(translate('save')).toBe('Сохранить')
    expect(translate('files', { count: 5 })).toBe('5 файлов')
    expect(translate('only')).toBe('English only')
    expect(translate('missing')).toBe('missing')
  })
})

describe('flattenMessages', () => {
  it('keeps a plural object whole and walks everything else', () => {
    const flat = flattenMessages({
      a: { b: 'text', n: { one: 'x', other: 'y' } },
    })
    expect([...flat.keys()]).toEqual(['a.b', 'a.n'])
    expect(flat.get('a.n')).toEqual({ one: 'x', other: 'y' })
  })
})
