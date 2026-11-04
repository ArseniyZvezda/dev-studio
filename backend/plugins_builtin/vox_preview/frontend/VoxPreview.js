/**
 * Voxarium (.voxarium) and MagicaVoxel (.vox) previewer for the Explorer preview pane (spec §6.2).
 *
 * Loaded at runtime as a plain ES module from `/api/plugins/vox_preview/assets/…`, so
 * it must have no imports at all: `vue` is handed in by the host through the factory
 * (a second Vue instance would share no reactivity), and everything else — the format
 * reader and the renderer — is in this file.
 *
 * It is one file on purpose. A dynamic `import()` cannot send an auth header, so the
 * host appends `?token=` to this URL; a relative `import './viewer.js'` from here would
 * resolve without that token and 401 in server mode. A real plugin ships a single
 * bundled module for the same reason.
 *
 * The parser is exported so it can be exercised head-lessly in Node, where
 * `DecompressionStream` and typed arrays behave exactly as they do in the browser.
 */

/* ------------------------------------------------------------------ messages */

/**
 * What the previewer says, in the Studio's interface languages (§6.4).
 *
 * The component words its own text while it renders. The reader and the viewer below run
 * outside it — and head-lessly in Node, with no host at all — so what they throw is worded
 * by `say()` when it is thrown: in the interface language once the factory has run, in
 * English before. A preview reads its file each time it is mounted, so that sentence is in
 * the language the file was opened in. Object names out of the file are data.
 */
const MESSAGES = {
  en: {
    reading: 'Reading voxels…',
    unreadable: 'Could not read this voxel file — {reason}',
    projectVersion: 'This project uses a newer Voxarium format ({version})',
    projectMissing: 'This Voxarium project contains no readable voxel scene',
    projectFlags: 'This Voxarium scene uses unsupported compression',
    notVox: 'Not a MagicaVoxel .vox file (bad magic)',
    noModels: 'This .vox file contains no voxel models',
    objects: {
      unnamed: 'Object {number}',
      empty: 'Nothing to draw for this object',
      toggle: 'Show or hide · alt-click to isolate',
      expand: 'Expand',
      collapse: 'Collapse',
      showAll: 'Show all',
      hideAll: 'Hide all',
      shown: { one: '{shown} / {count} object', other: '{shown} / {count} objects' },
    },
    stats: {
      scene: '{size} scene',
      grid: '{size} grid',
      voxels: { one: '{count} voxel', other: '{count} voxels' },
      tris: { one: '{count} tri', other: '{count} tris' },
      models: { one: '{count} model', other: '{count} models' },
      truncated: {
        one: 'truncated at {shown} of {count} object',
        other: 'truncated at {shown} of {count} objects',
      },
    },
    hint: 'drag to orbit · shift-drag to pan · scroll to zoom · double-click to fit',
    webgl: {
      unavailable: 'WebGL is not available in this browser',
      compile: 'Shader failed to compile: {log}',
      link: 'Shader program failed to link: {log}',
    },
  },
  ru: {
    reading: 'Чтение вокселей…',
    unreadable: 'Не удалось прочитать воксельный файл — {reason}',
    projectVersion: 'Проект использует более новую версию формата Voxarium ({version})',
    projectMissing: 'В проекте Voxarium нет доступной для чтения воксельной сцены',
    projectFlags: 'Воксельная сцена использует неподдерживаемое сжатие',
    notVox: 'Это не файл MagicaVoxel .vox (неверная сигнатура)',
    noModels: 'В этом файле .vox нет воксельных моделей',
    objects: {
      unnamed: 'Объект {number}',
      empty: 'Для этого объекта нечего рисовать',
      toggle: 'Показать или скрыть · alt+щелчок — изолировать',
      expand: 'Развернуть',
      collapse: 'Свернуть',
      showAll: 'Показать все',
      hideAll: 'Скрыть все',
      shown: {
        one: '{shown} / {count} объект',
        few: '{shown} / {count} объекта',
        many: '{shown} / {count} объектов',
        other: '{shown} / {count} объекта',
      },
    },
    stats: {
      scene: 'сцена {size}',
      grid: 'сетка {size}',
      voxels: {
        one: '{count} воксель',
        few: '{count} вокселя',
        many: '{count} вокселей',
        other: '{count} вокселя',
      },
      tris: {
        one: '{count} треугольник',
        few: '{count} треугольника',
        many: '{count} треугольников',
        other: '{count} треугольника',
      },
      models: {
        one: '{count} модель',
        few: '{count} модели',
        many: '{count} моделей',
        other: '{count} модели',
      },
      truncated: {
        one: 'показано {shown} из {count} объекта',
        few: 'показано {shown} из {count} объектов',
        many: 'показано {shown} из {count} объектов',
        other: 'показано {shown} из {count} объекта',
      },
    },
    hint: 'вращение: ЛКМ · сдвиг: Shift+ЛКМ · масштаб: колесо · вписать: двойной клик',
    webgl: {
      unavailable: 'WebGL недоступен в этом браузере',
      compile: 'Не удалось скомпилировать шейдер: {log}',
      link: 'Не удалось скомпоновать шейдерную программу: {log}',
    },
  },
}

/** The host's translator, bound when the factory runs. */
let translate = null

/** A message for code outside the component; English, as written, until there is a host. */
function say(key, params = {}) {
  if (translate) return translate(key, params)
  const message = key.split('.').reduce((node, part) => node?.[part], MESSAGES.en)
  const text =
    typeof message === 'string'
      ? message
      : (message?.[params.count === 1 ? 'one' : 'other'] ?? message?.other ?? key)
  return text.replace(/\{(\w+)\}/g, (match, name) => {
    if (!Object.hasOwn(params, name)) return match
    const value = params[name]
    return name === 'count' && typeof value === 'number' ? value.toLocaleString('en') : String(value)
  })
}

/* ------------------------------------------------------------------ .vox format */

const TEXT = new TextDecoder()

/** Faces are only emitted where a voxel has no solid neighbour, but a dense 256³ grid
 *  can still overwhelm the GPU; past this the mesh is truncated and the UI says so. */
const MAX_FACES = 400_000

/**
 * MagicaVoxel's built-in palette, used only when a file ships no RGBA chunk.
 * It is a 6-level RGB cube (216 entries) followed by four 10-step ramps — red, green,
 * blue, grey — which is why it can be generated instead of tabulated.
 */
function defaultPalette() {
  const entries = []
  const levels = [255, 204, 153, 102, 51, 0]
  for (const r of levels) for (const g of levels) for (const b of levels) entries.push([r, g, b])
  const ramp = [238, 221, 187, 170, 136, 119, 85, 68, 34, 17]
  for (const v of ramp) entries.push([v, 0, 0])
  for (const v of ramp) entries.push([0, v, 0])
  for (const v of ramp) entries.push([0, 0, v])
  for (const v of ramp) entries.push([v, v, v])

  // Colour index 0 means "empty", so index i reads palette entry i - 1.
  const palette = new Uint8Array(256 * 3)
  for (let i = 1; i < 256; i += 1) {
    const [r, g, b] = entries[i - 1]
    palette[i * 3] = r
    palette[i * 3 + 1] = g
    palette[i * 3 + 2] = b
  }
  return palette
}

/** A rotation is a signed axis permutation: `world[r] = sign[r] * local[axis[r]]`. */
const IDENTITY = { axis: [0, 1, 2], sign: [1, 1, 1] }

