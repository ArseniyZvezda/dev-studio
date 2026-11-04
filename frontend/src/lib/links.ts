/** Only web/mail links may become active in agent-written content (block-editor spec §3.4). */
export function safeLinkHref(raw: string): string | null {
  const url = raw.trim()
  // Browsers discard controls when interpreting schemes. Check the normalized form,
  // while preserving the original URL for legitimate links and relative paths.
  const normalized = [...url].filter((ch) => {
    const point = ch.codePointAt(0)!
    return point > 0x20 && point !== 0x7f
  }).join('')
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(normalized)
  if (!scheme) return url || null
  return ['http', 'https', 'mailto'].includes(scheme[1].toLowerCase()) ? url : null
}
