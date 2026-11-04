# image_ops

2D image nodes for the half of a generation pipeline that is still a picture.

`remove_background` → `crop` is the pair to know: the first gives a picture an alpha
channel, the second uses that alpha to decide where the picture's edges should have been.

Both draw a preview, and both carry the same small button in its top-left: it switches
`image-rendering` between smooth and `pixelated`, so pixel art can be looked at as pixels.
The choice is shared with `preview2d.view` and kept in `localStorage`, so one click sets
every preview on the canvas and it survives a reload. It is a view setting and not a port —
a port would go into the §8.2 cache key and re-run the node for an identical file.

## Remove Background

`image_ops.remove_background` keys one colour out of an image — white by default — and
writes a PNG with a real alpha channel. It is the magic wand, not a segmentation model:
it decides pixel by pixel how far a colour is from the key, which is exactly right for a
render, a sprite sheet or a product shot on a flat backdrop, and exactly wrong for a
photograph of a cluttered room. For that, `hunyuan3d.image_to_mesh` has a `remove_background`
port that runs rembg on the server side.

| Port | What it decides |
|---|---|
| `image` | The picture. PNG needs nothing installed; every other format needs the environment below |
| `key_color` | The background colour. Pick it off the image with the eyedropper on the node |
| `tolerance` | How far a pixel may differ and still count as background, 0–100 |
| `region` | `edges` floods in from the border; `everywhere` takes every matching pixel |
| `edge_sharpness` | 100 is a hard threshold; lower fades the alpha across the tolerance band |
| `antialias` | Smooths the cut edge afterwards. On by default |

Output is straight (un-premultiplied) alpha: a keyed-out pixel keeps the colour it had.

### The two regions

`edges` is the default because it is the one that keeps a white glint in an eye, the hole
in a letter, or a pale highlight in the middle of the subject — the fill only takes matching
pixels it can *reach* from the border. Switch to `everywhere` when the background is not
connected to the border, which is what a tiled sprite sheet looks like.

### Sharpness and antialiasing are not the same setting

`edge_sharpness` decides how the cut treats colours *approaching* the key: at 100 a pixel
is either background or not, at lower values it goes partly transparent as it nears the key
colour, which is what saves a soft shadow or a motion-blurred outline from being cut square.

`antialias` then smooths whatever edge that produced, so a hard cut does not read as a
staircase at 100%. Blurring a flat region gives the same region back, so it only ever
changes pixels that already sit on a transition — and it costs half a pixel of coverage on
each side, which is why it can be turned off for pixel art.

## JPEG to PNG

`image_ops.jpeg_to_png` re-containers a picture: one file in, the same pixels out as a
PNG. No settings, no eyedropper and no canvas component — there is nothing to aim and the
result is the picture that went in, so it renders as a small default node that stays out of
the way in the middle of a chain.

| Port | What it decides |
|---|---|
| `image` | The JPEG. Any other container Pillow opens converts too — the picker's list says what the node is *for*, not what it takes |

It is the one node here that **does not work before the environment is built**, and that is
the same decision as everything below: JPEG is precisely the container `nodes/imaging.py`
will not decode itself, so Pillow is its decoder rather than an optimisation. Without it the
node fails naming the button that fixes it.

The output is RGBA even though a JPEG carries no alpha. An opaque channel is a flat plane,
which `Up` filtering turns into a run of zeros and deflate charges almost nothing for —
a second encoder path would be saving something nobody would measure.

## Trim 4x4

`image_ops.trim_4x4` cuts a picture into four along its middle — a 2x2 grid — and hands
each quadrant to a port of its own. Like the converter it has no settings and no canvas
component: the only decision a halving could offer is *where* to cut, and anywhere but the
middle is a crop rather than this node.

| Port | What it carries |
|---|---|
| `image` | The picture to cut, in (at least 2x2 pixels) |
| `top_left` `top_right` `bottom_left` `bottom_right` | One PNG each, reading order |

**Four outputs rather than a folder of tiles**, and that is the point of the node: a port
each is what lets the corners go to four different places on the canvas — three of them
somewhere and the fourth nowhere — where a directory would make every consumer re-derive
which file was which corner.

An odd width or height cannot halve evenly, so the extra row or column goes to the right
and bottom quadrants: a 9x7 picture comes out as 4x3, 5x3, 4x4 and 5x4, which still
reassemble into the original. Each quadrant's filename carries the corner and a digest of
the source bytes, so the four never collide and re-running overwrites the same four files.

## Crop

