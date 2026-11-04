/**
 * Mesh (.glb/.gltf/.stl/.obj) previewer for the Explorer preview pane (spec §6.2).
 *
 * Loaded at runtime as a plain ES module from `/api/plugins/mesh_preview/assets/…`, so
 * it must have no imports at all: `vue` arrives through the factory (a second Vue
 * instance would share no reactivity), and the format readers and the renderer live
 * here.
 *
 * One file on purpose. A dynamic `import()` cannot send an auth header, so the host
 * appends `?token=`; a relative `import './viewer.js'` would resolve without it and
 * 401 in server mode. The viewer below is therefore a near-copy of the one in
 * fbx_preview — plugins are installed independently and must not import each other.
 *
 * The readers are the same three the pipeline already has in Python
 * (`hunyuan3d/nodes/meshio.py`, which is what `hunyuan3d.voxelize` is fed by), and where a
 * format left a choice open this file makes the same one: node transforms composed and
 * applied before anything else looks at the geometry, n-gons fanned, negative OBJ
 * indices counted back from the vertices seen so far. Two readers of one file that
 * disagree about what it contains are worse than either being wrong.
 *
 * The parsing entry points are exported so they can be exercised head-lessly in Node,
 * where `atob`, `TextDecoder` and typed arrays behave exactly as they do in the browser.
 */

/* ------------------------------------------------------------------ messages */

/**
 * What the previewer says, in the Studio's interface languages (§6.4).
 *
 * The component words its own text while it renders. The readers and the viewer below run
 * outside it — and head-lessly in Node, with no host at all — so what they throw and warn is
 * worded by `say()` when it happens: in the interface language once the factory has run, in
 * English before. A preview reads its file each time it is mounted, so those sentences are
 * in the language the file was opened in. Names out of the file — materials, images,
 * attributes — are data and stay as the file spells them.
 */
