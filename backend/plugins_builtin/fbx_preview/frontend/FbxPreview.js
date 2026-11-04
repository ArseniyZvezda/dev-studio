/**
 * Binary FBX (.fbx) previewer for the Explorer preview pane (spec §6.2).
 *
 * Loaded at runtime as a plain ES module from `/api/plugins/fbx_preview/assets/…`, so
 * it must have no imports at all: `vue` arrives through the factory (a second Vue
 * instance would share no reactivity), and the format reader and renderer live here.
 *
 * One file on purpose. A dynamic `import()` cannot send an auth header, so the host
 * appends `?token=`; a relative `import './viewer.js'` would resolve without it and
 * 401 in server mode. The viewer below is therefore a near-copy of the one in
 * vox_preview — plugins are installed independently and must not import each other.
 *
 * The parsing entry points are exported so they can be exercised head-lessly in Node,
 * where `DecompressionStream` behaves as it does in the browser.
 */

/* ------------------------------------------------------------------ messages */

/**
 * What the previewer says, in the Studio's interface languages (§6.4).
 *
 * The component words its own text while it renders. The reader and the viewer below run
 * outside it — and head-lessly in Node, with no host at all — so what they throw is worded
 * by `say()` when it is thrown: in the interface language once the factory has run, in
 * English before. A preview reads its file each time it is mounted, so that sentence is in
 * the language the file was opened in.
 */
