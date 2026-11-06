/**
 * The language the Studio's own interface speaks.
 *
 * English is the source: every key exists in `locales/en.json`, the type of a key is derived
 * from that file, and any other language falls back to it one message at a time — so a
 * locale that is a few keys behind shows a few English strings rather than raw keys. Other
 * languages are fetched only when chosen; an English session never downloads them.
 *
 * Deliberately not vue-i18n. The Studio's copy is full of the characters that library treats
 * as syntax — `{graph}` placeholders shown to the user verbatim, `|` in "may not contain |",
 * `@` in paths — and every one would need escaping in every language. Here a message is
 * plain text with exactly two rules:
 *
 * - `{name}` is replaced only when `name` is a parameter the caller passed. Any other brace
 *   is text, so a literal `{width=300}` or `{skill}` needs no escaping.
 * - A message that varies by number is an object with one text per `Intl.PluralRules`
 *   category — `one`/`other` in English, `one`/`few`/`many`/`other` in Russian — and is
 *   chosen by the `count` parameter. `count` is always a quantity, so it is also printed
 *   with the language's digit grouping; every other number is printed as written, because
 *   a port or a task number is not a quantity.
 *
 * The choice belongs to the user, not to the project (§6.4). Settings persists it in the
 * user's application config. `localStorage` caches it for startup and offline screens;
 * the session store applies the authoritative preference after loading the backend.
 *
 * `t()` is a plain function over reactive state. A template, a `computed` or a store getter
 * that calls it re-runs when the language changes; a module-level constant that called it
 * once at import time does not, which is why label tables hold keys and resolve them at
 * render time.
 */

import { computed, ref, shallowRef } from 'vue'

import { en, loadLocaleMessages } from './messages'

export type Locale = 'en' | 'ru'

export interface LocaleOption {
  value: Locale
  /** The language's name in itself — never translated, so it can always be found. */
  label: string
}

/** Every language the interface ships. */
export const LOCALES: readonly LocaleOption[] = [
  { value: 'en', label: 'English' },
  { value: 'ru', label: 'Русский' },
]

export const DEFAULT_LOCALE: Locale = 'en'

const STORAGE_KEY = 'voxarium_studio.locale'

/** One text per plural category; `other` is the one every language has. */
export type PluralMessage = Partial<Record<Intl.LDMLPluralRule, string>> & { other: string }

export interface MessageTree {
  [key: string]: string | PluralMessage | MessageTree
}

export type MessageParams = Record<string, string | number>

type Leaves<T, Prefix extends string = ''> = {
  [K in keyof T & string]: T[K] extends string
    ? `${Prefix}${K}`
    : T[K] extends { other: string }
      ? `${Prefix}${K}`
      : Leaves<T[K], `${Prefix}${K}.`>
}[keyof T & string]

/** Every key `locales/en.json` defines — a typo in `t('…')` fails the type check. */
export type MessageKey = Leaves<typeof en>

type Message = string | PluralMessage
type Catalogue = Map<string, Message>

const PLURAL_CATEGORIES = new Set<string>(['zero', 'one', 'two', 'few', 'many', 'other'])

function isPlural(value: unknown): value is PluralMessage {
  if (!value || typeof value !== 'object') return false
  const entries = Object.entries(value)
  return (
    typeof (value as { other?: unknown }).other === 'string' &&
    entries.every(([key, text]) => PLURAL_CATEGORIES.has(key) && typeof text === 'string')
  )
}

/** A nested message tree as one flat map of dotted keys, built once per language. */
export function flattenMessages(tree: MessageTree, prefix = '', into: Catalogue = new Map()): Catalogue {
  for (const [key, value] of Object.entries(tree)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (typeof value === 'string' || isPlural(value)) into.set(path, value)
    else if (value && typeof value === 'object') flattenMessages(value, path, into)
  }
  return into
}

const fallback = flattenMessages(en as MessageTree)

const current = ref<Locale>(DEFAULT_LOCALE)
const messages = shallowRef<Catalogue>(fallback)

/** The language on screen. Read-only: `setLocale` is the one way to change it. */
export const locale = computed<Locale>(() => current.value)

const pluralRules = new Map<string, Intl.PluralRules>()
const numberFormats = new Map<string, Intl.NumberFormat>()

function rulesFor(language: string): Intl.PluralRules {
  let rules = pluralRules.get(language)
  if (!rules) {
    rules = new Intl.PluralRules(language)
    pluralRules.set(language, rules)
  }
  return rules
}

/** A number with the digit grouping of the language on screen: `1,234` / `1 234`. */
export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  const language = current.value
  if (options) return new Intl.NumberFormat(language, options).format(value)
  let format = numberFormats.get(language)
  if (!format) {
    format = new Intl.NumberFormat(language)
    numberFormats.set(language, format)
  }
  return format.format(value)
}

/**
 * A date or a time in the language on screen. The options are passed through untouched, so
 * say which fields you want — unlike `toLocaleDateString`, nothing is added for you.
 */