/**
 * Decode the extension format's ROTATION byte.
 *
 * It stores a row-major signed permutation matrix: bits 0-1 and 2-3 are the column of
 * the non-zero entry in rows 1 and 2 (row 3 takes the column neither used), and bits
 * 4-6 are the three signs. Anything self-contradictory falls back to identity — a
 * preview that is upright but unrotated beats one that collapses a model onto a plane.
 */
export function decodeRotation(byte) {
  const first = byte & 3
  const second = (byte >> 2) & 3
  if (first > 2 || second > 2 || first === second) return IDENTITY
  return {
    axis: [first, second, 3 - first - second],
    sign: [byte & 0x10 ? -1 : 1, byte & 0x20 ? -1 : 1, byte & 0x40 ? -1 : 1],
  }
}

/**
 * Compose two rotations so that `apply(compose(a, b), v) === apply(a, apply(b, v))`,
 * i.e. `a` is the parent and `b` the child. Written out rather than going through 3×3
 * matrices because a permutation composes in three lookups and stays exact.
 */
function composeRotation(a, b) {
  return {
    axis: [b.axis[a.axis[0]], b.axis[a.axis[1]], b.axis[a.axis[2]]],
    sign: [
      a.sign[0] * b.sign[a.axis[0]],
      a.sign[1] * b.sign[a.axis[1]],
      a.sign[2] * b.sign[a.axis[2]],
    ],
  }
}

function applyRotation(rotation, v) {
  if (rotation.matrix) return [0, 1, 2].map(row => rotation.matrix[row*3]*v[0] + rotation.matrix[row*3+1]*v[1] + rotation.matrix[row*3+2]*v[2])
  return [
    rotation.sign[0] * v[rotation.axis[0]],
    rotation.sign[1] * v[rotation.axis[1]],
    rotation.sign[2] * v[rotation.axis[2]],
  ]
}

/**
 * Read a .vox file into models plus the scene graph placing them.
 *
 * The format is a tree of `<id><contentBytes><childrenBytes>` chunks. Only the ones
 * needed to draw are interpreted; anything else (materials, notes, render settings) is
 * skipped by its declared size, which is what keeps this robust across MagicaVoxel
 * versions.
 *
 * The scene chunks — nTRN/nGRP/nSHP and LAYR — are from the extension spec
 * (`MagicaVoxel-file-format-vox-extension.txt`). They are what turns a file from a bag
 * of grids into a placed scene, so a viewer that skips them shows one object where the
 * author built twenty.
 */

/** Native VXRM v0/v1 container; mirrors VoxariumVoxel crates/format/src/container.rs.
 * Unknown chunks and damaged optional metadata are tolerated. Required scene errors
 * are shown by the preview instead of escaping into the host application.
 */
export function parseVoxarium(buffer) {
  const bytes = new Uint8Array(buffer)
  if (bytes.length < 8 || tagAt(bytes, 0) !== 'VXRM') throw new Error(say('projectMissing'))
  const view = new DataView(buffer)
  const version = view.getUint32(4, true)
  if (version > 1) throw new Error(say('projectVersion', { version }))
  let scene = null, metadata = null
  for (let offset = 8; offset + 12 <= bytes.length;) {
    const id = tagAt(bytes, offset)
    const flags = view.getUint32(offset + 4, true)
    const length = view.getUint32(offset + 8, true)
    offset += 12
    if (length > bytes.length - offset) break
    if (id === 'VOX ' && scene === null) {
      if (flags) throw new Error(say('projectFlags'))
      scene = parseVox(buffer.slice(offset, offset + length))
    } else if (id === 'PROJ' && metadata === null && flags === 0 && length <= 16 * 1024 * 1024) {
      try { metadata = JSON.parse(TEXT.decode(bytes.subarray(offset, offset + length))) } catch { /* optional */ }
    }
    offset += length
  }
  if (!scene) throw new Error(say('projectMissing'))
  const entries = Array.isArray(metadata?.objects) ? metadata.objects : []
  for (const placement of scene.placements) {
    const entry = entries[placement.model]
    if (!entry || typeof entry !== 'object') continue
    if (typeof entry.hidden === 'boolean') placement.hidden = entry.hidden
    // Parents affect visibility; object transforms in VXRM are already world-space.
    const seen = new Set([placement.model])
    let parent = entry.parent
    while (Number.isInteger(parent) && parent >= 0 && parent < entries.length && !seen.has(parent) && seen.size < 2048) {
      seen.add(parent)
      placement.hidden ||= entries[parent]?.hidden === true
      parent = entries[parent]?.parent
    }
    const rawFree = entry.free
    const array = v => v && typeof v === 'object' ? [v.x, v.y, v.z] : null
    const free = rawFree && { position: array(rawFree.position), rotation: array(rawFree.rotation), scale: array(rawFree.scale) }
    const vec = v => Array.isArray(v) && v.length === 3 && v.every(n => typeof n === 'number' && Number.isFinite(n) && Math.abs(n) <= 1e9)
    if (!free || !vec(free.position) || !vec(free.rotation) || !vec(free.scale) || free.scale.some(n => Math.abs(n) < 0.0001 || Math.abs(n) > 10000)) continue
    const [x, y, z] = free.rotation.map(n => n * Math.PI / 180)
    const [cx, cy, cz, sx, sy, sz] = [Math.cos(x), Math.cos(y), Math.cos(z), Math.sin(x), Math.sin(y), Math.sin(z)]
    // Rz * Ry * Rx; column scaling. Normals use inverse-transpose scaling.
    const rotation = [cz*cy, cz*sy*sx-sz*cx, cz*sy*cx+sz*sx, sz*cy, sz*sy*sx+cz*cx, sz*sy*cx-cz*sx, -sy, cy*sx, cy*cx]
    placement.rotation = { matrix: rotation.map((n,i) => n * free.scale[i % 3]), normal: rotation.map((n,i) => n / free.scale[i % 3]) }
    placement.translation = free.position
    placement.mirrored = free.scale[0] * free.scale[1] * free.scale[2] < 0
  }
  return { ...scene, hidden: scene.placements.filter(p => p.hidden).length, bounds: sceneBounds(scene.models, scene.placements.filter(p => !p.hidden)) }
}

export function parseVoxelFile(buffer) {
  return tagAt(new Uint8Array(buffer), 0) === 'VXRM' ? parseVoxarium(buffer) : parseVox(buffer)
}

