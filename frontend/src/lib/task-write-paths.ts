/** Entries of the explicit write list (§10.5) — exact files, or a folder as `foo/bar/*` — never paths inferred from prose. */
export function writePathsFromText(text: string): string[] {
  return [...new Set(text.split(/\r?\n/).map(path => path.trim()).filter(Boolean))]
}