const MESSAGES = {
  en: {
    reading: 'Reading mesh…',
    unreadable: 'Could not read this {suffix} file — {reason}',
    unreadableMesh: 'Could not read this mesh file — {reason}',
    stats: {
      tris: { one: '{count} tri', other: '{count} tris' },
      meshes: { one: '{count} mesh', other: '{count} meshes' },
      primitives: { one: '{count} primitive', other: '{count} primitives' },
      textured: 'textured',
      textures: { one: '{count} texture', other: '{count} textures' },
      truncated: {
        one: 'truncated at {shown} of {count} triangle',
        other: 'truncated at {shown} of {count} triangles',
      },
    },
    hint: 'drag to orbit · shift-drag to pan · scroll to zoom · double-click to fit',
    noReader: 'No mesh reader for {suffix}',
    noReaderWithoutSuffix: 'No mesh reader for a suffixless file',
    stl: {
      tooSmall: 'This file is too small to be an STL',
      unreadable: 'Could not read this as a binary or an ASCII STL',
      noFacets: 'This ASCII STL declares no complete facets',
    },
    obj: {
      noFaces: 'This OBJ file declares no faces',
      missingVertex: 'This OBJ face references vertex {vertex}, which does not exist',
    },
    gltf: {
      dracoSkipped: {
        one: '{count} Draco-compressed primitive skipped',
        other: '{count} Draco-compressed primitives skipped',
      },
      allDraco: 'Every primitive in this file is Draco-compressed, which this viewer cannot decode',
      noTriangles: 'This glTF file contains no triangles',
      tooSmall: 'This GLB is too small to hold a header',
      glbVersion: 'Only glTF 2.0 is supported; this GLB declares version {version}',
      truncatedChunk: 'Truncated glTF chunk',
      noJsonChunk: 'This GLB has no JSON chunk',
      invalidJson: 'This file is not valid glTF JSON — {reason}',
      fileVersion: 'Only glTF 2.0 is supported; this file declares version {version}',
      missingBuffer: 'This glTF names a buffer it does not carry',
      emptyBuffer: 'Buffer {index} has no data and this file has no BIN chunk for it',
      glbExternal:
        'This GLB keeps part of its data in a separate file ({name}), which cannot be read from the preview pane',
      gltfExternal:
        'This .gltf keeps its geometry in a separate file ({name}), which cannot be read from the preview pane — open the .glb instead',
      badDataUri: 'Malformed data: URI in this glTF',
      missingAccessor: 'glTF accessor {index} does not exist',
      sparse: 'Sparse glTF accessors are not supported',
      unsupportedAccessor: 'Unsupported glTF accessor {type}/{componentType} for {attribute}',
      quantized: 'This glTF stores {attribute} in a quantized form this viewer cannot read',
      noBufferView: 'glTF accessor without a bufferView is not supported',
      missingBufferView: 'glTF bufferView {index} does not exist',
      pastBuffer: 'This glTF reads past the end of its own buffer',
    },
    texture: {
      namedMaterial: '"{name}"',
      material: 'A material',
      baseColour: 'base colour',
      noTexCoord: '{name} is textured but its geometry carries no TEXCOORD_{index}',
      missingTexture: '{material} names texture {index}, which this file does not carry',
      transform: '{material} uses KHR_texture_transform, which this viewer does not apply',
      basis: '{material} stores its texture as Basis/KTX2, which this viewer cannot decode',
      noImage: '{material} names a texture with no image',
      missingImage: '{material} names image {index}, which this file does not carry',
      external:
        '{material} keeps its texture in a separate file ({name}), which cannot be read from the preview pane',
      missingBufferView: '{material} names bufferView {index}, which this file does not carry',
      pastBuffer: '{material} stores its texture past the end of the buffer',
      noSource: '{material} names an image with neither a URI nor a bufferView',
      empty: '{material} names an empty image',
      undecodable: 'could not decode the {name} texture — {reason}',
      browserRefused: 'the browser could not decode it',
    },
    build: {
      noTriangles: 'This file has geometry but no triangles',
      noUsableTriangles: 'This file has geometry but no usable triangles',
    },
    webgl: {
      unavailable: 'WebGL is not available in this browser',
      compile: 'Shader failed to compile: {log}',
      link: 'Shader program failed to link: {log}',
    },
  },
  ru: {
    reading: 'Чтение меша…',
    unreadable: 'Не удалось прочитать файл {suffix} — {reason}',
    unreadableMesh: 'Не удалось прочитать файл меша — {reason}',
    stats: {
      tris: {
        one: '{count} треугольник',
        few: '{count} треугольника',
        many: '{count} треугольников',
        other: '{count} треугольника',
      },
      meshes: { one: '{count} меш', few: '{count} меша', many: '{count} мешей', other: '{count} меша' },
      primitives: {
        one: '{count} примитив',
        few: '{count} примитива',
        many: '{count} примитивов',
        other: '{count} примитива',
      },
      textured: 'с текстурой',
      textures: {
        one: '{count} текстура',
        few: '{count} текстуры',
        many: '{count} текстур',
        other: '{count} текстуры',
      },
      truncated: {
        one: 'показано {shown} из {count} треугольника',
        few: 'показано {shown} из {count} треугольников',
        many: 'показано {shown} из {count} треугольников',
        other: 'показано {shown} из {count} треугольника',
      },
    },
    hint: 'вращение: ЛКМ · сдвиг: Shift+ЛКМ · масштаб: колесо · вписать: двойной клик',
    noReader: 'Для {suffix} нет модуля чтения мешей',
    noReaderWithoutSuffix: 'Для файла без расширения нет модуля чтения мешей',
    stl: {
      tooSmall: 'Файл слишком мал для STL',
      unreadable: 'Файл не читается ни как двоичный STL, ни как ASCII STL',
      noFacets: 'В этом ASCII STL нет ни одной полной грани',
    },
    obj: {
      noFaces: 'В этом файле OBJ не объявлено ни одной грани',
      missingVertex: 'Грань OBJ ссылается на вершину {vertex}, которой не существует',
    },
    gltf: {
      dracoSkipped: {
        one: 'пропущен {count} примитив со сжатием Draco',
        few: 'пропущено {count} примитива со сжатием Draco',
        many: 'пропущено {count} примитивов со сжатием Draco',
        other: 'пропущено {count} примитива со сжатием Draco',
      },
      allDraco: 'Все примитивы в этом файле сжаты Draco, а предпросмотр не умеет его декодировать',
      noTriangles: 'В этом файле glTF нет треугольников',
      tooSmall: 'Этот GLB слишком мал, чтобы вместить заголовок',
      glbVersion: 'Поддерживается только glTF 2.0, а этот GLB объявляет версию {version}',
      truncatedChunk: 'Блок glTF обрезан',
      noJsonChunk: 'В этом GLB нет блока JSON',
      invalidJson: 'Этот файл не является корректным JSON glTF — {reason}',
      fileVersion: 'Поддерживается только glTF 2.0, а этот файл объявляет версию {version}',
      missingBuffer: 'Этот glTF ссылается на буфер, которого в нём нет',
      emptyBuffer: 'У буфера {index} нет данных, а в файле нет для него блока BIN',
      glbExternal:
        'Этот GLB хранит часть данных в отдельном файле ({name}), который нельзя прочитать из панели предпросмотра',
      gltfExternal:
        'Этот .gltf хранит геометрию в отдельном файле ({name}), который нельзя прочитать из панели предпросмотра, — откройте вместо него .glb',
      badDataUri: 'Некорректный data: URI в этом glTF',
      missingAccessor: 'В glTF нет аксессора {index}',
      sparse: 'Разреженные аксессоры glTF не поддерживаются',
      unsupportedAccessor: 'Неподдерживаемый аксессор glTF {type}/{componentType} для {attribute}',
      quantized: 'Этот glTF хранит {attribute} в квантованном виде, который предпросмотр не умеет читать',
      noBufferView: 'Аксессор glTF без bufferView не поддерживается',
      missingBufferView: 'В glTF нет bufferView {index}',
      pastBuffer: 'Этот glTF читает за концом собственного буфера',
    },
    texture: {
      namedMaterial: 'Материал «{name}»',
      material: 'Материал',
      baseColour: 'базовый цвет',
      noTexCoord: '«{name}»: текстура есть, но в геометрии нет TEXCOORD_{index}',
      missingTexture: '{material} ссылается на текстуру {index}, которой нет в этом файле',
      transform: '{material} использует KHR_texture_transform, а предпросмотр его не применяет',
      basis: '{material} хранит текстуру в формате Basis/KTX2, а предпросмотр не умеет его декодировать',
      noImage: '{material} ссылается на текстуру без изображения',
      missingImage: '{material} ссылается на изображение {index}, которого нет в этом файле',
      external:
        '{material} хранит текстуру в отдельном файле ({name}), который нельзя прочитать из панели предпросмотра',
      missingBufferView: '{material} ссылается на bufferView {index}, которого нет в этом файле',
      pastBuffer: '{material} хранит текстуру за концом буфера',
      noSource: '{material} ссылается на изображение без URI и без bufferView',
      empty: '{material} ссылается на пустое изображение',
      undecodable: 'не удалось декодировать текстуру «{name}» — {reason}',
      browserRefused: 'браузер не смог её декодировать',
    },
    build: {
      noTriangles: 'В этом файле есть геометрия, но нет треугольников',
      noUsableTriangles: 'В этом файле есть геометрия, но нет пригодных треугольников',
    },
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

/* ------------------------------------------------------------------ constants */

const TEXT = new TextDecoder()

/** Flat shading duplicates every vertex, so a preview has to draw a line somewhere. */
const MAX_TRIANGLES = 500_000

/** The colour a surface takes when its file names none. Same value fbx_preview uses. */
const DEFAULT_COLOR = [0.72, 0.75, 0.82]

/** glTF's own default `baseColorFactor`, and so the tint a texture is left alone by. */
const WHITE = [1, 1, 1]

/**
 * What every reader answers with, before the cap and the flat-shading pass.
 *
 * A `part` is one drawable run — an STL solid, an OBJ file, one glTF primitive as one
 * node places it — with `positions` already in world space, `indices` a plain triangle
 * list (or null when the positions are already three-per-triangle), `normals` parallel
 * to `positions` (or null), `color` a flat sRGB triple in 0…1 (or null for the default
 * grey), `uvs` two floats per vertex (or null), and `texture` an index into the parsed
 * result's `textures` (or null). A part carrying both a texture and UVs is drawn with
 * `color` as a tint over the sampled texel, which is what glTF's `baseColorFactor`
 * means; a part with one and not the other is drawn flat, because half of a material
 * is not a material.
 */

/* ------------------------------------------------------------------ dispatch */

/** Pick a reader by suffix, the way `meshio.read_triangles` does on the Python side. */
export function parseMesh(path, buffer) {
  const suffix = (path.match(/\.[^./\\]+$/)?.[0] ?? '').toLowerCase()
  if (suffix === '.stl') return parseStl(buffer)
  if (suffix === '.obj') return parseObj(buffer)
  if (suffix === '.glb' || suffix === '.gltf') return parseGlb(buffer)
  throw new Error(suffix ? say('noReader', { suffix }) : say('noReaderWithoutSuffix'))
}

/* ------------------------------------------------------------------ STL */

/**
 * Read a binary or an ASCII STL.
 *
 * Which one it is comes from the arithmetic, never from the leading word: a binary STL
 * whose 80-byte header happens to begin `solid` is common enough that exporters warn
 * about it. A file whose declared triangle count accounts for its size exactly is
 * binary; anything else is offered to the ASCII reader first, and only a file that is
 * *larger* than its count implies — a binary STL with something appended — comes back
 * to the binary path.
 */
export function parseStl(buffer) {
  const bytes = new Uint8Array(buffer)
  if (bytes.length < 84) throw new Error(say('stl.tooSmall'))

  const view = new DataView(buffer)
  const count = view.getUint32(80, true)
  const declared = 84 + count * 50
  // A count of zero is what a file that is not an STL at all looks like from here, so
  // it never takes the binary path however well the arithmetic works out.
  if (count > 0 && declared === bytes.length) return binaryStl(view, count)

  const head = TEXT.decode(bytes.subarray(0, 512))
  if (/^\s*solid/.test(head) && /facet\s+normal/.test(head)) return asciiStl(bytes)
  if (count > 0 && declared < bytes.length) return binaryStl(view, count)
  throw new Error(say('stl.unreadable'))
}

function binaryStl(view, count) {
  const positions = new Float32Array(count * 9)
  // The per-facet normal is the one thing an STL says about shading, so it is honoured
  // where it is usable. Exporters that write zeros are common; the builder falls back
  // to the geometric normal per triangle, which is what those files needed anyway.
  const normals = new Float32Array(count * 9)
  for (let t = 0; t < count; t += 1) {
    const record = 84 + t * 50
    const nx = view.getFloat32(record, true)
    const ny = view.getFloat32(record + 4, true)
    const nz = view.getFloat32(record + 8, true)
    for (let corner = 0; corner < 3; corner += 1) {
      const at = t * 9 + corner * 3
      positions[at] = view.getFloat32(record + 12 + corner * 12, true)
      positions[at + 1] = view.getFloat32(record + 16 + corner * 12, true)
      positions[at + 2] = view.getFloat32(record + 20 + corner * 12, true)
      normals[at] = nx
      normals[at + 1] = ny
      normals[at + 2] = nz
    }
  }
  return stlResult(positions, normals, 'binary')
}

function asciiStl(bytes) {
  const positions = []
  const normals = []
  let facet = [NaN, NaN, NaN]
  let corners = 0
  for (const line of TEXT.decode(bytes).split('\n')) {
    const parts = line.trim().split(/\s+/)
    if (parts[0] === 'facet' && parts[1] === 'normal' && parts.length >= 5) {
      facet = [Number(parts[2]), Number(parts[3]), Number(parts[4])]
      corners = 0
    } else if (parts[0] === 'vertex' && parts.length >= 4) {
      // A malformed facet with four vertices would otherwise shift every later
      // triangle by one corner; take the first three and let the rest go.
      if (corners >= 3) continue
      positions.push(Number(parts[1]), Number(parts[2]), Number(parts[3]))
      normals.push(facet[0], facet[1], facet[2])
      corners += 1
    }
  }
  if (positions.length < 9) throw new Error(say('stl.noFacets'))
  const usable = Math.floor(positions.length / 9) * 9
  return stlResult(
    new Float32Array(positions.slice(0, usable)),
    new Float32Array(normals.slice(0, usable)),
    'ascii',
  )
}

function stlResult(positions, normals, framing) {
  return {
    format: 'stl',
    framing,
    parts: [{ name: 'solid', positions, normals, indices: null, color: null, uvs: null, texture: null }],
    meshes: 1,
    primitives: 1,
    textures: [],
    warnings: [],
  }
}

/* ------------------------------------------------------------------ OBJ */

/**
 * Read the geometry out of a Wavefront OBJ — `v`, `vn` and `f`, and nothing else.
 *
 * `vt`, `usemtl`, `mtllib`, groups and smoothing bands are skipped: the material
 * library is a second file this viewer cannot fetch, and a preview that ignored it
 * silently would be less honest than one that never looked. Faces are emitted
 * un-indexed because a corner is a `(position, normal)` pair — sharing positions would
 * mean de-duplicating those pairs to keep both, which costs more than it saves on the
 * mesh sizes a preview pane is given.
 */
export function parseObj(buffer) {
  const positions = []
  const normals = []
  const vertices = []
  const vertexNormals = []
  let sawNormals = false

  for (const line of TEXT.decode(new Uint8Array(buffer)).split('\n')) {
    const parts = line.trim().split(/\s+/)
    if (parts[0] === 'v' && parts.length >= 4) {
      vertices.push(Number(parts[1]), Number(parts[2]), Number(parts[3]))
    } else if (parts[0] === 'vn' && parts.length >= 4) {
      vertexNormals.push(Number(parts[1]), Number(parts[2]), Number(parts[3]))
    } else if (parts[0] === 'f' && parts.length >= 4) {
      const corners = parts.slice(1).map((token) => objCorner(token, vertices.length / 3, vertexNormals.length / 3))
      // An n-gon fans out into n-2 triangles, which is what every consumer expects.
      for (let i = 1; i + 1 < corners.length; i += 1) {
        for (const corner of [corners[0], corners[i], corners[i + 1]]) {
          positions.push(
            vertices[corner.v * 3],
            vertices[corner.v * 3 + 1],
            vertices[corner.v * 3 + 2],
          )
          if (corner.n < 0) {
            // No `vn` for this corner: NaN reaches the builder, which reads that as
            // "shade this face flat" rather than as a normal pointing nowhere.
            normals.push(NaN, NaN, NaN)
          } else {
            sawNormals = true
            normals.push(
              vertexNormals[corner.n * 3],
              vertexNormals[corner.n * 3 + 1],
              vertexNormals[corner.n * 3 + 2],
            )
          }
        }
      }
    }
  }

  if (positions.length < 9) throw new Error(say('obj.noFaces'))
  return {
    format: 'obj',
    parts: [
      {
        name: 'mesh',
        positions: new Float32Array(positions),
        normals: sawNormals ? new Float32Array(normals) : null,
        indices: null,
        color: null,
        uvs: null,
        texture: null,
      },
    ],
    meshes: 1,
    primitives: 1,
    textures: [],
    warnings: [],
  }
}

/** `v`, `v/vt`, `v//vn` and `v/vt/vn`, 1-based, negative counting back from here. */
function objCorner(token, vertexCount, normalCount) {
  const fields = token.split('/')
  const v = objIndex(fields[0], vertexCount)
  if (v < 0) throw new Error(say('obj.missingVertex', { vertex: fields[0] }))
  const n = fields.length >= 3 && fields[2] ? objIndex(fields[2], normalCount) : -1
  return { v, n }
}

function objIndex(field, total) {
  const raw = Number.parseInt(field, 10)
  if (!Number.isFinite(raw) || raw === 0) return -1
  const index = raw > 0 ? raw - 1 : total + raw
  return index >= 0 && index < total ? index : -1
}

/* ------------------------------------------------------------------ glTF 2.0 */

const GLB_MAGIC = 0x46546c67 // 'glTF', little-endian
const GLB_JSON_CHUNK = 0x4e4f534a
const GLB_BIN_CHUNK = 0x004e4942

/** TRIANGLES, TRIANGLE_STRIP, TRIANGLE_FAN. Points and lines carry no surface to draw. */
const TRIANGLE_MODES = new Set([4, 5, 6])

const COMPONENT_READERS = {
  5120: [1, (view, at) => view.getInt8(at)],
  5121: [1, (view, at) => view.getUint8(at)],
  5122: [2, (view, at) => view.getInt16(at, true)],
  5123: [2, (view, at) => view.getUint16(at, true)],
  5125: [4, (view, at) => view.getUint32(at, true)],
  5126: [4, (view, at) => view.getFloat32(at, true)],
}
const ELEMENT_COMPONENTS = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 }
/** What one unit of a normalized integer attribute is worth, per component type. */
const NORMALIZED_SCALE = { 5120: 1 / 127, 5121: 1 / 255, 5122: 1 / 32767, 5123: 1 / 65535 }

/**
 * Read a GLB, or a self-contained .gltf.
 *
 * Both framings reach the same document; what separates them is where the bytes are. A
 * GLB carries them in its BIN chunk, a .gltf in a `data:` URI — and a .gltf that names
 * a sibling `.bin` is refused outright, because this module is handed one file's bytes
 * and has no way to ask for a second. Half-rendering that file would be worse: the
 * missing buffer is usually all of the geometry, so the pane would show an empty
 * viewport and claim success.
 */
export function parseGlb(buffer) {
  const bytes = new Uint8Array(buffer)
  const { document, binary } = bytes.length >= 4 && new DataView(buffer).getUint32(0, true) === GLB_MAGIC
    ? splitGlb(buffer, bytes)
    : { document: parseGltfJson(bytes), binary: null }

  const buffers = bufferResolver(document, binary)
  const meshes = document.meshes ?? []
  const nodes = document.nodes ?? []
  const warnings = []
  const textures = textureResolver(document, buffers, warnings)
  const parts = []
  let placements = 0
  let draco = 0

  const emit = (meshIndex, matrix) => {
    const mesh = meshes[meshIndex]
    if (!mesh) return
    let emitted = 0
    for (const primitive of mesh.primitives ?? []) {
      if (primitive.extensions?.KHR_draco_mesh_compression) {
        // Decoding Draco means shipping the decoder, which is exactly the 3D dependency
        // §6.2 keeps out of the Studio. Name it instead of drawing nothing and saying so.
        draco += 1
        continue
      }
      const part = primitivePart(document, buffers, textures, primitive, matrix, mesh.name)
      if (part) {
        parts.push(part)
        emitted += 1
      }
    }
    if (emitted) placements += 1
  }

  for (const [index, matrix] of nodeTransforms(document, nodes)) {
    const meshIndex = nodes[index].mesh
    if (meshIndex !== undefined) emit(meshIndex, matrix)
  }
  if (!parts.length && meshes.length) {
    // No scene graph reaching them: some exporters write meshes no node references.
    for (let index = 0; index < meshes.length; index += 1) emit(index, identity())
  }

  if (draco) {
    warnings.push(say('gltf.dracoSkipped', { count: draco }))
  }
  if (!parts.length) {
    throw new Error(draco ? say('gltf.allDraco') : say('gltf.noTriangles'))
  }
  return {
    format: 'glb',
    parts,
    meshes: placements,
    primitives: parts.length,
    // Only the textures some surviving primitive actually reached: a material the file
    // declares and no primitive uses costs nothing to skip and a megabyte to decode.
    textures: textures.used(parts),
    warnings,
  }
}

function splitGlb(buffer, bytes) {
  const view = new DataView(buffer)
  if (bytes.length < 20) throw new Error(say('gltf.tooSmall'))
  const version = view.getUint32(4, true)
  if (version !== 2) throw new Error(say('gltf.glbVersion', { version }))

  let document = null
  let binary = null
  let offset = 12
  while (offset + 8 <= bytes.length) {
    const length = view.getUint32(offset, true)
    const kind = view.getUint32(offset + 4, true)
    const start = offset + 8
    if (start + length > bytes.length) throw new Error(say('gltf.truncatedChunk'))
    if (kind === GLB_JSON_CHUNK && document === null) {
      document = parseGltfJson(bytes.subarray(start, start + length))
    } else if (kind === GLB_BIN_CHUNK && binary === null) {
      binary = bytes.subarray(start, start + length)
    }
    // Chunks are padded to a 4-byte boundary; the length excludes the padding.
    offset = start + length + ((-length % 4) + 4) % 4
  }
  if (document === null) throw new Error(say('gltf.noJsonChunk'))
  return { document, binary }
}

function parseGltfJson(bytes) {
  let document
  try {
    document = JSON.parse(TEXT.decode(bytes))
  } catch (cause) {
    throw new Error(say('gltf.invalidJson', { reason: cause.message }))
  }
  const version = document?.asset?.version
  if (version && !String(version).startsWith('2')) {
    throw new Error(say('gltf.fileVersion', { version }))
  }
  return document
}

/**
 * A lookup for the bytes behind a declared buffer, resolved on demand and memoised.
 *
 * A buffer with no `uri` is the GLB's own BIN chunk — which is why a .gltf that has one
 * is malformed rather than merely unsupported, and says so differently from the file
 * that simply points at a `.bin` next to itself.
 *
 * On demand rather than all at once, because "this needs a second file" is only fatal
 * for a buffer something actually reads. A GLB keeping its geometry in the BIN chunk and
 * its animation samplers in a sibling `.bin` is legal, and refusing it outright would
 * hide a mesh this viewer draws perfectly well.
 */
function bufferResolver(document, binary) {
  const declared = document.buffers ?? []
  const resolved = new Map()
  return (index) => {
    if (resolved.has(index)) return resolved.get(index)
    const buffer = declared[index]
    if (!buffer) throw new Error(say('gltf.missingBuffer'))
    if (buffer.uri && !/^data:/i.test(buffer.uri)) throw new Error(externalBuffer(buffer.uri, binary))
    if (!buffer.uri && !(binary && index === 0)) {
      throw new Error(say('gltf.emptyBuffer', { index }))
    }
    const bytes = buffer.uri ? decodeDataUri(buffer.uri) : binary
    resolved.set(index, bytes)
    return bytes
  }
}

/**
 * Name the sibling file, because someone looking at a preview pane can act on a name and
 * cannot act on a buffer index. The decode is guarded: a URI may carry a bare `%` that is
 * no escape at all, and the URIError would replace this whole sentence with
 * "URI malformed".
 */
function externalBuffer(uri, binary) {
  const tail = uri.split(/[/\\]/).pop() || uri
  let name = tail
  try {
    name = decodeURIComponent(tail)
  } catch {
    name = tail
  }
  return binary ? say('gltf.glbExternal', { name }) : say('gltf.gltfExternal', { name })
}

function decodeDataUri(uri) {
  const comma = uri.indexOf(',')
  if (comma < 0) throw new Error(say('gltf.badDataUri'))
  const payload = uri.slice(comma + 1)
  if (!/;base64$/i.test(uri.slice(0, comma))) {
    // Percent-decoded byte by byte, not through `decodeURIComponent`: that reads the
    // result as UTF-8 and would mangle every buffer byte above 0x7f.
    const out = []
    for (let i = 0; i < payload.length; i += 1) {
      if (payload[i] === '%') {
        out.push(Number.parseInt(payload.slice(i + 1, i + 3), 16))
        i += 2
      } else {
        out.push(payload.charCodeAt(i) & 0xff)
      }
    }
    return new Uint8Array(out)
  }
  const binary = atob(payload)
  const out = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i)
  return out
}

