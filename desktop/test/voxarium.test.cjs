const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../backend/plugins_builtin/vox_preview/frontend/VoxPreview.js'), 'utf8');
const modulePromise = import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const u32 = n => { const b = Buffer.alloc(4); b.writeUInt32LE(n); return b; };
const chunk = (tag, payload, second = 0) => Buffer.concat([Buffer.from(tag), u32(payload.length), u32(second), payload]);
const vox = () => Buffer.concat([Buffer.from('VOX '), u32(150), chunk('MAIN', Buffer.alloc(0), 44), chunk('SIZE', Buffer.concat([u32(2), u32(2), u32(2)])), chunk('XYZI', Buffer.from([1,0,0,0,0,0,0,1]))]);
const part = (tag, bytes, flags = 0) => Buffer.concat([Buffer.from(tag), u32(flags), u32(bytes.length), bytes]);
const project = (meta = {}, extra = [], version = 1) => Buffer.concat([Buffer.from('VXRM'), u32(version), ...extra, part('VOX ', vox()), part('PROJ', Buffer.from(JSON.stringify(meta)))]);
const array = b => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);

test('native Voxarium scene and raw VOX produce the same mesh', async () => {
  const { parseVoxelFile, buildVoxelMesh } = await modulePromise;
  const native = parseVoxelFile(array(project()));
  const imported = parseVoxelFile(array(vox()));
  assert.deepEqual(buildVoxelMesh(native), buildVoxelMesh(imported));
  assert.equal(native.models.length, 1);
});
test('native free transforms use Voxarium object vectors and update bounds', async () => {
  const { parseVoxarium, buildVoxelMesh } = await modulePromise;
  const scene = parseVoxarium(array(project({objects:[{hidden:false, free:{position:{x:10,y:20,z:30}, rotation:{x:0,y:0,z:45}, scale:{x:2,y:1,z:1}}}]})));
  assert.equal(scene.placements[0].translation[0], 10);
  assert.ok(scene.bounds.min[0] > 7 && scene.bounds.max[0] < 13);
  const mesh = buildVoxelMesh(scene);
  assert.ok(mesh.positions.every(Number.isFinite));
  assert.ok(mesh.normals.every(Number.isFinite));
});
test('native metadata visibility overrides the embedded VOX', async () => {
  const { parseVoxarium } = await modulePromise;
  const scene = parseVoxarium(array(project({objects:[{hidden:true}]})));
  assert.equal(scene.hidden, 1);
  assert.equal(scene.placements[0].hidden, true);
});
test('unknown chunks, duplicate scenes and broken optional metadata are tolerated', async () => {
  const { parseVoxarium } = await modulePromise;
  const bytes = project({}, [part('JUNK', Buffer.from('anything')), part('PROJ', Buffer.from('{'))]);
  assert.equal(parseVoxarium(array(bytes)).models.length, 1);
  assert.equal(parseVoxarium(array(Buffer.concat([bytes, part('VOX ', Buffer.from('bad'))]))).models.length, 1);
});
test('future versions, required compression and truncation fail predictably', async () => {
  const { parseVoxarium } = await modulePromise;
  assert.throws(() => parseVoxarium(array(project({}, [], 2))), /newer/);
  assert.throws(() => parseVoxarium(array(project({}, [part('VOX ', vox(), 1)]))), /compression/);
  const bytes = project();
  for (let i = 0; i < 28; i++) assert.throws(() => parseVoxarium(array(bytes.subarray(0,i))));
  const invalid = Buffer.from(bytes); invalid.writeUInt32LE(0xffffffff, 16);
  assert.throws(() => parseVoxarium(array(invalid)), /readable/);
});
