/** Exact file entries for the explicit write list (§10.5), never paths inferred from prose. */
export function writePathsFromText(text: string): string[] {
  return [...new Set(text.split(/\r?\n/).map(path => path.trim()).filter(Boolean))]
}