/** Walk the scene graph, yielding `[node index, world matrix]` for every node. */
function* nodeTransforms(document, nodes) {
  const scenes = document.scenes ?? []
  const scene = scenes[document.scene ?? 0]
  let roots = scene?.nodes ?? []
  if (!roots.length) roots = nodes.map((_, index) => index)

  const stack = [...roots].reverse().map((index) => [index, identity()])
  const seen = new Set()
  while (stack.length) {
    const [index, parent] = stack.pop()
    // A malformed file can name a node twice, or point a child back at an ancestor;
    // visiting each once keeps that from wedging the browser tab.
    if (seen.has(index) || !nodes[index]) continue
    seen.add(index)
    const world = multiply4(parent, localMatrix(nodes[index]))
    yield [index, world]
    for (const child of [...(nodes[index].children ?? [])].reverse()) stack.push([child, world])
  }
}

/** `matrix` when the node carries one, otherwise T·R·S — glTF's own composition order. */
function localMatrix(node) {
  if (Array.isArray(node.matrix) && node.matrix.length === 16) return Float64Array.from(node.matrix)

  const [tx, ty, tz] = node.translation ?? [0, 0, 0]
  const [qx, qy, qz, qw] = node.rotation ?? [0, 0, 0, 1]
  const [sx, sy, sz] = node.scale ?? [1, 1, 1]
  const m = new Float64Array(16)
  m[0] = (1 - 2 * (qy * qy + qz * qz)) * sx
  m[1] = 2 * (qx * qy + qz * qw) * sx
  m[2] = 2 * (qx * qz - qy * qw) * sx
  m[4] = 2 * (qx * qy - qz * qw) * sy
  m[5] = (1 - 2 * (qx * qx + qz * qz)) * sy
  m[6] = 2 * (qy * qz + qx * qw) * sy
  m[8] = 2 * (qx * qz + qy * qw) * sz
  m[9] = 2 * (qy * qz - qx * qw) * sz
  m[10] = (1 - 2 * (qx * qx + qy * qy)) * sz
  m[12] = tx
  m[13] = ty
  m[14] = tz
  m[15] = 1
  return m
}