export function parseVox(buffer) {
  const bytes = new Uint8Array(buffer)
  const view = new DataView(buffer)
  if (bytes.length < 8 || tagAt(bytes, 0) !== 'VOX ') {
    throw new Error(say('notVox'))
  }

  const models = []
  const transforms = new Map()
  const groups = new Map()
  const shapes = new Map()
  const layers = new Map()
  let palette = defaultPalette()
  let pendingSize = null

  readChildren(8, bytes.length)

  function readChildren(start, end, depth = 0) {
    if (depth > 64) throw new Error("Voxel chunk nesting limit exceeded")
    let offset = start
    while (offset + 12 <= end) {
      const id = tagAt(bytes, offset)
      const contentBytes = view.getUint32(offset + 4, true)
      const childBytes = view.getUint32(offset + 8, true)
      const contentStart = offset + 12
      const childStart = contentStart + contentBytes
      if (childStart + childBytes > end) break // truncated file — stop rather than throw
      readChunk(id, contentStart)
      if (childBytes > 0) readChildren(childStart, childStart + childBytes, depth + 1)
      offset = childStart + childBytes
    }
  }

  function readChunk(id, at) {
    if (id === 'SIZE') {
      pendingSize = [view.getUint32(at, true), view.getUint32(at + 4, true), view.getUint32(at + 8, true)]
    } else if (id === 'XYZI') {
      const count = view.getUint32(at, true)
      if (!pendingSize?.every(n => n >= 1 && n <= 256) || models.length >= 4096 || count > 256**3 || at + 4 + count * 4 > bytes.length) throw new Error('Invalid voxel grid size')
      models.push({
        size: pendingSize ?? [1, 1, 1],
        voxels: bytes.slice(at + 4, at + 4 + count * 4),
      })
    } else if (id === 'RGBA') {
      palette = new Uint8Array(256 * 3)
      for (let i = 1; i < 256; i += 1) {
        palette[i * 3] = bytes[at + (i - 1) * 4]
        palette[i * 3 + 1] = bytes[at + (i - 1) * 4 + 1]
        palette[i * 3 + 2] = bytes[at + (i - 1) * 4 + 2]
      }
    } else if (id === 'nTRN') {
      const nodeId = view.getInt32(at, true)
      const [attributes, afterAttributes] = readDict(at + 4)
      let cursor = afterAttributes
      const childId = view.getInt32(cursor, true)
      const layerId = view.getInt32(cursor + 8, true)
      cursor += 12 // childId, reserved, layerId
      const frameCount = view.getInt32(cursor, true)
      cursor += 4
      let translation = [0, 0, 0]
      let rotation = IDENTITY
      for (let i = 0; i < frameCount; i += 1) {
        const [frame, next] = readDict(cursor)
        cursor = next
        // Keyframes (`_f`) drive MagicaVoxel's animation timeline; a still preview wants
        // the base frame — the one with no `_f`, or an `_f` of 0.
        if (Number(frame._f ?? 0) !== 0) continue
        if (typeof frame._t === 'string') {
          const parts = frame._t.trim().split(/\s+/).map(Number)
          if (parts.length === 3 && parts.every(Number.isFinite)) translation = parts
        }
        if (frame._r !== undefined) {
          const raw = Number(frame._r)
          if (Number.isFinite(raw)) rotation = decodeRotation(raw & 0xff)
        }
      }
      transforms.set(nodeId, {
        childId,
        translation,
        rotation,
        layerId,
        name: typeof attributes._name === 'string' ? attributes._name : '',
        hidden: attributes._hidden === '1',
      })
    } else if (id === 'LAYR') {
      const layerId = view.getInt32(at, true)
      const [attributes] = readDict(at + 4)
      layers.set(layerId, {
        name: typeof attributes._name === 'string' ? attributes._name : '',
        hidden: attributes._hidden === '1',
      })
    } else if (id === 'nGRP') {
      const nodeId = view.getInt32(at, true)
      const [attributes, afterAttributes] = readDict(at + 4)
      let cursor = afterAttributes
      const count = view.getInt32(cursor, true)
      cursor += 4
      const children = []
      for (let i = 0; i < count; i += 1) children.push(view.getInt32(cursor + i * 4, true))
      groups.set(nodeId, { children, hidden: attributes._hidden === '1' })
    } else if (id === 'nSHP') {
      const nodeId = view.getInt32(at, true)
      let cursor = readDict(at + 4)[1]
      const count = view.getInt32(cursor, true)
      cursor += 4
      // A shape's models are its animation frames, not parts drawn together — placing
      // all of them would stack every frame of a spinner on one spot.
      const modelIds = []
      for (let i = 0; i < count; i += 1) {
        const modelId = view.getInt32(cursor, true)
        const [frame, next] = readDict(cursor + 4)
        cursor = next
        if (Number(frame._f ?? 0) === 0) modelIds.push(modelId)
      }
      shapes.set(nodeId, modelIds.length ? [modelIds[0]] : [])
    }
  }

  function readDict(at) {
    const count = view.getInt32(at, true)
    let cursor = at + 4
    const dict = {}
    for (let i = 0; i < count; i += 1) {
      const [key, afterKey] = readString(cursor)
      const [value, afterValue] = readString(afterKey)
      dict[key] = value
      cursor = afterValue
    }
    return [dict, cursor]
  }

  function readString(at) {
    const length = view.getInt32(at, true)
    // A corrupt length would otherwise decode megabytes of the file as one key.
    if (length < 0 || at + 4 + length > bytes.length) return ['', bytes.length]
    return [TEXT.decode(bytes.subarray(at + 4, at + 4 + length)), at + 4 + length]
  }

  return {
    models,
    palette,
    ...resolveScene(models, transforms, groups, shapes, layers),
  }
}

/**
 * Walk the scene graph from the root node, accumulating each object's world transform.
 *
 * Every object the graph places is kept, including the same model placed more than
 * once — instancing is how MagicaVoxel scenes are built, so the count of objects has no
 * relation to the count of models and must not be used to sanity-check the traversal.
 *
 * Single-model files — still the overwhelming majority — carry no scene graph at all
 * and land on the fallback at the bottom.
 */
function resolveScene(models, transforms, groups, shapes, layers) {
  const placements = []

  // Only node 0 is a root: the spec fixes it, and starting anywhere else on a malformed
  // file would silently render one subtree instead of falling back to every model.
  // `open` guards a file whose nodes reference each other in a loop, which would
  // otherwise recurse until the tab dies; it tracks the current path rather than every
  // node seen, so a subtree legitimately reached from two parents is still placed twice.
  const open = new Set()
  visit(0, [0, 0, 0], IDENTITY, false, '')

  // Objects hidden in MagicaVoxel are kept, not dropped: they are listed in the viewer's
  // object panel so they can be switched back on. Only `hidden` decides what is drawn.
  const found = placements.length
    ? placements
    // No usable graph: stack every model on the origin, centred like MagicaVoxel does.
    : models.map((_, index) => ({
        model: index,
        translation: [0, 0, 0],
        rotation: IDENTITY,
        name: '',
        hidden: false,
      }))

  return {
    placements: found,
    hidden: found.filter((placement) => placement.hidden).length,
    bounds: sceneBounds(models, found.filter((placement) => !placement.hidden)),
  }

  function visit(nodeId, translation, rotation, concealed, name) {
    if (open.has(nodeId)) return
    open.add(nodeId)
    try {
      const transform = transforms.get(nodeId)
      if (transform) {
        // Child-then-parent: the parent rotates the child's offset before adding its own.
        const moved = applyRotation(rotation, transform.translation)
        visit(
          transform.childId,
          [translation[0] + moved[0], translation[1] + moved[1], translation[2] + moved[2]],
          composeRotation(rotation, transform.rotation),
          concealed || transform.hidden || (layers.get(transform.layerId)?.hidden ?? false),
          transform.name || name,
        )
        return
      }
      const group = groups.get(nodeId)
      if (group) {
        for (const child of group.children) {
          visit(child, translation, rotation, concealed || group.hidden, name)
        }
        return
      }
      for (const modelId of shapes.get(nodeId) ?? []) {
        if (!models[modelId]) continue
        placements.push({ model: modelId, translation, rotation, name, hidden: concealed })
      }
    } finally {
      open.delete(nodeId)
    }
  }
}

