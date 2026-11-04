# hunyuan3d — Tencent's Hunyuan3D

Image(s) → mesh → voxels, as a real plugin. `hunyuan3d.image_to_mesh` and
`hunyuan3d.multiview_to_mesh` run the model; `hunyuan3d.voxelize` turns what they made
into a `.vox` with a real palette.

Getting the result *into* an engine is somebody else's node: `unreal.import_asset` for
Unreal, `core.write_bin_file` for a project that only needs the file to land somewhere.

## Hunyuan3D is image-to-3D, and only that

This is the first thing to know, because it decides what the nodes need from you. Every
server in the family — the local Hunyuan3D-2.1 API and upstream's `api_server.py` alike —
validates its request body as *"one image field, exactly one"*. **There is no text field**,
and the packages behind local mode condition on a picture just the same. Connect one to a
node's image port; a run without it is refused before anything is sent, rather than 422'd
by the server a moment later.

## Two nodes, because there are two shape models

| Node | Model | Input |
|---|---|---|
| **Hunyuan3D (Image → Mesh)** | `hunyuan3d-dit-v2-1` | one reference image |
| **Hunyuan3D (Multiview → Mesh)** | `hunyuan3d-dit-v2-mv` | up to four views of one object |

The multi-view model is conditioned on several views at once and gives each its own
positional embedding — it knows that `back` is the same object seen from behind, not a
second object. From one picture the far side is always invented; with views of it, the
model builds it.

They are separate node types rather than one node with three optional ports because
**which model ran changes the mesh**, and that has to be in the §8.2 cache key and visible
on the canvas. A node type carries both for free; an optional port that happened to be
connected would carry neither. On the wire it is `images` instead of `image`, plus
`shape_model: "mv"` — the multi-view node names the model rather than leaving it to the
server's `--shape-model` flag, so the same graph produces the same mesh whatever the
server was started with.

Which one to reach for: **if you only have one picture, use Image → Mesh.** The multi-view
checkpoint is a Hunyuan3D-2.0-generation model — 3072 latents against 4096, DINOv2-giant
instead of large — and on a single image the 2.1 single-view model usually returns smoother,
more detailed geometry. Multi-view wins when the views genuinely exist.

**Front is required, the other three are optional.** The server itself takes any one view;
this node narrows that to the front specifically, because upstream's texture pass is
conditioned on **one** image (`input_images[0:1]`), so a mesh whose texture was baked from
its left side is a surprise nobody asked for. If the front is missing the node says which
view the bake will use instead. Geometry is unaffected — every view given is used for that.

The views are `front`, `left`, `back`, `right`: the front, then clockwise seen from above.
They must show the same object at roughly the same scale; background removal runs per view,
so there is no need to cut them out first. The order they are sent in does not matter — the
server arranges them the way the model expects.

**Availability is checked before anything is queued.** The multi-view checkpoint is a
separate ~4.6 GB download (`tencent/Hunyuan3D-2mv`), and a server without it answers 409.
`GET /health` publishes `mv_available` and `mv_unavailable_reason`, so the node fails with
the directory the server looked in rather than with a status code — and **Check server** on
the settings page says the same thing before you run anything.

The two shape models are never on the card at once, whatever `--vram-mode` says: together
they are over 12 GB of weights and only one answers any given request. Alternating between
the two nodes costs about two seconds of swapping per switch; the first request to a model
that has never been loaded pays about fifteen. If most runs go through one of them, start
the server with it (`--shape-model mv`) so it is the one that warms up.

## Which format

The server exports **`glb` and `obj`, never `stl`**. The node still offers `stl` because
that is the format `hunyuan3d.voxelize`'s STL path was written for: it asks the server for
GLB and converts, in `nodes/meshio.py`, and says so in the log. Nothing is guessed — if the
conversion fails, the node fails rather than writing a renamed GLB.

`glb` is the format that carries a texture in one file. **A textured `obj` comes back as a
zip** of the OBJ, its `.mtl` and the PBR maps, so the node writes `.zip` — calling that
bundle `.obj` would break every consumer downstream that trusts the suffix. An *untextured*
`obj` is a plain `.obj`, because it needs no companions.

The voxelizer reads all of them (`stl`, `obj`, `glb`, and the zip), which is what keeps the
pipeline runnable whatever the node was asked for.

## What colour it comes out

The voxelizer samples the mesh's **base colour texture per voxel** and writes a real `.vox`
palette — up to 255 colours, median-cut from what was actually sampled. Each surface sample
already knows its barycentric weights, so the colour rides the same rasterisation the
geometry does and costs one texel lookup. A mesh with a flat material colour and no texture
gets that colour; a mesh that names neither — an STL always, since the format has no
materials — falls back to the height ramp this node has always written.