function primitivePart(document, buffers, textures, primitive, matrix, meshName) {
  const mode = primitive.mode ?? 4
  if (!TRIANGLE_MODES.has(mode)) return null

  const attributes = primitive.attributes ?? {}
  if (attributes.POSITION === undefined) return null
  const source = readAccessor(document, buffers, attributes.POSITION, 'POSITION')
  const count = Math.floor(source.length / 3)
  if (!count) return null

  const positions = new Float32Array(count * 3)
  for (let i = 0; i < count; i += 1) {
    const x = source[i * 3]
    const y = source[i * 3 + 1]
    const z = source[i * 3 + 2]
    positions[i * 3] = matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12]
    positions[i * 3 + 1] = matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13]
    positions[i * 3 + 2] = matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14]
  }

  let normals = null
  if (attributes.NORMAL !== undefined) {
    const supplied = readAccessor(document, buffers, attributes.NORMAL, 'NORMAL')
    normals = new Float32Array(count * 3)
    for (let i = 0; i < Math.min(count, Math.floor(supplied.length / 3)); i += 1) {
      const x = supplied[i * 3]
      const y = supplied[i * 3 + 1]
      const z = supplied[i * 3 + 2]
      // The upper 3×3, not its inverse transpose: under a non-uniform scale that tilts
      // the normal slightly, and carrying the inverse would cost more than the shading
      // error is worth here. A scale that degenerates the normal falls through to the
      // builder's geometric one anyway.
      normals[i * 3] = matrix[0] * x + matrix[4] * y + matrix[8] * z
      normals[i * 3 + 1] = matrix[1] * x + matrix[5] * y + matrix[9] * z
      normals[i * 3 + 2] = matrix[2] * x + matrix[6] * y + matrix[10] * z
    }
  }

  const order = primitive.indices === undefined
    ? null
    : readAccessor(document, buffers, primitive.indices, 'indices')
  const indices = triangleList(order, count, mode)
  if (!indices.length) return null

  // The transform never touches UVs — they index the image, not the world — so they are
  // the one attribute copied straight through.
  const texture = textures.resolve(primitive.material)
  let uvs = null
  if (texture) {
    const attribute = (attributes ?? {})[`TEXCOORD_${texture.texCoord}`]
    if (attribute === undefined) {
      textures.warnOnce(
        say('texture.noTexCoord', { name: texture.name, index: texture.texCoord }),
      )
    } else {
      const supplied = readAccessor(document, buffers, attribute, 'TEXCOORD')
      uvs = new Float32Array(count * 2)
      uvs.set(supplied.subarray(0, Math.min(uvs.length, supplied.length)))
    }
  }

  return {
    name: meshName ?? 'primitive',
    positions,
    normals,
    indices,
    color: baseColor(document, primitive.material),
    uvs,
    // A texture with nowhere to land is dropped here rather than in the builder, so a
    // primitive without UVs still draws in its material's flat colour.
    texture: uvs ? texture.id : null,
  }
}