/**
 * The scene's extent in MagicaVoxel space, used to report its size before any mesh is
 * built — a scene too dense to mesh in full should still be able to say how big it is.
 */
function sceneBounds(models, placements) {
  const min = [Infinity, Infinity, Infinity]
  const max = [-Infinity, -Infinity, -Infinity]
  for (const placement of placements) {
    const size = models[placement.model].size
    const pivot = modelPivot(size)
    // Two opposite corners are enough: the transform is a signed axis permutation, so
    // it maps the box's extremes to the box's extremes.
    for (const corner of [0,1,2,3,4,5,6,7].map(bits => size.map((n, i) => bits & (1 << i) ? n : 0))) {
      const local = [corner[0] - pivot[0], corner[1] - pivot[1], corner[2] - pivot[2]]
      const world = applyRotation(placement.rotation, local)
      for (let axis = 0; axis < 3; axis += 1) {
        const value = world[axis] + placement.translation[axis]
        if (value < min[axis]) min[axis] = value
        if (value > max[axis]) max[axis] = value
      }
    }
  }
  if (!placements.length) return { min: [0, 0, 0], max: [0, 0, 0], size: [0, 0, 0] }
  return { min, max, size: [max[0] - min[0], max[1] - min[1], max[2] - min[2]] }
}

/**
 * A model is placed by its centre, so the grid is centred on its own origin — and that
 * centre is also what a rotation turns about. Continuous rather than floored: an odd
 * size rotated about a floored pivot lands a voxel off its neighbours, which shows up
 * as a seam between two halves of one object.
 */
function modelPivot(size) {
  return [size[0] / 2, size[1] / 2, size[2] / 2]
}

function tagAt(bytes, offset) {
  return String.fromCharCode(bytes[offset], bytes[offset + 1], bytes[offset + 2], bytes[offset + 3])
}

/* ------------------------------------------------------------------ mesh building */

/**
 * The six cube faces. Corners are wound counter-clockwise around the outward normal,
 * in MagicaVoxel's Z-up space; `toViewSpace` below is a rotation, so the winding
 * survives the conversion.
 */
const FACES = [
  { normal: [1, 0, 0], corners: [[1, 0, 0], [1, 1, 0], [1, 1, 1], [1, 0, 1]] },
  { normal: [-1, 0, 0], corners: [[0, 0, 0], [0, 0, 1], [0, 1, 1], [0, 1, 0]] },
  { normal: [0, 1, 0], corners: [[0, 1, 0], [0, 1, 1], [1, 1, 1], [1, 1, 0]] },
  { normal: [0, -1, 0], corners: [[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]] },
  { normal: [0, 0, 1], corners: [[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]] },
  { normal: [0, 0, -1], corners: [[0, 0, 0], [0, 1, 0], [1, 1, 0], [1, 0, 0]] },
]

/** MagicaVoxel is Z-up; the viewer is Y-up. This is a rotation, not a mirror. */
function toViewSpace(x, y, z) {
  return [x, z, -y]
}

/**
 * Turn parsed voxels into a triangle soup, emitting only faces that are actually
 * visible. A solid 64³ block is 262144 voxels but just 24576 visible faces, so this
 * is the difference between a preview that renders instantly and one that does not.
 */
export function buildVoxelMesh(vox) {
  // Built on demand and shared between instances of the same model: a scene that places
  // one tree forty times must not decode that tree's grid forty times, and a scene
  // carrying models nothing places must not decode them at all.
  const grids = new Map()
  const faceCounts = new Map()
  const gridFor = (index) => {
    if (!grids.has(index)) grids.set(index, occupancyGrid(vox.models[index]))
    return grids.get(index)
  }
  const facesFor = (index) => {
    if (!faceCounts.has(index)) {
      faceCounts.set(index, countFaces(gridFor(index), vox.models[index].size))
    }
    return faceCounts.get(index)
  }

  // Counting first costs one extra pass but lets the buffers be allocated exactly;
  // sizing them from the voxel count instead would over-allocate by an order of
  // magnitude on anything solid.
  let faceCount = 0
  for (const placement of vox.placements) faceCount += facesFor(placement.model)
  const truncated = faceCount > MAX_FACES
  const faces = Math.min(faceCount, MAX_FACES)

  // Every object is meshed, including the ones hidden in MagicaVoxel, so the panel's eye
  // toggles are a change of what is drawn rather than a rebuild. Shown objects go first
  // so that when the face budget runs out it is the hidden ones that fall off the end.
  const order = vox.placements
    .map((placement, index) => ({ placement, index }))
    .sort((a, b) => Number(a.placement.hidden) - Number(b.placement.hidden))

  const positions = new Float32Array(faces * 18)
  const normals = new Float32Array(faces * 18)
  const colors = new Uint8Array(faces * 18)
  const bounds = { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] }
  // One entry per object that made it into the buffers: which slice of the vertex arrays
  // is its own, and the box it occupies, so the camera can frame a subset.
  const ranges = []
  let vertex = 0
  let voxelCount = 0
  let objectCount = 0

  emit: for (const { placement, index: placementIndex } of order) {
    const rangeFirst = vertex
    const rangeBounds = {
      min: [Infinity, Infinity, Infinity],
      max: [-Infinity, -Infinity, -Infinity],
    }
    const closeRange = () => {
      if (vertex > rangeFirst) {
        ranges.push({
          index: placementIndex,
          first: rangeFirst,
          count: vertex - rangeFirst,
          bounds: rangeBounds,
        })
      }
    }
    const model = vox.models[placement.model]
    const grid = gridFor(placement.model)
    const [sx, sy, sz] = model.size
    const rotation = placement.rotation ?? IDENTITY
    const { axis, sign } = rotation
    const [px, py, pz] = modelPivot(model.size)
    const [tx, ty, tz] = placement.translation
    objectCount += 1

    // A face's normal depends on the placement, never on the voxel, so the six of them
    // are rotated once here instead of once per emitted face.
    const faceNormals = FACES.map((face) => {
      const normal = applyRotation(rotation.normal ? { matrix: rotation.normal } : rotation, face.normal)
      const length = Math.hypot(...normal) || 1
      return toViewSpace(...normal.map(n => n / length))
    })
    const local = [0, 0, 0]

    for (let z = 0; z < sz; z += 1) {
      for (let y = 0; y < sy; y += 1) {
        for (let x = 0; x < sx; x += 1) {
          const colorIndex = grid[x + sx * (y + sy * z)]
          if (colorIndex === 0) continue
          voxelCount += 1
          const r = vox.palette[colorIndex * 3]
          const g = vox.palette[colorIndex * 3 + 1]
          const b = vox.palette[colorIndex * 3 + 2]

          for (let f = 0; f < FACES.length; f += 1) {
            const face = FACES[f]
            const nx = x + face.normal[0]
            const ny = y + face.normal[1]
            const nz = z + face.normal[2]
            const inside = nx >= 0 && ny >= 0 && nz >= 0 && nx < sx && ny < sy && nz < sz
            if (inside && grid[nx + sx * (ny + sy * nz)] !== 0) continue
            if (vertex / 6 >= faces) {
              closeRange()
              break emit
            }

            const normal = faceNormals[f]
            for (const index of (placement.mirrored ? [0, 2, 1, 0, 3, 2] : [0, 1, 2, 0, 2, 3])) {
              const corner = face.corners[index]
              // Centre on the model's pivot, rotate, then translate into the scene.
              local[0] = x + corner[0] - px
              local[1] = y + corner[1] - py
              local[2] = z + corner[2] - pz
              const transformed = rotation.matrix ? applyRotation(rotation, local) : null
              const wx = (transformed ? transformed[0] : sign[0] * local[axis[0]]) + tx
              const wy = (transformed ? transformed[1] : sign[1] * local[axis[1]]) + ty
              const wz = (transformed ? transformed[2] : sign[2] * local[axis[2]]) + tz

              // toViewSpace, inlined: this runs once per vertex of the whole scene.
              const vx = wx
              const vy = wz
              const vz = -wy
              positions[vertex * 3] = vx
              positions[vertex * 3 + 1] = vy
              positions[vertex * 3 + 2] = vz
              normals[vertex * 3] = normal[0]
              normals[vertex * 3 + 1] = normal[1]
              normals[vertex * 3 + 2] = normal[2]
              colors[vertex * 3] = r
              colors[vertex * 3 + 1] = g
              colors[vertex * 3 + 2] = b
              if (vx < rangeBounds.min[0]) rangeBounds.min[0] = vx
              if (vx > rangeBounds.max[0]) rangeBounds.max[0] = vx
              if (vy < rangeBounds.min[1]) rangeBounds.min[1] = vy
              if (vy > rangeBounds.max[1]) rangeBounds.max[1] = vy
              if (vz < rangeBounds.min[2]) rangeBounds.min[2] = vz
              if (vz > rangeBounds.max[2]) rangeBounds.max[2] = vz
              vertex += 1
            }
          }
        }
      }
    }
    closeRange()
  }

  // Ranges come out of the loop in shown-first order; the panel wants scene order.
  ranges.sort((a, b) => a.index - b.index)

  // The mesh's own box is what the camera frames, so it covers the objects actually being
  // drawn — a note parked far off to one side and hidden must not zoom the scene out.
  const shown = ranges.filter((range) => !vox.placements[range.index].hidden)
  for (const range of shown.length ? shown : ranges) {
    for (let axis = 0; axis < 3; axis += 1) {
      if (range.bounds.min[axis] < bounds.min[axis]) bounds.min[axis] = range.bounds.min[axis]
      if (range.bounds.max[axis] > bounds.max[axis]) bounds.max[axis] = range.bounds.max[axis]
    }
  }

  return {
    positions: positions.subarray(0, vertex * 3),
    normals: normals.subarray(0, vertex * 3),
    colors: colors.subarray(0, vertex * 3),
    bounds,
    ranges,
    stats: {
      voxels: voxelCount,
      triangles: vertex / 3,
      truncated,
      // How much of the scene made it in: `objects` stops at the placement truncation
      // broke out of, so it is what is on screen rather than what was asked for.
      objects: truncated ? objectCount : vox.placements.length,
      placed: vox.placements.length,
      shown: shown.length,
    },
  }
}