export function formatDate(value: Date | number, options: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(current.value, options).format(value)
}

function pick(message: Message, language: string, count: unknown): string {
  if (typeof message === 'string') return message
  if (typeof count !== 'number' || !Number.isFinite(count)) return message.other
  return message[rulesFor(language).select(count)] ?? message.other
}

function interpolate(text: string, params: MessageParams | undefined): string {
  if (!params) return text
  return text.replace(/\{(\w+)\}/g, (match, name: string) => {
    if (!Object.prototype.hasOwnProperty.call(params, name)) return match
    const value = params[name]
    return name === 'count' && typeof value === 'number' ? formatNumber(value) : String(value)
  })
}

const reported = new Set<string>()

function resolve(catalogue: Catalogue, language: string, key: string, params?: MessageParams): string {
  const own = catalogue.get(key)
  if (own !== undefined) return interpolate(pick(own, language, params?.count), params)
  // Chosen by English rules: a Russian `few` has no counterpart in an English message.
  const english = fallback.get(key)
  if (english !== undefined) return interpolate(pick(english, 'en', params?.count), params)
  if (import.meta.env?.DEV && !reported.has(key)) {
    reported.add(key)
    console.warn(`[i18n] No message for "${key}"`)
  }
  return key
}

/** The message for `key` in the language on screen, with `params` filled in. */
export function t(key: MessageKey, params?: MessageParams): string {
  return resolve(messages.value, current.value, key, params)
}

/**
 * Whether `key` names a message — the guard for a key built at runtime, such as
 * `status.${status}`, which the type checker cannot see into.
 */
export function hasMessage(key: string): key is MessageKey {
  return fallback.has(key)
}

function storedLocale(): Locale | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    return LOCALES.some((option) => option.value === value) ? (value as Locale) : null
  } catch {
    // No storage (a test, a locked-down profile): the default is the only memory there is.
    return null
  }
}

function remember(next: Locale): void {
  try {
    if (next === DEFAULT_LOCALE) localStorage.removeItem(STORAGE_KEY)
    else localStorage.setItem(STORAGE_KEY, next)
  } catch {
    /* The language still changes; it just will not survive a reload. */
  }
}

function stampDocument(next: Locale): void {
  // `lang` is what screen readers, hyphenation and the spell checker read.
  if (typeof document !== 'undefined') document.documentElement.lang = next
}

const loaded = new Map<Locale, Catalogue>([['en', fallback]])
let latestRequest = 0

async function catalogueFor(next: Locale): Promise<Catalogue> {
  const cached = loaded.get(next)
  if (cached) return cached
  const catalogue = flattenMessages(await loadLocaleMessages(next))
  loaded.set(next, catalogue)
  return catalogue
}

/**
 * Switch the interface to `next` and cache it in this browser. Settings persists the user
 * preference through the application-settings API before calling this function.
 *
 * The messages arrive before anything changes on screen, so the page never flashes keys; if
 * two choices race, the later one wins whichever download finishes first.
 */
export async function setLocale(next: Locale): Promise<void> {
  const request = ++latestRequest
  const catalogue = await catalogueFor(next)
  if (request !== latestRequest) return
  messages.value = catalogue
  current.value = next
  stampDocument(next)
  remember(next)
}

/**
 * Restore the language this browser chose last. Awaited before the app mounts, so a Russian
 * session paints Russian from its first frame. A language whose file fails to load leaves
 * the interface in English rather than stopping the Studio from starting.
 */
export async function initI18n(): Promise<void> {
  const stored = storedLocale()
  if (!stored || stored === DEFAULT_LOCALE) {
    stampDocument(DEFAULT_LOCALE)
    return
  }
  try {
    await setLocale(stored)
  } catch (error) {
    console.error(`Could not load the "${stored}" interface language`, error)
    stampDocument(DEFAULT_LOCALE)
  }
}

/**
 * A `t` over somebody else's messages — what a plugin module gets (`runtime.i18n`), since a
 * plugin ships its own strings and cannot add keys to the Studio's files.
 *
 * `catalogue` is keyed by language (`{ en: {…}, ru: {…} }`), each a message tree in the same
 * format as `locales/en.json`, plurals included. It follows the Studio's language reactively
 * and falls back to the catalogue's English, then to the key itself.
 */
export function createTranslator(
  catalogue: Record<string, MessageTree>,
): (key: string, params?: MessageParams) => string {
  const flat = new Map<string, Catalogue>()
  const english = flattenMessages(catalogue.en ?? {})
  return (key, params) => {
    const language = current.value
    let own = flat.get(language)
    if (!own) {
      own = flattenMessages(catalogue[language] ?? {})
      flat.set(language, own)
    }
    const message = own.get(key)
    if (message !== undefined) return interpolate(pick(message, language, params?.count), params)
    const base = english.get(key)
    if (base !== undefined) return interpolate(pick(base, 'en', params?.count), params)
    return key
  }
}