/** Every mode collapses to a plain triangle list here, so the builder sees only one. */
function triangleList(order, vertexCount, mode) {
  const at = (i) => (order ? order[i] : i)
  const total = order ? order.length : vertexCount
  const out = []
  if (mode === 4) {
    for (let i = 0; i + 2 < total; i += 3) out.push(at(i), at(i + 1), at(i + 2))
  } else if (mode === 5) {
    // Strip: each new vertex closes a triangle, and the winding alternates.
    for (let i = 0; i + 2 < total; i += 1) {
      out.push(i % 2 ? at(i + 1) : at(i), i % 2 ? at(i) : at(i + 1), at(i + 2))
    }
  } else {
    for (let i = 1; i + 1 < total; i += 1) out.push(at(0), at(i), at(i + 1))
  }
  // An index past the end of POSITION would read undefined and shade the whole mesh
  // black; drop those triangles rather than the file.
  const clean = new Uint32Array(out.length)
  let written = 0
  for (let i = 0; i + 2 < out.length; i += 3) {
    if (out[i] < vertexCount && out[i + 1] < vertexCount && out[i + 2] < vertexCount) {
      clean[written] = out[i]
      clean[written + 1] = out[i + 1]
      clean[written + 2] = out[i + 2]
      written += 3
    }
  }
  return clean.subarray(0, written)
}

/**
 * A primitive's flat colour, sRGB-encoded.
 *
 * `baseColorFactor` is the one material channel worth reading: Hunyuan3D's untextured
 * exports carry one, and it is the difference between a readable preview and a grey
 * blob. glTF stores it linear while the canvas is sRGB, so it is encoded here — skip
 * that and every mid-tone comes out visibly darker than the file meant.
 */
function baseColor(document, materialIndex) {
  const material = (document.materials ?? [])[materialIndex]
  const factor = material?.pbrMetallicRoughness?.baseColorFactor
  if (!Array.isArray(factor) || factor.length < 3) return null
  return factor.slice(0, 3).map((value) => {
    const linear = Math.min(Math.max(Number(value) || 0, 0), 1)
    return linear <= 0.0031308 ? linear * 12.92 : 1.055 * linear ** (1 / 2.4) - 0.055
  })
}

/* ---------------------------------------------------- glTF base colour textures */

/** glTF's sampler wrap enums. Anything else is REPEAT, which is the format's default. */
const WRAP_MODES = new Set([33071, 33648, 10497])
const REPEAT = 10497

/** Enough of each container to name it when the file did not. */
const IMAGE_SIGNATURES = [
  [[0x89, 0x50, 0x4e, 0x47], 'image/png'],
  [[0xff, 0xd8, 0xff], 'image/jpeg'],
  [[0x47, 0x49, 0x46, 0x38], 'image/gif'],
  [[0x42, 0x4d], 'image/bmp'],
]

/**
 * The base colour texture behind each material, read once and shared.
 *
 * Only `baseColorTexture`, and deliberately: it is the channel that carries what the
 * surface looks like, and it is the one a textured Hunyuan3D export writes. Normal,
 * metallic/roughness, occlusion and emissive maps are still ignored — evaluating them
 * would mean a PBR shader, and this pane draws flat-lit triangles.
 *
 * Resolution is per material and memoised, failures included: a file whose texture
 * lives in a sibling `.png` should say so once, not once per primitive.
 */
function textureResolver(document, buffers, warnings) {
  const byTexture = new Map()
  const list = []
  const said = new Set()

  const warnOnce = (message) => {
    if (said.has(message)) return
    said.add(message)
    warnings.push(message)
  }

  return {
    warnOnce,
    resolve(materialIndex) {
      const material = (document.materials ?? [])[materialIndex]
      const info = material?.pbrMetallicRoughness?.baseColorTexture
      if (!info || info.index === undefined) return null
      if (byTexture.has(info.index)) return byTexture.get(info.index)

      let entry = null
      try {
        entry = readTexture(document, buffers, info, material)
        entry.id = list.length
        list.push(entry)
      } catch (cause) {
        warnOnce(cause.message)
        entry = null
      }
      byTexture.set(info.index, entry)
      return entry
    },
    /** The entries some surviving part kept, renumbered so `part.texture` still finds
     * them. A material the file declares and no primitive reaches costs nothing to skip
     * here and a megabyte to decode later. */
    used(parts) {
      const wanted = new Set(parts.map((part) => part.texture).filter((id) => id !== null))
      const kept = []
      const renumbered = new Map()
      for (const entry of list) {
        if (!wanted.has(entry.id)) continue
        renumbered.set(entry.id, kept.length)
        kept.push({ ...entry, id: kept.length })
      }
      for (const part of parts) {
        if (part.texture !== null) part.texture = renumbered.get(part.texture) ?? null
      }
      return kept
    },
  }
}

/** One `baseColorTexture` as bytes the browser can decode, plus how to sample it. */
function readTexture(document, buffers, info, material) {
  const named = material?.name ? say('texture.namedMaterial', { name: material.name }) : say('texture.material')
  const texture = (document.textures ?? [])[info.index]
  if (!texture) throw new Error(say('texture.missingTexture', { material: named, index: info.index }))
  if (info.extensions?.KHR_texture_transform) {
    // Applying it means composing a UV matrix per texture; nothing that reaches this pane
    // writes one, and a silently untransformed texture reads as a broken unwrap.
    throw new Error(say('texture.transform', { material: named }))
  }

  const source = texture.source ?? texture.extensions?.EXT_texture_webp?.source
  if (source === undefined) {
    throw new Error(
      texture.extensions?.KHR_texture_basisu
        ? say('texture.basis', { material: named })
        : say('texture.noImage', { material: named }),
    )
  }
  const image = (document.images ?? [])[source]
  if (!image) throw new Error(say('texture.missingImage', { material: named, index: source }))

  let bytes
  let mime = image.mimeType ?? ''
  if (image.uri) {
    if (!/^data:/i.test(image.uri)) {
      throw new Error(say('texture.external', { material: named, name: fileName(image.uri) }))
    }
    bytes = decodeDataUri(image.uri)
  } else if (image.bufferView !== undefined) {
    const view = (document.bufferViews ?? [])[image.bufferView]
    if (!view) throw new Error(say('texture.missingBufferView', { material: named, index: image.bufferView }))
    const buffer = buffers(view.buffer ?? 0)
    const start = view.byteOffset ?? 0
    const length = view.byteLength ?? 0
    if (start + length > buffer.length) throw new Error(say('texture.pastBuffer', { material: named }))
    bytes = buffer.subarray(start, start + length)
  } else {
    throw new Error(say('texture.noSource', { material: named }))
  }
  if (!bytes.length) throw new Error(say('texture.empty', { material: named }))

  const sampler = (document.samplers ?? [])[texture.sampler] ?? {}
  return {
    id: -1,
    name: image.name ?? texture.name ?? material?.name ?? say('texture.baseColour'),
    bytes,
    // The bytes win over the declaration, which is not pedantry: Hunyuan3D writes
    // `data:image/png;base64,` in front of JPEG bytes on every textured export. Browsers
    // sniff a Blob's contents anyway, so this is about the label the warning quotes and
    // about not handing a lie to whatever reads this next.
    mime: sniffImageMime(bytes) || mime,
    wrapS: WRAP_MODES.has(sampler.wrapS) ? sampler.wrapS : REPEAT,
    wrapT: WRAP_MODES.has(sampler.wrapT) ? sampler.wrapT : REPEAT,
    texCoord: info.texCoord ?? 0,
    image: null,
  }
}

