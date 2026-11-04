/**
 * A document's title, derived the same way the backend derives it.
 *
 * `_title_of` in `backend/voxarium_studio/teams/docs.py` is the other half: the sidebar, the header
 * and the `[[` autocomplete all name a document, and they have to agree whether the name
 * came from a saved file or from what is being typed right now.
 */

export function titleOf(source: string, path: string): string {
  for (const line of source.split('\n', 20)) {
    const heading = /^#{1,6}\s+(.*)$/.exec(line.trim())
    if (heading) return heading[1].trim()
  }
  const name = path.split('/').pop() ?? path
  return name.replace(/\.(md|markdown|txt)$/i, '').replace(/[-_]/g, ' ')
}