Where the image comes from depends on the format, and it is the reason `glb` is the one to
ask for: a GLB carries its texture inside itself, the zip carries it beside the `.obj` and
its `.mtl`, and a bare `.obj` has UVs and nothing to put on them. That last case is what the
voxelizer's **Texture** port is for — connect or type an image and it wins over whatever the
mesh carries, which is also how one mesh is re-skinned without exporting it again. The mesh
still needs UV coordinates for the image to land on, and an STL has none.

**PNG and baseline JPEG both decode with no dependencies at all** (`nodes/imaging.py`).
JPEG is the one that matters: Hunyuan3D's texture pass bakes a 2048x2048 baseline JPEG and
then writes `data:image/png;base64,` in front of it, every time. Nothing here trusts a
declared MIME type for that reason — the container comes from the bytes, and the
declaration only ever gets quoted back in an error.

The JPEG path decodes **DC coefficients only**, which is a 1/8-scale image: 256 square out
of 2048 square, in half a second, against a full decode that would spend a minute of
pure-Python IDCT on detail a 200-cube grid cannot hold. Pillow is used only for containers
neither decoder handles (WebP, BMP, TGA) and never in preference to them, so the same mesh
voxelises to the same colours whether or not it happens to be installed — which matters,
because §8.2 caches the result.

**A mesh that resolves to one flat colour is shaded by height in that colour, not written
as a solid block.** That is the case an untextured `baseColorFactor` produces, and a block
with no silhouette is not something anyone can look at.

## Which way is up

`.vox` is Z-up. **glTF is Y-up**, normatively, in its own specification — so every mesh this
plugin hands the voxelizer is Y-up, the OBJ and the converted STL along with the GLB. That
is what the voxelizer's **Up axis** port is for, and why its default is `y`: left on `z` the
model voxelises lying on its back, and the height ramp the `.vox` writer paints runs along
the model's depth instead of its height.

Set it to `z` for a mesh that did not come from here — a CAD-exported STL usually is Z-up —
and to a negative axis for one that arrives upside down. Every option is a proper rotation,
never a component swap: swapping would put the axis where it belongs *and* mirror the model,
which looks right until you find something with a left and a right.

## Over a server (default)

Start the API server — for the local Hunyuan3D-2.1 installation that is:

```bash
run_server.bat
```

It listens on `http://127.0.0.1:8081`, with interactive docs at `/docs`. Then on
**Hunyuan3D Settings** set `Mode` to `server` and point `Server URL` at it. **Check server**
on that page calls `/health` and shows what actually decides a run: whether texturing is
available, and how much of the card other processes are holding.

Nothing is installed into the Studio or into this plugin's venv for this, and the GPU can
be a different machine. That is why it is the default: a plugin that made *anyone* who
enables it download torch would be the wrong trade.

For a deployment behind auth, put the token in an environment variable and name that
variable in `API key env var` — the value is never written to `config.yaml` (§12).

### Queued or synchronous

`Transport` picks how the request is made:

| | |
|---|---|
| `tasks` | `POST /tasks` → poll `GET /tasks/{uid}` → `GET /files/{uid}` |
| `sync` | `POST /generate`, one connection held open for the whole generation |
| `auto` | tries `tasks`, falls back to `sync` when the server answers 404 |

`auto` is the default and the queued flow is what it lands on with any server that has it.
That flow is better for the case that matters: a textured generation takes 95–130 s on a
12 GB card, and polling reports queue position and progress while it waits, carries the
server's own face and vertex counts back instead of a number parsed out of the file, and
names the uid if the node gives up — the job is still running, and `/files/{uid}` will
have it. `auto`'s fallback is also how an upstream `api_server.py`, which has no `/tasks`,
identifies itself: by answering the probe with a 404, not by any version sniffing.

### Progress on the node, and what the timeout actually bounds

While a queued task runs, `GET /tasks/{uid}` carries a `progress` object — stage id, human
label, overall percent, position inside the stage, and an ETA once there is enough data to
extrapolate. The node forwards it straight to the canvas, so the node itself reads
`Multiview diffusion · 9/15` and `48% · ~56s` instead of only "Running", and writes one log
line per stage entered. A server too old to send the field still gets a bar; it just sweeps
instead of filling.

**`Silence timeout` is not a limit on how long a generation may take.** It is how long the
server may say *nothing* — every answered status poll resets it. So a long queue, a slow
card and a textured run at 6 views are all waited out, while a server that has been killed,
has hung or has fallen off the network ends the node after one silence window. A single
dropped poll is not silence either: polls are retried, and the log says when the server went
quiet and when it came back.