/** The container from its own first bytes — a GLB may leave `mimeType` off entirely. */
export function sniffImageMime(bytes) {
  for (const [signature, mime] of IMAGE_SIGNATURES) {
    if (signature.every((byte, i) => bytes[i] === byte)) return mime
  }
  // RIFF….WEBP: the two pairs that separate it from any other RIFF container.
  if (bytes[0] === 0x52 && bytes[1] === 0x49 && bytes[8] === 0x57 && bytes[9] === 0x45) return 'image/webp'
  return 'application/octet-stream'
}

function fileName(uri) {
  const tail = uri.split(/[/\\]/).pop() || uri
  try {
    return decodeURIComponent(tail)
  } catch {
    return tail
  }
}

/**
 * Decode every texture a built mesh carries, in place, before it reaches the viewer.
 *
 * Separate from parsing because decoding is the browser's job and parsing is not: the
 * readers stay pure enough to run head-lessly in Node, and this is the one function that
 * needs a `Blob`. A texture that will not decode is downgraded to its flat tint and said
 * out loud rather than drawn black.
 */
export async function decodeTextures(mesh) {
  await Promise.all(
    (mesh.textures ?? []).map(async (entry) => {
      try {
        entry.image = await decodeImage(entry.bytes, entry.mime)
      } catch (cause) {
        entry.image = null
        mesh.stats.warnings.push(say('texture.undecodable', { name: entry.name, reason: cause.message }))
      }
    }),
  )
  mesh.stats.textures = (mesh.textures ?? []).filter((entry) => entry.image).length
  return mesh
}

function decodeImage(bytes, mime) {
  // The bytes are a view into the GLB's own buffer, so they are copied: a `Blob` keeps a
  // reference, and a subarray of a multi-megabyte GLB would pin the whole file.
  const blob = new Blob([bytes.slice()], { type: mime || 'application/octet-stream' })
  if (typeof createImageBitmap === 'function') {
    // `colorSpaceConversion: 'none'` because a base colour texture is already sRGB and
    // the canvas is sRGB; letting the browser "correct" it washes every texture out.
    return createImageBitmap(blob, { colorSpaceConversion: 'none' })
  }
  const url = URL.createObjectURL(blob)
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => {
      URL.revokeObjectURL(url)
      resolve(image)
    }
    image.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error(say('texture.browserRefused')))
    }
    image.src = url
  })
}

/** One accessor, flattened. `byteStride` is honoured: interleaved buffers are normal. */
function readAccessor(document, buffers, index, what) {
  const accessor = (document.accessors ?? [])[index]
  if (!accessor) throw new Error(say('gltf.missingAccessor', { index }))
  if (accessor.sparse) {
    // Legal, and means "zeros plus these entries"; nothing that reaches this pane emits
    // one, and guessing is worse than saying so.
    throw new Error(say('gltf.sparse'))
  }

  const reader = COMPONENT_READERS[accessor.componentType]
  const perElement = ELEMENT_COMPONENTS[accessor.type]
  if (!reader || !perElement) {
    throw new Error(
      say('gltf.unsupportedAccessor', {
        type: accessor.type,
        componentType: accessor.componentType,
        attribute: what,
      }),
    )
  }
  if (what !== 'indices' && accessor.componentType !== 5126 && !accessor.normalized) {
    // Quantized positions (KHR_mesh_quantization) would need the material's transform
    // applied on top; nothing here produces them, so refuse rather than mis-scale.
    throw new Error(say('gltf.quantized', { attribute: what }))
  }
  // A *normalized* integer attribute is a different thing and perfectly ordinary — UVs
  // are routinely stored as unsigned shorts — so it is scaled back into 0…1 rather than
  // refused. The signed clamp is glTF's own: -128/127 and -32768/32767 both mean -1.
  const scale = accessor.normalized ? NORMALIZED_SCALE[accessor.componentType] ?? 0 : 0
  const [size, read] = reader
  const count = accessor.count ?? 0
  if (count <= 0) return new Float32Array(0)

  if (accessor.bufferView === undefined) throw new Error(say('gltf.noBufferView'))
  const view = (document.bufferViews ?? [])[accessor.bufferView]
  if (!view) throw new Error(say('gltf.missingBufferView', { index: accessor.bufferView }))
  const bytes = buffers(view.buffer ?? 0)

  const start = (view.byteOffset ?? 0) + (accessor.byteOffset ?? 0)
  const stride = view.byteStride || size * perElement
  if (start < 0 || start + (count - 1) * stride + size * perElement > bytes.length) {
    throw new Error(say('gltf.pastBuffer'))
  }

  const data = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const out = what === 'indices' ? new Uint32Array(count * perElement) : new Float32Array(count * perElement)
  for (let i = 0; i < count; i += 1) {
    for (let component = 0; component < perElement; component += 1) {
      const value = read(data, start + i * stride + component * size)
      out[i * perElement + component] = scale ? Math.max(value * scale, -1) : value
    }
  }
  return out
}

/* ------------------------------------------------------------------ transforms */

function identity() {
  const m = new Float64Array(16)
  m[0] = m[5] = m[10] = m[15] = 1
  return m
}