function occupancyGrid(model) {
  const [sx, sy, sz] = model.size
  const grid = new Uint8Array(sx * sy * sz)
  for (let i = 0; i + 3 < model.voxels.length; i += 4) {
    const x = model.voxels[i]
    const y = model.voxels[i + 1]
    const z = model.voxels[i + 2]
    if (x < sx && y < sy && z < sz) grid[x + sx * (y + sy * z)] = model.voxels[i + 3]
  }
  return grid
}

function countFaces(grid, size) {
  const [sx, sy, sz] = size
  let count = 0
  for (let z = 0; z < sz; z += 1) {
    for (let y = 0; y < sy; y += 1) {
      for (let x = 0; x < sx; x += 1) {
        if (grid[x + sx * (y + sy * z)] === 0) continue
        for (const face of FACES) {
          const nx = x + face.normal[0]
          const ny = y + face.normal[1]
          const nz = z + face.normal[2]
          const inside = nx >= 0 && ny >= 0 && nz >= 0 && nx < sx && ny < sy && nz < sz
          if (!inside || grid[nx + sx * (ny + sy * nz)] === 0) count += 1
        }
      }
    }
  }
  return count
}

/* ------------------------------------------------------------------ WebGL viewer */

const VERTEX_SHADER = `
attribute vec3 aPosition;
attribute vec3 aNormal;
attribute vec3 aColor;
uniform mat4 uViewProjection;
varying vec3 vNormal;
varying vec3 vColor;
void main() {
  vNormal = aNormal;
  vColor = aColor;
  gl_Position = uViewProjection * vec4(aPosition, 1.0);
}`

const FRAGMENT_SHADER = `
precision mediump float;
varying vec3 vNormal;
varying vec3 vColor;
void main() {
  // Two-sided: imported meshes are not reliably wound, and a black backface reads as
  // a hole. Lighting is fixed in world space so rotating the model shades it.
  vec3 n = normalize(vNormal);
  if (!gl_FrontFacing) n = -n;
  float key = max(dot(n, normalize(vec3(0.45, 0.8, 0.35))), 0.0);
  float fill = max(dot(n, normalize(vec3(-0.6, 0.15, -0.5))), 0.0);
  float ambient = 0.34 + 0.14 * n.y;
  gl_FragColor = vec4(vColor * (ambient + 0.66 * key + 0.20 * fill), 1.0);
}`

function compileShader(gl, type, source) {
  const shader = gl.createShader(type)
  gl.shaderSource(shader, source)
  gl.compileShader(shader)
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader)
    gl.deleteShader(shader)
    throw new Error(say('webgl.compile', { log }))
  }
  return shader
}

function multiply(out, a, b) {
  for (let column = 0; column < 4; column += 1) {
    for (let row = 0; row < 4; row += 1) {
      out[column * 4 + row] =
        a[row] * b[column * 4] +
        a[4 + row] * b[column * 4 + 1] +
        a[8 + row] * b[column * 4 + 2] +
        a[12 + row] * b[column * 4 + 3]
    }
  }
  return out
}

function perspective(out, fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2)
  out.fill(0)
  out[0] = f / aspect
  out[5] = f
  out[10] = (far + near) / (near - far)
  out[11] = -1
  out[14] = (2 * far * near) / (near - far)
  return out
}

function lookAt(out, eye, center, up) {
  let z0 = eye[0] - center[0]
  let z1 = eye[1] - center[1]
  let z2 = eye[2] - center[2]
  let length = Math.hypot(z0, z1, z2) || 1
  z0 /= length
  z1 /= length
  z2 /= length
  let x0 = up[1] * z2 - up[2] * z1
  let x1 = up[2] * z0 - up[0] * z2
  let x2 = up[0] * z1 - up[1] * z0
  length = Math.hypot(x0, x1, x2)
  if (length === 0) {
    x0 = 1
    x1 = 0
    x2 = 0
  } else {
    x0 /= length
    x1 /= length
    x2 /= length
  }
  const y0 = z1 * x2 - z2 * x1
  const y1 = z2 * x0 - z0 * x2
  const y2 = z0 * x1 - z1 * x0
  out[0] = x0
  out[1] = y0
  out[2] = z0
  out[3] = 0
  out[4] = x1
  out[5] = y1
  out[6] = z1
  out[7] = 0
  out[8] = x2
  out[9] = y2
  out[10] = z2
  out[11] = 0
  out[12] = -(x0 * eye[0] + x1 * eye[1] + x2 * eye[2])
  out[13] = -(y0 * eye[0] + y1 * eye[1] + y2 * eye[2])
  out[14] = -(z0 * eye[0] + z1 * eye[1] + z2 * eye[2])
  out[15] = 1
  return out
}