const MESSAGES = {
  en: {
    reading: 'Reading mesh…',
    unreadable: 'Could not read this .fbx file — {reason}',
    tris: { one: '{count} tri', other: '{count} tris' },
    meshes: { one: '{count} mesh', other: '{count} meshes' },
    truncated: 'truncated',
    hint: 'drag to orbit · shift-drag to pan · scroll to zoom · double-click to reset',
    fbx: {
      ascii: 'This is an ASCII FBX; only binary FBX is supported — re-export as binary',
      notFbx: 'Not an FBX file',
      unknownProperty: 'Unknown FBX property type {code}',
      notArray: 'Not an FBX array property',
      noInflate: 'This browser cannot inflate compressed FBX arrays',
      noObjects: 'This FBX has no Objects section',
      noGeometry: 'This FBX contains no mesh geometry',
      noReadableGeometry: 'This FBX contains no readable mesh geometry',
      noTriangles: 'This FBX has geometry but no usable triangles',
    },
    webgl: {
      unavailable: 'WebGL is not available in this browser',
      compile: 'Shader failed to compile: {log}',
      link: 'Shader program failed to link: {log}',
    },
  },
  ru: {
    reading: 'Чтение меша…',
    unreadable: 'Не удалось прочитать файл .fbx — {reason}',
    tris: {
      one: '{count} треугольник',
      few: '{count} треугольника',
      many: '{count} треугольников',
      other: '{count} треугольника',
    },
    meshes: { one: '{count} меш', few: '{count} меша', many: '{count} мешей', other: '{count} меша' },
    truncated: 'обрезано',
    hint: 'вращение: ЛКМ · сдвиг: Shift+ЛКМ · масштаб: колесо · сброс: двойной клик',
    fbx: {
      ascii: 'Это ASCII FBX, а поддерживается только двоичный FBX — экспортируйте файл заново в двоичном формате',
      notFbx: 'Это не файл FBX',
      unknownProperty: 'Неизвестный тип свойства FBX: {code}',
      notArray: 'Это не свойство-массив FBX',
      noInflate: 'Этот браузер не умеет распаковывать сжатые массивы FBX',
      noObjects: 'В этом FBX нет раздела Objects',
      noGeometry: 'В этом FBX нет геометрии мешей',
      noReadableGeometry: 'В этом FBX нет читаемой геометрии мешей',
      noTriangles: 'В этом FBX есть геометрия, но нет пригодных треугольников',
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

/* ------------------------------------------------------------------ FBX container */

const TEXT = new TextDecoder()
const BINARY_MAGIC = 'Kaydara FBX Binary  '

/** Flat shading duplicates every vertex, so a preview has to draw a line somewhere. */
const MAX_TRIANGLES = 500_000

/**
 * Read the node tree.
 *
 * FBX is a tree of records — `EndOffset | NumProperties | PropertyListLen | name |
 * properties | children | null record` — where EndOffset is an absolute file offset
 * (64-bit from version 7500 on). Property arrays are left compressed and decoded on
 * demand: a rigged character is mostly weights and UVs this viewer never looks at.
 */
export function parseFbx(buffer) {
  const bytes = new Uint8Array(buffer)
  const view = new DataView(buffer)

  if (TEXT.decode(bytes.subarray(0, BINARY_MAGIC.length)) !== BINARY_MAGIC) {
    const head = TEXT.decode(bytes.subarray(0, 256))
    if (head.includes('FBX')) {
      throw new Error(say('fbx.ascii'))
    }
    throw new Error(say('fbx.notFbx'))
  }

  const version = view.getUint32(23, true)
  const wide = version >= 7500
  const headerSize = wide ? 25 : 13

  const nodes = []
  let cursor = 27
  while (cursor + headerSize <= bytes.length) {
    const [node, next] = readNode(cursor)
    if (!node) break
    nodes.push(node)
    cursor = next
  }
  return { version, nodes }

  function readNode(offset) {
    const endOffset = wide ? Number(view.getBigUint64(offset, true)) : view.getUint32(offset, true)
    if (endOffset === 0) return [null, offset + headerSize] // null record: end of a list

    const propertyCount = wide
      ? Number(view.getBigUint64(offset + 8, true))
      : view.getUint32(offset + 4, true)
    const propertyBytes = wide
      ? Number(view.getBigUint64(offset + 16, true))
      : view.getUint32(offset + 8, true)

    let at = offset + (wide ? 24 : 12)
    const nameLength = bytes[at]
    at += 1
    const name = TEXT.decode(bytes.subarray(at, at + nameLength))
    at += nameLength

    const properties = []
    const propertiesEnd = at + propertyBytes
    for (let i = 0; i < propertyCount && at < propertiesEnd; i += 1) {
      const [value, next] = readProperty(at)
      properties.push(value)
      at = next
    }

    const children = []
    let childCursor = propertiesEnd
    while (childCursor + headerSize <= endOffset) {
      const [child, next] = readNode(childCursor)
      if (!child) break
      children.push(child)
      childCursor = next
    }

    return [{ name, properties, children }, endOffset]
  }

  function readProperty(offset) {
    const code = String.fromCharCode(bytes[offset])
    const at = offset + 1
    switch (code) {
      case 'Y':
        return [view.getInt16(at, true), at + 2]
      case 'C':
        return [bytes[at] !== 0, at + 1]
      case 'I':
        return [view.getInt32(at, true), at + 4]
      case 'F':
        return [view.getFloat32(at, true), at + 4]
      case 'D':
        return [view.getFloat64(at, true), at + 8]
      case 'L':
        return [Number(view.getBigInt64(at, true)), at + 8]
      case 'S':
      case 'R': {
        const length = view.getUint32(at, true)
        const raw = bytes.subarray(at + 4, at + 4 + length)
        return [code === 'S' ? TEXT.decode(raw) : raw, at + 4 + length]
      }
      case 'f':
      case 'd':
      case 'l':
      case 'i':
      case 'b': {
        const length = view.getUint32(at, true)
        const encoding = view.getUint32(at + 4, true)
        const byteLength = view.getUint32(at + 8, true)
        return [
          { array: code, length, encoding, data: bytes.subarray(at + 12, at + 12 + byteLength) },
          at + 12 + byteLength,
        ]
      }
      default:
        throw new Error(say('fbx.unknownProperty', { code: JSON.stringify(code) }))
    }
  }
}

const ARRAY_READERS = {
  f: [4, Float32Array, (view, at) => view.getFloat32(at, true)],
  d: [8, Float64Array, (view, at) => view.getFloat64(at, true)],
  l: [8, Float64Array, (view, at) => Number(view.getBigInt64(at, true))],
  i: [4, Int32Array, (view, at) => view.getInt32(at, true)],
  b: [1, Uint8Array, (view, at) => view.getUint8(at)],
}

/** Decode one array property, inflating it first if the file stored it compressed. */
export async function readArrayProperty(property) {
  if (!property || !property.array) throw new Error(say('fbx.notArray'))
  const [stride, TypedArray, read] = ARRAY_READERS[property.array]

  let raw = property.data
  if (property.encoding === 1) {
    if (typeof DecompressionStream === 'undefined') {
      throw new Error(say('fbx.noInflate'))
    }
    // FBX uses zlib-wrapped deflate, which is exactly what 'deflate' means here.
    const stream = new Blob([property.data]).stream().pipeThrough(new DecompressionStream('deflate'))
    raw = new Uint8Array(await new Response(stream).arrayBuffer())
  }

  // Read through a DataView rather than aliasing the buffer: the slice is rarely
  // aligned to the element size, and a typed-array view would throw on that.
  const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength)
  const count = Math.min(property.length, Math.floor(raw.byteLength / stride))
  const out = new TypedArray(count)
  for (let i = 0; i < count; i += 1) out[i] = read(view, i * stride)
  return out
}

function childNamed(node, name) {
  return node.children.find((child) => child.name === name) ?? null
}

function childrenNamed(node, name) {
  return node.children.filter((child) => child.name === name)
}

function topLevel(document, name) {
  return document.nodes.find((node) => node.name === name) ?? null
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

/** Translation · Rz · Ry · Rx · Scale — FBX's default eEulerXYZ order, in degrees. */
function compose(translation, rotation, scaling) {
  const [rx, ry, rz] = rotation.map((degrees) => (degrees * Math.PI) / 180)
  const [sx, sy, sz] = scaling
  const cx = Math.cos(rx)
  const sxr = Math.sin(rx)
  const cy = Math.cos(ry)
  const syr = Math.sin(ry)
  const cz = Math.cos(rz)
  const szr = Math.sin(rz)

  const r00 = cz * cy
  const r01 = cz * syr * sxr - szr * cx
  const r02 = cz * syr * cx + szr * sxr
  const r10 = szr * cy
  const r11 = szr * syr * sxr + cz * cx
  const r12 = szr * syr * cx - cz * sxr
  const r20 = -syr
  const r21 = cy * sxr
  const r22 = cy * cx

  const m = new Float64Array(16)
  m[0] = r00 * sx
  m[1] = r10 * sx
  m[2] = r20 * sx
  m[4] = r01 * sy
  m[5] = r11 * sy
  m[6] = r21 * sy
  m[8] = r02 * sz
  m[9] = r12 * sz
  m[10] = r22 * sz
  m[12] = translation[0]
  m[13] = translation[1]
  m[14] = translation[2]
  m[15] = 1
  return m
}

/**
 * Read a `Properties70` entry: `P: name, type, subtype, flags, value…`.
 * Values always start at index 4, whatever the declared type.
 */
function property70(node, name, fallback) {
  const container = childNamed(node, 'Properties70')
  if (!container) return fallback
  for (const entry of childrenNamed(container, 'P')) {
    if (entry.properties[0] === name) {
      const values = entry.properties.slice(4).filter((value) => typeof value === 'number')
      if (values.length) return values
    }
  }
  return fallback
}

function localMatrix(model) {
  return compose(
    property70(model, 'Lcl Translation', [0, 0, 0]),
    property70(model, 'Lcl Rotation', [0, 0, 0]),
    property70(model, 'Lcl Scaling', [1, 1, 1]),
  )
}

function geometricMatrix(model) {
  return compose(
    property70(model, 'GeometricTranslation', [0, 0, 0]),
    property70(model, 'GeometricRotation', [0, 0, 0]),
    property70(model, 'GeometricScaling', [1, 1, 1]),
  )
}

/* ------------------------------------------------------------------ mesh building */

/**
 * Parse an FBX file into one flat-shaded triangle soup ready for the viewer.
 *
 * Normals are computed per triangle rather than read from `LayerElementNormal`: the
 * mapping/reference-mode matrix there has half a dozen combinations, and for a preview
 * a face normal is both correct and free.
 */
export async function buildFbxMesh(buffer) {
  const document = parseFbx(buffer)
  const objects = topLevel(document, 'Objects')
  if (!objects) throw new Error(say('fbx.noObjects'))

  const geometries = childrenNamed(objects, 'Geometry').filter(
    (node) => node.properties[2] === 'Mesh' || childNamed(node, 'Vertices'),
  )
  if (!geometries.length) throw new Error(say('fbx.noGeometry'))

  const models = new Map()
  for (const model of childrenNamed(objects, 'Model')) models.set(model.properties[0], model)

  // OO connections are child -> parent, which is how a geometry finds its model and a
  // model finds the node it hangs under.
  const parents = new Map()
  const connections = topLevel(document, 'Connections')
  for (const connection of connections ? childrenNamed(connections, 'C') : []) {
    if (connection.properties[0] === 'OO') {
      parents.set(connection.properties[1], connection.properties[2])
    }
  }

  const axisMatrix = upAxisMatrix(document)

  const parts = []
  let triangleTotal = 0
  for (const geometry of geometries) {
    const vertexProperty = childNamed(geometry, 'Vertices')?.properties[0]
    const indexProperty = childNamed(geometry, 'PolygonVertexIndex')?.properties[0]
    if (!vertexProperty?.array || !indexProperty?.array) continue

    const vertices = await readArrayProperty(vertexProperty)
    const indices = await readArrayProperty(indexProperty)
    if (vertices.length < 9 || indices.length < 3) continue

    parts.push({ vertices, indices, matrix: worldMatrix(geometry.properties[0]) })
    triangleTotal += countTriangles(indices)
  }
  if (!parts.length) throw new Error(say('fbx.noReadableGeometry'))

  const truncated = triangleTotal > MAX_TRIANGLES
  const triangles = Math.min(triangleTotal, MAX_TRIANGLES)
  const positions = new Float32Array(triangles * 9)
  const normals = new Float32Array(triangles * 9)
  const bounds = { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] }
  let written = 0

  for (const part of parts) {
    const points = transformVertices(part.vertices, part.matrix)
    const polygon = []
    for (let i = 0; i < part.indices.length; i += 1) {
      const raw = part.indices[i]
      const last = raw < 0
      polygon.push(last ? ~raw : raw)
      if (!last) continue
      // Fan triangulation: FBX polygons are planar and convex in practice, and a
      // preview does not justify carrying an ear-clipper.
      for (let corner = 1; corner + 1 < polygon.length; corner += 1) {
        if (written >= triangles) break
        if (addTriangle(points, polygon[0], polygon[corner], polygon[corner + 1], written)) {
          written += 1
        }
      }
      polygon.length = 0
    }
  }

  if (!written) throw new Error(say('fbx.noTriangles'))

  return {
    positions: positions.subarray(0, written * 9),
    normals: normals.subarray(0, written * 9),
    colors: null,
    color: [0.72, 0.75, 0.82],
    bounds,
    stats: { triangles: written, meshes: parts.length, truncated },
  }

  function worldMatrix(geometryId) {
    const modelId = parents.get(geometryId)
    const model = models.get(modelId)
    if (!model) return axisMatrix

    let matrix = geometricMatrix(model)
    let current = model
    const seen = new Set()
    while (current && !seen.has(current.properties[0])) {
      seen.add(current.properties[0])
      matrix = multiply4(localMatrix(current), matrix)
      current = models.get(parents.get(current.properties[0])) ?? null
    }
    return multiply4(axisMatrix, matrix)
  }

  function addTriangle(points, a, b, c, slot) {
    const ax = points[a * 3]
    const ay = points[a * 3 + 1]
    const az = points[a * 3 + 2]
    const bx = points[b * 3]
    const by = points[b * 3 + 1]
    const bz = points[b * 3 + 2]
    const cx = points[c * 3]
    const cy = points[c * 3 + 1]
    const cz = points[c * 3 + 2]
    if (![ax, ay, az, bx, by, bz, cx, cy, cz].every(Number.isFinite)) return false

    let nx = (by - ay) * (cz - az) - (bz - az) * (cy - ay)
    let ny = (bz - az) * (cx - ax) - (bx - ax) * (cz - az)
    let nz = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    const length = Math.hypot(nx, ny, nz)
    if (length === 0) {
      // Degenerate triangle: keep it out of the buffer rather than shading it black.
      return false
    }
    nx /= length
    ny /= length
    nz /= length

    const base = slot * 9
    const corners = [ax, ay, az, bx, by, bz, cx, cy, cz]
    for (let i = 0; i < 9; i += 1) positions[base + i] = corners[i]
    for (let i = 0; i < 3; i += 1) {
      normals[base + i * 3] = nx
      normals[base + i * 3 + 1] = ny
      normals[base + i * 3 + 2] = nz
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

/** Some exporters keep the DCC tool's Z-up world; the viewer is Y-up. */
function upAxisMatrix(document) {
  const settings = topLevel(document, 'GlobalSettings')
  const upAxis = settings ? property70(settings, 'UpAxis', [1])[0] : 1
  if (upAxis !== 2) return identity()
  return compose([0, 0, 0], [-90, 0, 0], [1, 1, 1])
}

function transformVertices(vertices, matrix) {
  const count = Math.floor(vertices.length / 3)
  const out = new Float64Array(count * 3)
  for (let i = 0; i < count; i += 1) {
    const x = vertices[i * 3]
    const y = vertices[i * 3 + 1]
    const z = vertices[i * 3 + 2]
    out[i * 3] = matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12]
    out[i * 3 + 1] = matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13]
    out[i * 3 + 2] = matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14]
  }
  return out
}

function countTriangles(indices) {
  let triangles = 0
  let corners = 0
  for (let i = 0; i < indices.length; i += 1) {
    corners += 1
    if (indices[i] < 0) {
      if (corners >= 3) triangles += corners - 2
      corners = 0
    }
  }
  return triangles
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
    gl.drawArrays(gl.TRIANGLES, 0, vertexCount)
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
  // The reader and the viewer speak the interface language from here on, too.
  translate = t

  return defineComponent({
    name: 'FbxPreview',
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

      onMounted(async () => {
        try {
          const mesh = await buildFbxMesh(await rawFileBytes(props.path))
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
                [
                  h('span', t('tris', { count: stats.value.triangles })),
                  h('span', t('meshes', { count: stats.value.meshes })),
                  stats.value.truncated
                    ? h('span', { style: { color: 'var(--vxs-warning)' } }, t('truncated'))
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