function multiply4(a, b) {
  const out = new Float64Array(16)
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

/* ------------------------------------------------------------------ mesh building */

function partTriangles(part) {
  return part.indices ? Math.floor(part.indices.length / 3) : Math.floor(part.positions.length / 9)
}

/** The texture a part is actually drawable with — one without UVs cannot be sampled. */
function textureOf(part) {
  return part.uvs && part.texture !== null && part.texture !== undefined ? part.texture : null
}

/** Parts in file order, gathered under the texture each is drawn with. */
function groupByTexture(parts) {
  const groups = new Map()
  for (const part of parts) {
    const key = textureOf(part)
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(part)
  }
  return groups
}

/**
 * The flat colour a part's corners carry, as bytes.
 *
 * White under a texture rather than the default grey: glTF multiplies `baseColorFactor`
 * into the sampled texel and defaults that factor to white, so a part that named no
 * factor must not tint its texture at all. Grey there would darken every textured mesh
 * by a quarter and look like bad lighting.
 */
function tintOf(part) {
  const base = part.color ?? (textureOf(part) !== null ? WHITE : DEFAULT_COLOR)
  return base.map((c) => Math.round(Math.min(Math.max(c, 0), 1) * 255))
}

/**
 * Turn what a reader answered with into one triangle soup ready for the viewer.
 *
 * A corner keeps the file's own normal when it has a usable one — a Hunyuan3D GLB does,
 * and smooth shading is what makes its surface readable — and takes the triangle's
 * geometric normal otherwise, which is both correct and free. Degenerate triangles are
 * dropped rather than shaded black, exactly as fbx_preview drops them.
 */
export function buildMesh(parsed) {
  let total = 0
  for (const part of parsed.parts) total += partTriangles(part)
  if (!total) throw new Error(say('build.noTriangles'))

  const truncated = total > MAX_TRIANGLES
  const triangles = Math.min(total, MAX_TRIANGLES)
  const positions = new Float32Array(triangles * 9)
  const normals = new Float32Array(triangles * 9)
  // A textured part is drawn as tint × texel, and its tint is white when the material
  // named no factor — so one textured part is enough to need the colour buffer, even if
  // nothing else in the file names a colour at all.
  const textured = parsed.parts.some((part) => textureOf(part) !== null)
  const colors =
    textured || parsed.parts.some((part) => part.color) ? new Uint8Array(triangles * 9) : null
  const uvs = textured ? new Float32Array(triangles * 6) : null
  const bounds = { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] }
  // One scratch triangle for the whole build: at half a million faces, allocating this
  // per call is the difference between a preview and a garbage-collection pause.
  const corners = new Float64Array(9)
  let written = 0

  // One draw call per texture, so the parts sharing one are written as a contiguous run.
  // Order inside a group and between groups is the file's own — a builder that sorted
  // the mesh would change which of two coplanar faces wins the depth test.
  const groups = []
  for (const [texture, members] of groupByTexture(parsed.parts)) {
    const start = written
    for (const part of members) {
      const count = partTriangles(part)
      const rgb = colors ? tintOf(part) : null
      for (let t = 0; t < count; t += 1) {
        if (written >= triangles) break
        const a = part.indices ? part.indices[t * 3] : t * 3
        const b = part.indices ? part.indices[t * 3 + 1] : t * 3 + 1
        const c = part.indices ? part.indices[t * 3 + 2] : t * 3 + 2
        if (addTriangle(part, a, b, c, rgb, written)) written += 1
      }
      if (written >= triangles) break
    }
    // Empty when every triangle in the group was degenerate, or when the cap ran out
    // first; a zero-length draw range would bind a texture to draw nothing.
    if (written > start) groups.push({ texture, start: start * 3, count: (written - start) * 3 })
    if (written >= triangles) break
  }

  if (!written) throw new Error(say('build.noUsableTriangles'))

  return {
    positions: positions.subarray(0, written * 9),
    normals: normals.subarray(0, written * 9),
    colors: colors ? colors.subarray(0, written * 9) : null,
    uvs: uvs ? uvs.subarray(0, written * 6) : null,
    groups,
    textures: parsed.textures ?? [],
    color: DEFAULT_COLOR,
    bounds,
    stats: {
      format: parsed.format,
      triangles: written,
      declared: total,
      truncated,
      meshes: parsed.meshes,
      primitives: parsed.primitives,
      // What `decodeTextures` will be asked for; it lowers this to what actually decoded.
      textures: new Set(groups.map((group) => group.texture).filter((id) => id !== null)).size,
      // Copied, not aliased: decoding runs after this and appends to it.
      warnings: [...(parsed.warnings ?? [])],
    },
  }

  function addTriangle(part, a, b, c, rgb, slot) {
    for (let corner = 0; corner < 3; corner += 1) {
      const from = [a, b, c][corner] * 3
      corners[corner * 3] = part.positions[from]
      corners[corner * 3 + 1] = part.positions[from + 1]
      corners[corner * 3 + 2] = part.positions[from + 2]
    }
    for (let i = 0; i < 9; i += 1) if (!Number.isFinite(corners[i])) return false

    let nx = (corners[4] - corners[1]) * (corners[8] - corners[2]) - (corners[5] - corners[2]) * (corners[7] - corners[1])
    let ny = (corners[5] - corners[2]) * (corners[6] - corners[0]) - (corners[3] - corners[0]) * (corners[8] - corners[2])
    let nz = (corners[3] - corners[0]) * (corners[7] - corners[1]) - (corners[4] - corners[1]) * (corners[6] - corners[0])
    const length = Math.hypot(nx, ny, nz)
    if (length === 0) {
      // Degenerate triangle: keep it out of the buffer rather than shading it black.
      return false
    }
    nx /= length
    ny /= length
    nz /= length

    const base = slot * 9
    for (let i = 0; i < 9; i += 1) positions[base + i] = corners[i]
    for (let corner = 0; corner < 3; corner += 1) {
      const supplied = part.normals ? [a, b, c][corner] * 3 : -1
      const sx = supplied < 0 ? NaN : part.normals[supplied]
      const sy = supplied < 0 ? NaN : part.normals[supplied + 1]
      const sz = supplied < 0 ? NaN : part.normals[supplied + 2]
      const usable = Number.isFinite(sx) && Number.isFinite(sy) && Number.isFinite(sz) && Math.hypot(sx, sy, sz) > 0
      normals[base + corner * 3] = usable ? sx : nx
      normals[base + corner * 3 + 1] = usable ? sy : ny
      normals[base + corner * 3 + 2] = usable ? sz : nz
      if (rgb) {
        colors[base + corner * 3] = rgb[0]
        colors[base + corner * 3 + 1] = rgb[1]
        colors[base + corner * 3 + 2] = rgb[2]
      }
      if (uvs && part.uvs) {
        const at = [a, b, c][corner] * 2
        uvs[slot * 6 + corner * 2] = part.uvs[at]
        uvs[slot * 6 + corner * 2 + 1] = part.uvs[at + 1]
      }
    }
    for (let i = 0; i < 9; i += 3) {
      for (let axis = 0; axis < 3; axis += 1) {
        const value = corners[i + axis]
        if (value < bounds.min[axis]) bounds.min[axis] = value
        if (value > bounds.max[axis]) bounds.max[axis] = value
      }
    }
    return true
  }
}

/* ------------------------------------------------------------------ WebGL viewer */

const VERTEX_SHADER = `
attribute vec3 aPosition;
attribute vec3 aNormal;
attribute vec3 aColor;
attribute vec2 aUv;
uniform mat4 uViewProjection;
varying vec3 vNormal;
varying vec3 vColor;
varying vec2 vUv;
void main() {
  vNormal = aNormal;
  vColor = aColor;
  vUv = aUv;
  gl_Position = uViewProjection * vec4(aPosition, 1.0);
}`

