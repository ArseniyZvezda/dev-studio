/**
 * What a `![alt](src)` reference means, before anything is fetched (block-editor spec §3.4).
 *
 * Two questions, and neither belongs in a component: whether the reference points at a file
 * of this project at all, and what to call the picture when it cannot be shown. Both are data
 * — `images.spec.ts` covers them — and the first has to agree with the backend's own rule
 * (`DocStore.asset`), or the editor asks for a picture the server was never going to serve.
 */

/**
 * `https://…`, `data:…`, `//host/x.png`, `C:/art/x.png` — anything but a path inside the
 * project. The test is the URL's *scheme*, which is what makes the Windows path an absolute
 * reference to a machine rather than a folder called `C:`.
 */
export function isRemoteImage(src: string): boolean {
  const value = src.trim()
  return /^[a-z][a-z0-9+.\-]*:/i.test(value) || value.startsWith('//')
}

/**
 * The suffixes a document will take, which **must stay the same set** as the backend's
 * `IMAGE_SUFFIXES` (`explorer/service.py`). It is duplicated rather than fetched because the
 * drop handler has to answer instantly, on a `dragover` the browser gives no time in; a list
 * that drifts wider here shows the user an upload that the server then refuses.
 */
export const IMAGE_SUFFIXES = [
  '.png',
  '.jpg',
  '.jpeg',
  '.gif',
  '.webp',
  '.bmp',
  '.svg',
  '.ico',
] as const

/**
 * Whether a dropped file is one of those, judged **by name and not by MIME type**.
 *
 * The suffix is what the backend stores and what decides the file's name on disk, so
 * accepting `image/avif` because the browser said so would upload a file the server refuses
 * — the name is the thing both sides can agree about.
 */
export function isImageFile(file: { name: string }): boolean {
  const name = file.name.toLowerCase()
  return IMAGE_SUFFIXES.some((suffix) => name.endsWith(suffix))
}

/**
 * The name to show for a picture that is not on screen: the author's alt text, else the file's
 * own name out of the reference.
 *
 * Percent-encoding is decoded because an exported document writes a space as `%20`, and a
 * reference that is not valid encoding is shown as written rather than thrown away — this is a
 * label, and the only failure available to it is being ugly.
 */
export function imageLabel(src: string, alt?: string | null): string {
  const label = (alt ?? '').trim()
  if (label) return label

  const value = src.trim()
  const name = value.split(/[?#]/, 1)[0]?.split('/').pop() ?? ''
  try {
    return decodeURIComponent(name) || value
  } catch {
    return name || value
  }
}