/**
 * A deliberately small orbit viewer: one program, three buffers, no scene graph.
 *
 * The Studio ships no 3D library (§14) and a plugin must not bundle a 500 kB one to
 * show a preview thumbnail, so flat-shaded triangles with a fixed light is the whole
 * feature set.
 */
export function createViewer(canvas) {
  // `alpha: true` with a transparent clear lets the pane's own CSS background show
  // through, so the viewer follows the light/dark theme without reading any token.
  const gl = canvas.getContext('webgl', { antialias: true, alpha: true, premultipliedAlpha: false })
  if (!gl) throw new Error(say('webgl.unavailable'))

  const program = gl.createProgram()
  gl.attachShader(program, compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER))
  gl.attachShader(program, compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER))
  gl.linkProgram(program)
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(say('webgl.link', { log: gl.getProgramInfoLog(program) }))
  }

  const attributes = {
    position: gl.getAttribLocation(program, 'aPosition'),
    normal: gl.getAttribLocation(program, 'aNormal'),
    color: gl.getAttribLocation(program, 'aColor'),
  }
  const uViewProjection = gl.getUniformLocation(program, 'uViewProjection')
  const buffers = {
    position: gl.createBuffer(),
    normal: gl.createBuffer(),
    color: gl.createBuffer(),
  }

  const projection = new Float32Array(16)
  const view = new Float32Array(16)
  const viewProjection = new Float32Array(16)
  const camera = { yaw: 0.85, pitch: 0.5, distance: 4, target: [0, 0, 0], radius: 1 }
  let vertexCount = 0
  let hasColors = false
  let flatColor = [0.74, 0.76, 0.82]
  let pending = 0
  let disposed = false
  // Objects live in one pair of buffers; hiding one is a change to what gets drawn, not a
  // rebuild. `runs` is the draw list — neighbouring visible objects are merged so a scene
  // with everything on still costs a single drawArrays.
  let ranges = []
  let runs = []
  // A Set of placement indices, or null for "everything".
  let visible = null

  gl.enable(gl.DEPTH_TEST)
  gl.disable(gl.CULL_FACE)

  function requestRender() {
    if (!pending && !disposed) pending = requestAnimationFrame(render)
  }

  function render() {
    pending = 0
    if (disposed) return
    const ratio = Math.min(window.devicePixelRatio || 1, 2)
    const width = Math.max(1, Math.round(canvas.clientWidth * ratio))
    const height = Math.max(1, Math.round(canvas.clientHeight * ratio))
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width
      canvas.height = height
    }

    gl.viewport(0, 0, width, height)
    gl.clearColor(0, 0, 0, 0)
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT)
    if (vertexCount === 0 || runs.length === 0) return

    const eye = eyePosition()
    // Near/far track the model size, so a 4-voxel gizmo and a 256³ scene both keep
    // usable depth precision.
    const near = Math.max(camera.radius * 0.005, camera.distance * 0.01)
    perspective(projection, (50 * Math.PI) / 180, width / height, near, camera.distance + camera.radius * 8)
    lookAt(view, eye, camera.target, [0, 1, 0])
    multiply(viewProjection, projection, view)

    gl.useProgram(program)
    gl.uniformMatrix4fv(uViewProjection, false, viewProjection)

    bindFloat(buffers.position, attributes.position)
    bindFloat(buffers.normal, attributes.normal)
    if (hasColors) {
      gl.enableVertexAttribArray(attributes.color)
      gl.bindBuffer(gl.ARRAY_BUFFER, buffers.color)
      gl.vertexAttribPointer(attributes.color, 3, gl.UNSIGNED_BYTE, true, 0, 0)
    } else {
      gl.disableVertexAttribArray(attributes.color)
      gl.vertexAttrib3f(attributes.color, flatColor[0], flatColor[1], flatColor[2])
    }
    for (const run of runs) gl.drawArrays(gl.TRIANGLES, run.first, run.count)
  }

  function bindFloat(buffer, location) {
    gl.enableVertexAttribArray(location)
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
    gl.vertexAttribPointer(location, 3, gl.FLOAT, false, 0, 0)
  }

  function eyePosition() {
    const cos = Math.cos(camera.pitch)
    return [
      camera.target[0] + camera.distance * cos * Math.sin(camera.yaw),
      camera.target[1] + camera.distance * Math.sin(camera.pitch),
      camera.target[2] + camera.distance * cos * Math.cos(camera.yaw),
    ]
  }

  function setMesh(mesh) {
    gl.bindBuffer(gl.ARRAY_BUFFER, buffers.position)
    gl.bufferData(gl.ARRAY_BUFFER, mesh.positions, gl.STATIC_DRAW)
    gl.bindBuffer(gl.ARRAY_BUFFER, buffers.normal)
    gl.bufferData(gl.ARRAY_BUFFER, mesh.normals, gl.STATIC_DRAW)
    hasColors = Boolean(mesh.colors && mesh.colors.length === mesh.positions.length)
    if (hasColors) {
      gl.bindBuffer(gl.ARRAY_BUFFER, buffers.color)
      gl.bufferData(gl.ARRAY_BUFFER, mesh.colors, gl.STATIC_DRAW)
    }
    if (mesh.color) flatColor = mesh.color
    vertexCount = mesh.positions.length / 3

    // A mesh with no ranges — one undivided blob — draws as a single run, so the viewer
    // stays usable for callers that know nothing about objects.
    ranges = mesh.ranges ?? []
    visible = null
    if (ranges.length) updateRuns(null)
    else runs = [{ first: 0, count: vertexCount }]

    // Camera before the frame is asked for: every step here is a pure state change and
    // the one render request comes last, so setMesh cannot draw a half-configured view.
    frame(mesh.bounds)
    resetCamera()
    requestRender()
  }

  /** Point the camera at a box without changing the orbit angles. */
  function frame({ min, max }) {
    if (!Number.isFinite(min[0])) return
    camera.radius = Math.max(Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) / 2, 0.001)
    camera.target = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2]
  }

  /**
   * Rebuild the draw list for `indices` (a Set of placement indices, or `null` for all).
   *
   * Neighbouring visible objects are merged into one run, so the common cases — nothing
   * hidden, or one object hidden — cost one or two draw calls rather than one per object.
   */
  function updateRuns(indices) {
    visible = indices
    const drawn = indices ? ranges.filter((range) => indices.has(range.index)) : ranges.slice()
    drawn.sort((a, b) => a.first - b.first)

    runs = []
    for (const range of drawn) {
      const last = runs[runs.length - 1]
      if (last && last.first + last.count === range.first) last.count += range.count
      else runs.push({ first: range.first, count: range.count })
    }
  }

  /** Choose which objects are drawn, by placement index. `null` means all of them. */
  function setVisible(indices) {
    updateRuns(indices)
    requestRender()
  }

  /** Frame whatever is currently visible, then look at it from the default angles. */
  function frameVisible() {
    const bounds = { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] }
    const drawn = visible ? ranges.filter((range) => visible.has(range.index)) : ranges
    for (const range of drawn) {
      for (let axis = 0; axis < 3; axis += 1) {
        if (range.bounds.min[axis] < bounds.min[axis]) bounds.min[axis] = range.bounds.min[axis]
        if (range.bounds.max[axis] > bounds.max[axis]) bounds.max[axis] = range.bounds.max[axis]
      }
    }
    frame(bounds)
    resetCamera()
    requestRender()
  }

  function resetCamera() {
    camera.yaw = 0.85
    camera.pitch = 0.5
    camera.distance = camera.radius / Math.sin((50 * Math.PI) / 360) + camera.radius * 0.2
  }

  function resetView() {
    resetCamera()
    requestRender()
  }

  /* ---- interaction: drag orbits, shift/middle-drag pans, wheel zooms ---- */

  let dragging = null

  function onPointerDown(event) {
    canvas.setPointerCapture(event.pointerId)
    dragging = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      pan: event.button === 1 || event.shiftKey,
    }
    canvas.style.cursor = 'grabbing'
  }

  function onPointerMove(event) {
    if (!dragging || dragging.id !== event.pointerId) return
    const dx = event.clientX - dragging.x
    const dy = event.clientY - dragging.y
    dragging.x = event.clientX
    dragging.y = event.clientY

    if (dragging.pan) {
      // Scale by what one pixel spans at the target, so panning tracks the cursor.
      const perPixel =
        (2 * camera.distance * Math.tan((50 * Math.PI) / 360)) / Math.max(canvas.clientHeight, 1)
      const eye = eyePosition()
      const forward = normalise([
        camera.target[0] - eye[0],
        camera.target[1] - eye[1],
        camera.target[2] - eye[2],
      ])
      const right = normalise(cross(forward, [0, 1, 0]))
      const up = cross(right, forward)
      for (let axis = 0; axis < 3; axis += 1) {
        camera.target[axis] += (-dx * right[axis] + dy * up[axis]) * perPixel
      }
    } else {
      camera.yaw -= dx * 0.008
      camera.pitch = Math.max(-1.5, Math.min(1.5, camera.pitch + dy * 0.008))
    }
    requestRender()
  }

  function onPointerUp(event) {
    if (!dragging || dragging.id !== event.pointerId) return
    dragging = null
    canvas.style.cursor = 'grab'
  }

  function onWheel(event) {
    event.preventDefault()
    const scale = Math.exp(event.deltaY * 0.0015)
    camera.distance = Math.max(camera.radius * 0.05, Math.min(camera.radius * 40, camera.distance * scale))
    requestRender()
  }

  canvas.addEventListener('pointerdown', onPointerDown)
  canvas.addEventListener('pointermove', onPointerMove)
  canvas.addEventListener('pointerup', onPointerUp)
  canvas.addEventListener('pointercancel', onPointerUp)
  canvas.addEventListener('wheel', onWheel, { passive: false })
  // Fit rather than merely reset: after isolating one object, framing the whole scene
  // again would leave the user staring at empty space where the rest used to be.
  canvas.addEventListener('dblclick', frameVisible)
  canvas.style.cursor = 'grab'

  const observer = new ResizeObserver(requestRender)
  observer.observe(canvas)

  return {
    setMesh,
    setVisible,
    frameVisible,
    resetView,
    requestRender,
    dispose() {
      disposed = true
      if (pending) cancelAnimationFrame(pending)
      observer.disconnect()
      canvas.removeEventListener('pointerdown', onPointerDown)
      canvas.removeEventListener('pointermove', onPointerMove)
      canvas.removeEventListener('pointerup', onPointerUp)
      canvas.removeEventListener('pointercancel', onPointerUp)
      canvas.removeEventListener('wheel', onWheel)
      canvas.removeEventListener('dblclick', frameVisible)
      // Browsers cap live WebGL contexts (~16). Clicking through a folder of models
      // would blow past that, so hand the context back instead of waiting for GC.
      gl.getExtension('WEBGL_lose_context')?.loseContext()
    },
  }
}