`image_ops.crop` trims a picture to the bounding box of its non-transparent pixels, and
frames that box at whatever output resolution you ask for. It is Remove Background's usual
sequel: key the backdrop out, then cut off the empty margin the backdrop was filling.

| Port | What it decides |
|---|---|
| `image` | The picture, alpha channel and all |
| `alpha_threshold` | How opaque a pixel must be to count as subject, 0–100. 0 takes anything not fully transparent |
| `width` `height` | Output size in pixels, or 0 for the bounding box's own — the plain trim. Larger than the box pads; smaller shrinks the subject to fit |

The node draws its own preview, and the **Source** button is why: a crop is decided by two
rectangles nobody typed — the box the node *measured* and the frame it placed around that
box — so switching to the source draws both over it. The result is otherwise a picture with
no explanation of where its edges came from.

### A frame with room pads; a frame without it shrinks the subject

Those are the only two cases, and the box is centred in both. An odd difference cannot be
split evenly, so the frame's centre goes half a pixel right of and below the box's — a wider
frame pads one more column on the right than the left. Worth knowing before comparing two
runs at neighbouring resolutions and wondering which way the subject moved.

**Padding never touches a pixel.** Resampling a picture that already fits would cost it
detail for nothing, so the ordinary trim and the ordinary pad are exact.

**Shrinking uses one factor for both axes**, so the subject keeps its proportions and the
slack on the other axis becomes transparent margin. It fits inside the frame and is centred
there; it is never stretched to fill, which would be the one framing that changes what the
subject *is*, and it is never cut, which is what this node did before 1.1.0.

### The shrink is a median, and it selects rather than mixes

An average of two colours is a third colour that was never in the picture, and on a keyed
sprite that third colour is a rim of half-background all the way round the subject. A median
cannot produce that rim.

The colour is taken from a single source pixel — the footprint ranked by `r + g + b`, and
that pixel used whole. That is deliberately **not** the textbook per-channel median, which
takes the middle red, the middle green and the middle blue independently and so recombines:
a footprint straddling an orange body and a blue outline medians to a pink that is in
neither. Selecting whole means **the output's palette is a subset of the input's**, which is
what a `.vox`, an indexed PNG or pixel art needs downstream. On the demo project's logo,
shrinking a 235x245 cutout to 128 square yields 229 colours, none of them new.

Alpha is medianed separately over the whole footprint, because coverage is not colour: a
footprint that is nine parts transparent comes out nearly transparent whichever pixel lends
it its colour. And the colour is ranked only among the pixels that *draw* something — straight
alpha means a keyed-out pixel still carries the background's colour, so letting those rank
would put the colour the key just removed back on the subject's edge. That distinction only
runs on the outline, so it costs about 1%.

It is pure standard library like the rest of the plugin. Measured on this machine: a 2048
square box down to 512 takes 0.8 s, 4096 down to 512 takes 1.9 s. An extreme reduction thins
each footprint to a regular grid of at most 16x16 of its source pixels — deterministic, so
the §8.2 cache key still describes the result.

### The threshold is for the halo, not for the subject

`Remove Background` with antialiasing on leaves a fringe of barely-there alpha around the
cut, and at the default 0 that fringe is part of the box. A few percent excludes it: on the
demo project's logo the box goes from 235x245 to 232x242 at 30%, which is the antialiased
edge and nothing else.

### What it will not do

A **fully transparent** picture fails the node rather than producing something: there is no
rectangle, so there is nothing to centre and nothing to write. A picture carrying **no alpha
at all** — every JPEG, most flattened PNGs — is opaque everywhere, so its box is the whole
picture; the node says so in the log and goes on to frame it, because "there was no margin
to trim" and "the node is broken" look identical otherwise.

## The environment

Nothing has to be installed for PNG: `nodes/imaging.py` decodes and encodes it on `zlib`,
so the node works the moment the plugin is enabled, and its own output is a PNG.

Building the environment (Plugins → Build environment) installs Pillow, which adds JPEG,
WebP, BMP, TGA and TIFF — and takes over PNG too, because it is faster and produces
identical pixels. That last point is the difference from `hunyuan3d/nodes/imaging.py`, which
deliberately keeps Pillow away from JPEG: PNG decoding has exactly one right answer, while
two JPEG decoders need not agree in the last bit, and the §8.2 cache key cannot see which
one ran.

## What it does not do

- **No colour decontamination.** A semi-transparent edge pixel keeps its blended colour, so
  a subject cut off a strong background can show a faint rim of it when composited over
  something very different. Keying with a lower `edge_sharpness` usually reads better than
  a defringe would.
- **One key colour.** Two backdrops mean two passes, chained.
- **The first frame only** of an animated GIF or APNG.