const FRAGMENT_SHADER = `
precision mediump float;
varying vec3 vNormal;
varying vec3 vColor;
varying vec2 vUv;
uniform sampler2D uTexture;
uniform float uTextured;
void main() {
  // Two-sided: imported meshes are not reliably wound, and a black backface reads as
  // a hole. Lighting is fixed in world space so rotating the model shades it.
  vec3 n = normalize(vNormal);
  if (!gl_FrontFacing) n = -n;
  float key = max(dot(n, normalize(vec3(0.45, 0.8, 0.35))), 0.0);
  float fill = max(dot(n, normalize(vec3(-0.6, 0.15, -0.5))), 0.0);
  float ambient = 0.34 + 0.14 * n.y;
  // glTF's base colour is factor × texel, and both are sRGB by the time they arrive —
  // the tint is encoded when it is read and a base colour texture is stored that way.
  // Multiplying here rather than in linear light is the same shortcut the flat path
  // already takes, and keeps a texture looking like its own file.
  vec3 base = vColor * mix(vec3(1.0), texture2D(uTexture, vUv).rgb, uTextured);
  gl_FragColor = vec4(base * (ambient + 0.66 * key + 0.20 * fill), 1.0);
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
 * show a preview, so flat-shaded triangles with a fixed light is the whole feature set.
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
    uv: gl.getAttribLocation(program, 'aUv'),
  }
  const uViewProjection = gl.getUniformLocation(program, 'uViewProjection')
  const uTexture = gl.getUniformLocation(program, 'uTexture')
  const uTextured = gl.getUniformLocation(program, 'uTextured')
  const buffers = {
    position: gl.createBuffer(),
    normal: gl.createBuffer(),
    color: gl.createBuffer(),
    uv: gl.createBuffer(),
  }

  const projection = new Float32Array(16)
  const view = new Float32Array(16)
  const viewProjection = new Float32Array(16)
  const camera = { yaw: 0.85, pitch: 0.5, distance: 4, target: [0, 0, 0], radius: 1 }
  let vertexCount = 0
  let hasColors = false
  let hasUvs = false
  let flatColor = DEFAULT_COLOR
  let groups = []
  let textures = []
  let pending = 0
  let disposed = false

  gl.enable(gl.DEPTH_TEST)
  gl.disable(gl.CULL_FACE)

  // Unit 0 always has something bound to it: a sampler pointing at nothing is undefined
  // behaviour even inside a branch the shader never takes, and drivers disagree about
  // what it draws. One opaque white texel is the identity for the multiply.
  const blank = gl.createTexture()
  gl.bindTexture(gl.TEXTURE_2D, blank)
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([255, 255, 255, 255]))
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)

  /**
   * Upload one decoded texture.
   *
   * `UNPACK_FLIP_Y_WEBGL` stays off, which is what glTF means: its UV origin is the
   * image's top-left, and an unflipped upload puts the first row of pixels at t=0.
   * Flipping here — the reflex from every other WebGL texture — turns every model
   * upside down in texture space, which on a baked Hunyuan3D mesh reads as a smeared
   * unwrap rather than as an obvious flip.
   *
   * WebGL 1 will not repeat or mipmap a non-power-of-two texture, and the sizes that
   * arrive here are exactly that: Hunyuan3D bakes at 768 as often as 512. So the wrap
   * the sampler asked for is honoured only when it can be, and clamped otherwise —
   * silently, because the alternative is a black model.
   */
  function uploadTexture(entry) {
    const texture = gl.createTexture()
    gl.bindTexture(gl.TEXTURE_2D, texture)
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false)
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, entry.image)

    const power = (value) => value > 0 && (value & (value - 1)) === 0
    const repeatable = power(entry.image.width) && power(entry.image.height)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, repeatable ? entry.wrapS : gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, repeatable ? entry.wrapT : gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    if (repeatable) {
      gl.generateMipmap(gl.TEXTURE_2D)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR)
    } else {
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    }
    return texture
  }

  function releaseTextures() {
    for (const texture of textures) if (texture) gl.deleteTexture(texture)
    textures = []
  }

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
    if (vertexCount === 0) return

    const eye = eyePosition()
    // Near/far track the model size, so a 1-unit prop and a 10000-unit level both keep
    // usable depth precision.
    const near = Math.max(camera.radius * 0.005, camera.distance * 0.01)
    perspective(projection, (50 * Math.PI) / 180, width / height, near, camera.distance + camera.radius * 8)
    lookAt(view, eye, camera.target, [0, 1, 0])
    multiply(viewProjection, projection, view)

    gl.useProgram(program)
    gl.uniformMatrix4fv(uViewProjection, false, viewProjection)

    bindFloat(buffers.position, attributes.position, 3)
    bindFloat(buffers.normal, attributes.normal, 3)
    if (hasColors) {
      gl.enableVertexAttribArray(attributes.color)
      gl.bindBuffer(gl.ARRAY_BUFFER, buffers.color)
      gl.vertexAttribPointer(attributes.color, 3, gl.UNSIGNED_BYTE, true, 0, 0)
    } else {
      gl.disableVertexAttribArray(attributes.color)
      gl.vertexAttrib3f(attributes.color, flatColor[0], flatColor[1], flatColor[2])
    }
    if (hasUvs) {
      bindFloat(buffers.uv, attributes.uv, 2)
    } else {
      gl.disableVertexAttribArray(attributes.uv)
      gl.vertexAttrib2f(attributes.uv, 0, 0)
    }

    gl.activeTexture(gl.TEXTURE0)
    gl.uniform1i(uTexture, 0)
    for (const group of groups) {
      const texture = group.texture === null ? null : textures[group.texture]
      gl.bindTexture(gl.TEXTURE_2D, texture ?? blank)
      gl.uniform1f(uTextured, texture ? 1 : 0)
      gl.drawArrays(gl.TRIANGLES, group.start, group.count)
    }
  }

  function bindFloat(buffer, location, size) {
    gl.enableVertexAttribArray(location)
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
    gl.vertexAttribPointer(location, size, gl.FLOAT, false, 0, 0)
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
    hasUvs = Boolean(mesh.uvs && mesh.uvs.length === (mesh.positions.length / 3) * 2)
    if (hasUvs) {
      gl.bindBuffer(gl.ARRAY_BUFFER, buffers.uv)
      gl.bufferData(gl.ARRAY_BUFFER, mesh.uvs, gl.STATIC_DRAW)
    }
    if (mesh.color) flatColor = mesh.color
    vertexCount = mesh.positions.length / 3

    // Only what decoded: `decodeTextures` leaves `image` null on the rest, and those
    // groups fall back to their flat tint instead of sampling nothing.
    releaseTextures()
    textures = (mesh.textures ?? []).map((entry) => (entry.image ? uploadTexture(entry) : null))
    // A reader that answered with no groups — every one but glTF — is one untextured run.
    groups = mesh.groups?.length ? mesh.groups : [{ texture: null, start: 0, count: vertexCount }]

    const { min, max } = mesh.bounds
    camera.radius = Math.max(Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) / 2, 0.001)
    camera.target = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2]
    resetView()
  }

  function resetView() {
    camera.yaw = 0.85
    camera.pitch = 0.5
    camera.distance = camera.radius / Math.sin((50 * Math.PI) / 360) + camera.radius * 0.2
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
  canvas.addEventListener('dblclick', resetView)
  canvas.style.cursor = 'grab'

  const observer = new ResizeObserver(requestRender)
  observer.observe(canvas)

  return {
    setMesh,
    resetView,
    requestRender,
    dispose() {
      disposed = true
      if (pending) cancelAnimationFrame(pending)
      releaseTextures()
      gl.deleteTexture(blank)
      observer.disconnect()
      canvas.removeEventListener('pointerdown', onPointerDown)
      canvas.removeEventListener('pointermove', onPointerMove)
      canvas.removeEventListener('pointerup', onPointerUp)
      canvas.removeEventListener('pointercancel', onPointerUp)
      canvas.removeEventListener('wheel', onWheel)
      canvas.removeEventListener('dblclick', resetView)
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

export default function ({ vue, rawFileBytes, i18n }) {
  const { defineComponent, h, onBeforeUnmount, onMounted, ref } = vue
  const t = i18n.translator(MESSAGES)
  // The readers and the viewer speak the interface language from here on, too.
  translate = t

  return defineComponent({
    name: 'MeshPreview',
    props: {
      path: { type: String, required: true },
      preview: { type: Object, default: null },
    },
    setup(props) {
      const canvas = ref(null)
      const error = ref(null)
      const stats = ref(null)
      const loading = ref(true)
      let viewer = null

      // Four suffixes reach one component, so the error line has to name the file's own
      // rather than a format the reader guessed at.
      const suffix = (props.path.match(/\.[^./\\]+$/)?.[0] ?? '').toLowerCase()

      onMounted(async () => {
        try {
          const mesh = buildMesh(parseMesh(props.path, await rawFileBytes(props.path)))
          // Before the viewer exists, so a context is never created for a mesh whose
          // textures are still decoding — and so one `setMesh` uploads everything.
          await decodeTextures(mesh)
          viewer = createViewer(canvas.value)
          viewer.setMesh(mesh)
          stats.value = mesh.stats
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

      const statsStrip = () =>
        h(
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
          [
            h('span', t('stats.tris', { count: stats.value.triangles })),
            // Only glTF has a scene graph to describe; an STL is one solid and an OBJ
            // read this way is one mesh, so the counts would say nothing there.
            stats.value.format === 'glb'
              ? h('span', t('stats.meshes', { count: stats.value.meshes }))
              : null,
            stats.value.format === 'glb' && stats.value.primitives !== stats.value.meshes
              ? h('span', t('stats.primitives', { count: stats.value.primitives }))
              : null,
            // Worth a chip of its own: "textured" is the difference between a model that
            // looks wrong and one whose material simply names a flat colour.
            stats.value.textures
              ? h(
                  'span',
                  stats.value.textures === 1
                    ? t('stats.textured')
                    : t('stats.textures', { count: stats.value.textures }),
                )
              : null,
            stats.value.truncated
              ? h(
                  'span',
                  { style: { color: 'var(--vxs-warning)' } },
                  t('stats.truncated', {
                    shown: stats.value.triangles.toLocaleString(i18n.locale.value),
                    count: stats.value.declared,
                  }),
                )
              : null,
            ...stats.value.warnings.map((warning) =>
              h('span', { style: { color: 'var(--vxs-warning)' } }, warning),
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
          error.value
            ? overlay(
                suffix
                  ? t('unreadable', { suffix, reason: error.value })
                  : t('unreadableMesh', { reason: error.value }),
                'var(--vxs-danger)',
              )
            : null,

          stats.value ? statsStrip() : null,

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