That is also why `hunyuan3d.image_to_mesh` has no `timeout_seconds` in its schema. The
engine's timeout is a wall clock over the whole node, and there is no number that both
allows a legitimate textured run and catches a dead server — the script watches the thing
that does tell them apart. A run you have changed your mind about is Cancel, as before.

Two consequences worth knowing. On `sync` the distinction cannot exist: `/generate` sends
nothing until the model is finished, so a working GPU and a dead socket look identical from
this side, and the timeout there really is a limit on the generation. And a server that
keeps answering "running" forever will be waited on forever — the node says so in the log
after one silence window (`has reported nothing new for …`) rather than ending the run,
because the job is still on the GPU either way.

### Sending the image by path

`image_path` is a path on the *server's* filesystem, and it exists because base64 of a
multi-megabyte picture is the slow part of an otherwise local call. `Send image by path`
defaults to `auto`, which uses it only when the server is on loopback. `always` is for the
case the plugin cannot detect — a share both machines mount at the same path.

## Locally

Both nodes work here too — the multi-view pipeline is conditioned on a dict of views, which
is the same shape the server's `images` map has. Its checkpoint is a **separate repository**
(`tencent/Hunyuan3D-2mv`), which is why the settings page has two checkpoint fields and two
subfolder fields; the multi-view weights sit in `hunyuan3d-dit-v2-mv`.

Append the model to `requirements.txt` and press **Build environment**:

```
--extra-index-url https://download.pytorch.org/whl/cu124
torch
# then whichever generation you cloned, installed from its checkout:
#   Hunyuan3D 2.0 → hy3dgen        (pip install -e <checkout>)
#   Hunyuan3D 2.1 → hy3dshape, hy3dpaint
```

Neither package is on PyPI under a name that resolves to the real thing, so both are
installed from a checkout. `nodes/hunyuan3d.py` accepts either layout and logs which one
it imported.

## What ends up in the cache key

Anything that changes the *result* is a node param, and params are hashed (§8.2):
checkpoint, seed, steps, guidance, octree resolution, chunk size, texture, texture
resolution, bake views, face count, background removal, format — and on the voxelizer,
resolution and the up axis. Re-running after changing one of them really re-runs.

The shape model is in the key too, and by the strongest means available: it is the node
type. Swapping Image → Mesh for Multiview → Mesh is a different node, not a different
param.

Settings named `hunyuan_*` are transport — mode, URL, silence timeout, transport, poll
interval,
image-path mode, device — and are **not** in the key. Moving the server to another host
does not invalidate meshes it already produced, which is the intended behaviour.

`face_count` used to be on the wrong side of that line, as `hunyuan_face_count`: it
decimates the mesh, so changing it silently handed back the previous result. It is a port
now, and the old setting is still read as its default so a project that set it keeps the
value.

## Tuning, and the one number that matters

The server's own measurements, on an RTX 4070 SUPER (12 GB): shape alone at 30 steps and
octree 256 is 30–39 s; shape plus PBR textures at 4 views is 95–130 s.

**`Bake views` is the parameter to be careful with.** Leave it at 0 and the server picks
by card size — 4 views under 16 GB, 6 above. On a 12 GB card 6 views do not fit, and
Windows does not fail with an OOM: it pages through WDDM, and the same 15 diffusion steps
go from ~24 seconds to tens of minutes. If a bake crawls, check `/health` before anything
else — the gap between `vram_used_gb` and `torch_reserved_gb` is other processes on the
card, and closing them is usually the whole fix.

An OOM in the *shape* stage answers 507 and the fix is `Octree resolution` (192 or 128)
and `Decode chunk size`. The node passes the server's advice through rather than reporting
a bare status code.

## Files

| File | What it is |
|---|---|
| `plugin.py` | node schemas; settings become port defaults here, at registration |
| `nodes/image_to_mesh.py` | the single-view node script: payload in, options out, artifact emitted |
| `nodes/multiview_to_mesh.py` | the same for the view map, asking for `shape_model: mv` |
| `nodes/hunyuan3d.py` | both transports, both server flows, both shape models |
| `nodes/meshio.py` | stdlib mesh reading, counting and STL writing — no trimesh |
| `nodes/imaging.py` | a stdlib PNG decoder and a texel sampler; Pillow only if it is there |
| `nodes/voxelize.py` | mesh → `.vox`, sampling the texture once per voxel |
| `frontend/` | the custom canvas node — one component for both generation nodes — and the settings page (§9.3) |