function cross(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}

function normalise(v) {
  const length = Math.hypot(v[0], v[1], v[2]) || 1
  return [v[0] / length, v[1] / length, v[2] / length]
}

/* ------------------------------------------------------------------ Vue component */

/** Feather's eye / eye-off, inlined — a plugin cannot reach the app's icon set. */
function eyeIcon(h, open) {
  const path = (d) => h('path', { d })
  return h(
    'svg',
    {
      width: '13',
      height: '13',
      viewBox: '0 0 24 24',
      fill: 'none',
      stroke: 'currentColor',
      'stroke-width': '2',
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
      style: { flex: '0 0 auto' },
    },
    open
      ? [path('M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z'), h('circle', { cx: '12', cy: '12', r: '3' })]
      : [
          path('M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94'),
          path('M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19'),
          path('M14.12 14.12a3 3 0 1 1-4.24-4.24'),
          h('line', { x1: '1', y1: '1', x2: '23', y2: '23' }),
        ],
  )
}

export default function ({ vue, rawFileBytes, i18n }) {
  const { computed, defineComponent, h, onBeforeUnmount, onMounted, ref } = vue
  const t = i18n.translator(MESSAGES)
  // The reader and the viewer speak the interface language from here on, too.
  translate = t

  return defineComponent({
    name: 'VoxPreview',
    props: {
      path: { type: String, required: true },
      preview: { type: Object, default: null },
    },
    setup(props) {
      const canvas = ref(null)
      const error = ref(null)
      const stats = ref(null)
      const loading = ref(true)
      // One row per object in the scene graph, in scene order. `shown` starts from what
      // MagicaVoxel had visible and is what the eye toggles.
      const objects = ref([])
      const collapsed = ref(false)
      let viewer = null

      const shownCount = computed(() => objects.value.filter((object) => object.shown).length)

      function applyVisibility() {
        viewer?.setVisible(new Set(objects.value.filter((o) => o.shown).map((o) => o.index)))
      }

      function toggle(object) {
        object.shown = !object.shown
        applyVisibility()
      }

      function setAll(shown) {
        for (const object of objects.value) object.shown = shown
        applyVisibility()
      }

      /** Alt-click an eye to isolate that object — the fastest way through a busy scene. */
      function solo(object) {
        const alone = shownCount.value === 1 && object.shown
        for (const other of objects.value) other.shown = alone || other === object
        applyVisibility()
      }

      onMounted(async () => {
        try {
          const vox = parseVoxelFile(await rawFileBytes(props.path))
          if (!vox.models.length) throw new Error(t('noModels'))
          const mesh = buildVoxelMesh(vox)
          viewer = createViewer(canvas.value)
          viewer.setMesh(mesh)

          const drawable = new Set(mesh.ranges.map((range) => range.index))
          objects.value = vox.placements.map((placement, index) => ({
            index,
            // Empty for an object the file leaves unnamed; the row words that when it renders.
            name: placement.name,
            size: vox.models[placement.model].size,
            model: placement.model,
            shown: !placement.hidden,
            // Truncation and empty models both leave an object with nothing to draw; the
            // row is still listed, but saying so beats an eye that does nothing.
            empty: !drawable.has(index),
          }))
          applyVisibility()

          stats.value = {
            ...mesh.stats,
            models: vox.models.length,
            hidden: vox.hidden,
            // The scene's extent, not the first model's grid — with everything placed,
            // those are the same number only for a single-object file.
            size: vox.bounds.size,
          }
        } catch (cause) {
          error.value = cause instanceof Error ? cause.message : String(cause)
        } finally {
          loading.value = false
        }
      })

      onBeforeUnmount(() => viewer?.dispose())

      const overlay = (children, tone) =>
        h(
          'div',
          {
            style: {
              position: 'absolute',
              inset: '0',
              display: 'grid',
              placeItems: 'center',
              padding: 'var(--vxs-pane-pad)',
              textAlign: 'center',
              color: tone ?? 'var(--vxs-text-muted)',
            },
          },
          children,
        )

      const iconButton = (props, children) =>
        h(
          'button',
          {
            type: 'button',
            ...props,
            style: {
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: 'none',
              border: 'none',
              padding: '2px',
              cursor: 'pointer',
              color: 'inherit',
              font: 'inherit',
              ...(props.style ?? {}),
            },
          },
          children,
        )

      const objectRow = (object) => {
        const name = object.name || t('objects.unnamed', { number: object.index + 1 })
        return h(
          'div',
          {
            key: object.index,
            style: {
              display: 'flex',
              alignItems: 'center',
              gap: 'var(--vxs-space-2)',
              padding: '2px 0',
              opacity: object.shown ? '1' : '0.45',
            },
          },
          [
            iconButton(
              {
                title: object.empty ? t('objects.empty') : t('objects.toggle'),
                disabled: object.empty,
                onClick: (event) => (event.altKey ? solo(object) : toggle(object)),
                style: {
                  color: object.shown ? 'var(--vxs-text)' : 'var(--vxs-text-faint)',
                  cursor: object.empty ? 'default' : 'pointer',
                },
              },
              eyeIcon(h, object.shown),
            ),
            h(
              'span',
              {
                title: name,
                style: {
                  flex: '1 1 auto',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  textDecoration: object.empty ? 'line-through' : 'none',
                },
              },
              name,
            ),
            h(
              'span',
              { style: { color: 'var(--vxs-text-faint)', fontVariantNumeric: 'tabular-nums' } },
              object.size.join('×'),
            ),
          ],
        )
      }

      const objectPanel = () =>
        h(
          'div',
          {
            style: {
              position: 'absolute',
              top: 'var(--vxs-space-3)',
              right: 'var(--vxs-space-3)',
              display: 'flex',
              flexDirection: 'column',
              width: '13.5rem',
              maxHeight: 'calc(100% - var(--vxs-space-3) * 2 - 1.75rem)',
              borderRadius: 'var(--vxs-radius-md)',
              background: 'var(--vxs-bg-surface)',
              border: '1px solid var(--vxs-border)',
              color: 'var(--vxs-text-muted)',
              fontSize: '11px',
            },
          },
          [
            h(
              'div',
              {
                style: {
                  display: 'flex',
                  alignItems: 'center',
                  gap: 'var(--vxs-space-2)',
                  padding: '4px 8px',
                  borderBottom: collapsed.value ? 'none' : '1px solid var(--vxs-border)',
                },
              },
              [
                iconButton(
                  {
                    title: collapsed.value ? t('objects.expand') : t('objects.collapse'),
                    onClick: () => (collapsed.value = !collapsed.value),
                    style: { flex: '1 1 auto', justifyContent: 'flex-start', gap: '6px' },
                  },
                  [
                    h(
                      'span',
                      {
                        style: {
                          display: 'inline-block',
                          transition: 'transform 120ms',
                          transform: collapsed.value ? 'rotate(-90deg)' : 'none',
                        },
                      },
                      '▾',
                    ),
                    h(
                      'span',
                      t('objects.shown', {
                        shown: shownCount.value.toLocaleString(i18n.locale.value),
                        count: objects.value.length,
                      }),
                    ),
                  ],
                ),
                iconButton(
                  {
                    title:
                      shownCount.value === objects.value.length
                        ? t('objects.hideAll')
                        : t('objects.showAll'),
                    onClick: () => setAll(shownCount.value !== objects.value.length),
                  },
                  eyeIcon(h, shownCount.value !== objects.value.length),
                ),
              ],
            ),

            collapsed.value
              ? null
              : h(
                  'div',
                  { style: { overflowY: 'auto', padding: '4px 8px 6px' } },
                  objects.value.map(objectRow),
                ),
          ],
        )

      return () =>
        h('div', { style: { position: 'relative', height: '100%', width: '100%' } }, [
          // The canvas is always mounted: `onMounted` needs the ref, and a v-if would
          // leave it null on the first pass.
          h('canvas', {
            ref: canvas,
            style: {
              display: 'block',
              height: '100%',
              width: '100%',
              touchAction: 'none',
              visibility: error.value ? 'hidden' : 'visible',
            },
          }),

          loading.value ? overlay(t('reading')) : null,
          error.value ? overlay(t('unreadable', { reason: error.value }), 'var(--vxs-danger)') : null,

          // Shown for a single-object file too, even though its one eye can only blank the
          // pane. The panel is how you tell the viewer read the scene at all, so making it
          // conditional means a missing panel reads as "the plugin is stale" — which is
          // exactly the confusion that a >1 threshold caused.
          objects.value.length ? objectPanel() : null,

          stats.value
            ? h(
                'div',
                {
                  style: {
                    position: 'absolute',
                    left: 'var(--vxs-space-3)',
                    bottom: 'var(--vxs-space-3)',
                    display: 'flex',
                    gap: 'var(--vxs-space-3)',
                    padding: '4px 10px',
                    borderRadius: 'var(--vxs-radius-md)',
                    background: 'var(--vxs-bg-surface)',
                    border: '1px solid var(--vxs-border)',
                    color: 'var(--vxs-text-faint)',
                    fontSize: '11px',
                    pointerEvents: 'none',
                  },
                },
                // Object counts live in the panel; this line describes the mesh, which
                // does not change as eyes are toggled.
                [
                  h(
                    'span',
                    t(stats.value.placed > 1 ? 'stats.scene' : 'stats.grid', {
                      size: stats.value.size.map(value => value.toLocaleString(i18n.locale.value, { maximumFractionDigits: 2 })).join('×'),
                    }),
                  ),
                  h('span', t('stats.voxels', { count: stats.value.voxels })),
                  h('span', t('stats.tris', { count: stats.value.triangles })),
                  stats.value.placed > 1 && stats.value.models !== stats.value.placed
                    ? h('span', t('stats.models', { count: stats.value.models }))
                    : null,
                  stats.value.truncated
                    ? h(
                        'span',
                        { style: { color: 'var(--vxs-warning)' } },
                        t('stats.truncated', {
                          shown: stats.value.objects.toLocaleString(i18n.locale.value),
                          count: stats.value.placed,
                        }),
                      )
                    : null,
                ],
              )
            : null,

          error.value
            ? null
            : h(
                'div',
                {
                  style: {
                    position: 'absolute',
                    right: 'var(--vxs-space-3)',
                    bottom: 'var(--vxs-space-3)',
                    // Wraps upward inside the right half rather than running under the stats
                    // strip on the left — which a longer language reaches in a narrow pane.
                    maxWidth: '45%',
                    textAlign: 'right',
                    color: 'var(--vxs-text-faint)',
                    fontSize: '11px',
                    pointerEvents: 'none',
                  },
                },
                t('hint'),
              ),
        ])
    },
  })
}
