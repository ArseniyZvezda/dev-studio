/**
 * Shared-secret auth (spec §2.5 / §12).
 *
 * `voxarium_studio run --token …` prints a URL of the form `http://host:port/?token=…`, so the
 * token arrives once in the query string. We stash it and scrub the URL, because
 * leaving a credential in the address bar means it ends up in history, in screenshots
 * and in any link the user copies out of the browser.
 *
 * On a loopback run without `--token` the backend disables auth entirely and every
 * function here is a harmless no-op.
 */

const STORAGE_KEY = 'voxarium_studio.token'

let token: string | null = null

/** Read `?token=` if present, persist it, and strip it back out of the address bar. */
export function captureTokenFromUrl(): void {
  const url = new URL(window.location.href)
  const fromUrl = url.searchParams.get('token')
  if (fromUrl) {
    setToken(fromUrl)
    url.searchParams.delete('token')
    window.history.replaceState({}, '', url.pathname + url.search + url.hash)
    return
  }
  token = window.localStorage.getItem(STORAGE_KEY)
}

export function getToken(): string | null {
  if (token === null) token = window.localStorage.getItem(STORAGE_KEY)
  return token
}

export function setToken(value: string | null): void {
  token = value
  if (value) window.localStorage.setItem(STORAGE_KEY, value)
  else window.localStorage.removeItem(STORAGE_KEY)
}

/** Header form, used for every REST call. */
export function authHeaders(): Record<string, string> {
  const value = getToken()
  return value ? { Authorization: `Bearer ${value}` } : {}
}

/**
 * Query-param form. Only for the WebSocket handshake — the `WebSocket` constructor
 * gives no way to set request headers, which is why the backend accepts `?token=` too.
 */
export function withTokenParam(url: string): string {
  const value = getToken()
  if (!value) return url
  const separator = url.includes('?') ? '&' : '?'
  return `${url}${separator}token=${encodeURIComponent(value)}`
}
