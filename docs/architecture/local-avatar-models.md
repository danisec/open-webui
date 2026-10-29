# Local avatar models (S1)

Voice mode can render a Live2D character whose mouth follows the spoken audio.
The model is supplied by the user from their own device and never touches the
server. This document covers why that path was chosen, how it works, and what
the limits are.

## Why local-only

Two independent constraints pointed the same way.

**Rendering is client-side.** Open WebUI builds with `adapter-static`, and the
only process in the container is `uvicorn`. Avatar rendering happens through
WebGL in the user's browser; the server's whole role is `open()` + `sendfile()`
in `routers/files.py`. Adding replicas behind a load balancer would not make a
model load faster, because the server never renders anything.

**Model licences forbid redistribution.** The witch model used for testing
(`SnowBear Project`, BOOTH 6499774) permits personal use but states:
*"Redistribution and resale are prohibited. Modification of the model or the
original artwork is not allowed."* Uploading it to a shared instance, or baking
it into an image pushed to a registry, is redistribution.

Storing the model in the browser satisfies both: zero server bytes, and each
user runs their own licensed copy.

## The hardware reality

The k8s node runs on 2 vCPU with no GPU. That is irrelevant here, and that is
the point: every pixel is rendered on the client.

Texture memory is the real constraint, and it is paid by whoever opens the page:

| Texture | Pixels | VRAM as RGBA | With mipmaps |
|---|---|---|---|
| 8192×8192 | 67.1 MP | 256 MiB | — |
| 4096×8192 | 33.6 MP | 128 MiB | — |
| Both | 100.7 MP | **384 MiB** | **≈ 512 MiB** |

A model at this size works on a desktop with a discrete GPU and fails as a
blank canvas on mobile drivers whose `MAX_TEXTURE_SIZE` is 4096. The loader
reads the device's real limit with `gl.getParameter(gl.MAX_TEXTURE_SIZE)` and
refuses before uploading, rather than showing nothing.

Reading that limit correctly turned out to be its own problem; see
[The texture-limit false positive](#the-texture-limit-false-positive).

## How it works

```
user picks .zip
  → fflate unpacks in the browser
  → settings JSON parsed, declared resources checked
  → one object URL per archive entry
  → Cubism Core injected, runtime imported dynamically
  → Cubism4ModelSettings with resolveURL pointed at those object URLs
  → PIXI renders to a canvas inside the call overlay
  → object URLs revoked when the model is replaced or forgotten
```

### Files

| File | Role |
|---|---|
| `src/lib/utils/live2d-model-files.ts` | Unpack zip, resolve paths, probe texture size and GL limit, own object URLs |
| `src/lib/utils/live2d-avatar-renderer.ts` | PixiJS + Cubism 4 renderer, lip-sync and idle motion |
| `src/lib/utils/avatar-storage.ts` | IndexedDB persistence of the picked archive |
| `src/lib/utils/audio-level.ts` | RMS of `#audioElement` for lip-sync |
| `src/lib/components/.../VoiceAvatarLive2D.svelte` | Canvas host, rAF loop, visibility and context-loss handling |
| `static/live2dcubismcore.min.js` | Cubism Core (redistributable per Live2D's licence) |

### Why the library's `File[]` path is not used

`pixi-live2d-display` accepts an array of `File` objects and maps each
`webkitRelativePath` to an object URL. It cannot handle this archive.

`FileLoader.factory` validates with

```js
settings.validateFiles(files.map((f) => encodeURI(f.webkitRelativePath)));
```

while `ModelSettings.resolveURL` returns the **raw** path from
`utils.url.resolve`. For entries named `魔女/魔女.moc3`, `encodeURI` produces
`%E9%AD%94%E5%A5%B3/...`, the two sides never match, and loading throws
*"file is defined in settings, but doesn't exist in given files"*. `upload()`
has the mirror-image problem, comparing raw paths against decoded ones.

So the model is not handed over as `File[]`. `live2d-model-files.ts` parses the
settings JSON itself, creates one object URL per entry, and builds a
`Cubism4ModelSettings` whose `resolveURL` is overridden to return those URLs.
`Live2DFactory` accepts a `ModelSettings` instance directly, which skips the
`File[]` middleware entirely and keeps every path literal.

`new URL()` cannot join these paths either, for the same percent-encoding
reason, so `resolveRelative()` walks the segments by hand.

### Why the audio graph is built late

The realtime path gets `bridge.outputLevel` from the server's PCM stream. The
classic path has no such stream: audio goes into the shared `#audioElement`
declared in `Chat.svelte`.

Two browser rules make this delicate:

1. `createMediaElementSource` may be called **once** per element, and afterwards
   the element's output is permanently routed through the node. A node that is
   not connected to `destination` means silence.
2. An `AudioContext` created outside a user gesture starts `suspended`, and a
   suspended context produces no sound.

Routing the element into a suspended context would mute the assistant, and the
routing cannot be undone. So `prepareAudioLevel()` creates the context, awaits
`resume()`, and only calls `createMediaElementSource` once the state is
`running`. If it is not, nothing is created and the element keeps playing
normally; lip-sync degrades to a closed mouth.

The graph is page-scoped and kept across mounts, because the `<audio>` element
outlives the overlay. The analyser loop runs only while the assistant is
speaking.

## Using it

1. Open voice mode.
2. Press the face button (next to the camera button) and pick a `.zip`
   containing a `.model3.json` and its resources.
3. The archive is unpacked locally and stored in IndexedDB, so it is restored
   on the next call without picking it again.
4. The trash button forgets it.

Errors are shown under the status line rather than swallowed: an incomplete
archive, a texture above the device limit, or a load failure all produce a
message.

## Limits

- **Cubism 4/5 only.** Cubism 2 models use `.model.json` + `.moc` and a
  different runtime; they are not supported.
- **No gestures or expressions.** This model declares none in its
  `model3.json`, so `playGesture` reports `unavailable` instead of guessing a
  mapping.
- **Model is per-browser.** IndexedDB is not synced; a different device needs
  the file picked again.
- **The 512 MiB figure is unavoidable** for this particular model. Reducing it
  means reducing the texture, which the licence forbids without permission.

## The fit() scale bug

`fit()` used to compute the scale as `rect.height / model.height`. In pixi,
`model.height` is a **getter** defined as `scale.y * getLocalBounds().height` —
it is not the model's intrinsic size. Dividing by it means the divisor changes
with the very scale `fit()` is about to set, so the function is not idempotent
and successive calls flip between two values.

It always ran at least twice on mount: once explicitly, and once from
`ResizeObserver`, which delivers an initial callback as soon as `observe()` is
called. With a 5167×9410 model in a ~400×300 panel that produced:

| call | `model.height` read | new scale | drawn size |
|------|--------------------|-----------|-----------|
| 1 | 9410 | 0.0291 | 152 × 276 px |
| 2 | 274 | **1.0000** | **5167 × 9410 px** |

Scale 1 always came second, so the panel ended up showing 7.7% of the model's
width and 3.2% of its height, centred — the waist and skirt. The report
"only the skirt is visible" was this, not a clipping or canvas-size problem.

The fix reads the intrinsic size once and derives the scale from constants:

```ts
const baseWidth = model.internalModel.width;
const baseHeight = model.internalModel.height;
const scale = Math.min(rect.height / baseHeight, rect.width / baseWidth) * 0.92;
```

`Math.min` over both axes also replaces height-only fitting, which silently
clipped any rig wider than its panel. `魔女` is portrait so it survived that
bug by luck.

Two things worth remembering when touching this code:

- Never use `model.width` / `model.height` as a *measurement* of a Live2D model.
  They are outputs of the current transform, not inputs.
- Any `ResizeObserver` callback runs once immediately. Code that assumes it
  only fires on real resizes will misbehave in exactly this way.

## The lip-sync gain bug

The mouth did not move during speech. Nothing was disconnected: the analyser
graph was built, `audioLevel()` returned non-zero, and the renderer wrote to
`ParamMouthOpenY` every frame. The value was simply too small to see.

`audioLevel()` returns raw RMS of the time-domain samples. Speech is naturally
low in RMS — crest factor 10–20× is normal — so peaks land around 0.13. Measured
against a real Supertonic3 response for this rig:

| stage | value |
|-------|-------|
| WAV | 44100 Hz, mono, 16-bit, 2.368 s |
| raw RMS | 1604 / 32768 = 0.049 |
| `audioLevel()` mean | 0.0324 |
| `audioLevel()` peak | 0.1330 |
| `mouthWeight` peak after smoothing | 0.0964 |
| `ParamMouthOpenY` travel | 0 … 2.1 |
| **mouth open at peak** | **4.59 %** |

Four percent of travel is a one-pixel twitch at the lip edge — indistinguishable
from broken. The VRM renderer had always compensated
(`Math.min(1, level * 7) * 0.7` in `voice-avatar-renderer.ts`); this path
multiplied by 1.

### Three defects, one write

1. **No gain.** Fixed with `MOUTH_GAIN = 4`, which puts speech peaks at ~39 % of
   travel. Above ~8 the clamp saturates on every stressed syllable and the mouth
   sticks open, so this is not a knob to raise carelessly.

2. **Clamped to a normalised 0..1 range that the rig does not use.** `魔女`
   authors `ParamMouthOpenY` as 0..2.1 — its own `魔女.vtube.json` maps
   `MouthOpen 0..1` to `0..2.1`. Writing `mouthWeight` directly capped the mouth
   at 48 % of travel even at full scale. The ceiling is now read from the model:

   ```ts
   const mouthIndex = core.getParameterIndex?.(MOUTH_PARAM) ?? -1;
   const rawMouthMax = mouthIndex >= 0 ? core.getParameterMaximumValue?.(mouthIndex) : undefined;
   const mouthMax =
       typeof rawMouthMax === 'number' && Number.isFinite(rawMouthMax) && rawMouthMax > 0
           ? rawMouthMax
           : 1;
   ```

   The `Number.isFinite` guard is not defensive noise. `getParameterIndex`
   fabricates an index for unknown ids (see below), so it can point past the end
   of `maximumValues` and yield `undefined`. `NaN` reaching a parameter value
   propagates into the model matrix and the whole model disappears.

3. **`ParamMouthForm` never returned to rest.** It was set to 0.4 while speaking
   and never written again, so the first response left the character smiling
   until reload. It now eases to zero and *stops writing once settled*, so
   expression files that drive the same parameter are not overwritten at 60 Hz.

### `hasParameter()` cannot fail

`CubismModel.getParameterIndex()` never returns `-1`. Unknown ids are registered
in `_notExistParameterId` and handed a synthetic index past the real table
(`cubism4.es.js:2094`):

```js
parameterIndex = this._model.parameters.count + Object.keys(this._notExistParameterId).length;
this._notExistParameterId[parameterId] = parameterIndex;
```

So the capability check in this file always reports `true`. It is harmless —
writing a parameter the rig lacks does nothing — but it means a rig with
differently-named mouth parameters cannot be *detected* as missing, only
silently ignored. A real check would read `getModel().parameters.ids` and
`indexOf` it.

### The silent-failure path

`CallOverlay.svelte` returned from `startAvatarLevel` without a word when
`prepareAudioLevel` reported failure — which happens whenever the `AudioContext`
cannot reach `running` (autoplay policy, background tab, no gesture yet). That
made "graph could not start" indistinguishable from "renderer is broken". It now
logs once per speaking transition.

Both failure modes degrade safely: the element keeps playing normally and the
mouth simply stays shut.

### Verified numbers

Simulated against the real WAV through the exact `audioLevel()` and smoothing
code paths:

| | peak | mean |
|---|---|---|
| before | 4.59 % | 1.55 % |
| after | **38.6 %** | **13.0 %** |

`ParamMouthForm` decays below 0.001 in 1.0 s once speech stops.

## The texture-limit false positive

A phone that renders the model fine reported:

> This model uses a 8192px texture, above this device's 4096px limit. It would
> load as an empty canvas.

The model genuinely is 8192px. The **4096 was invented by the probe.** Two
separate defects produced the same confident number.

### 1. Failure was reported as a measurement

```ts
if (typeof document === 'undefined') return 4096;
const gl = canvas.getContext('webgl2') ?? canvas.getContext('webgl');
if (!gl) return 4096;               // <-- "could not tell" became "4096"
```

Three paths returned a hardcoded 4096: no `document` (SSR), no context, or a
non-finite `getParameter`. Every one of them means *the limit is unknown*. The
code turned that into a hardware claim, and the caller had no way to tell the
two apart, so it rejected the load.

`maxTextureSize()` now returns `number | null`, and `checkTextureBudget` treats
`null` as "unknown, let it try":

```ts
if (limit === null) {
    console.warn(`[avatar] could not read this device's texture limit; ...`);
    return;
}
```

That is the right trade. A model that genuinely exceeds the limit fails during
upload with its own error; a model that would have loaded is no longer refused
on the strength of a guess.

### 2. One canvas, two context types

```ts
const gl = canvas.getContext('webgl2') ?? canvas.getContext('webgl');
```

A canvas is permanently locked to the first context type requested. When
`getContext('webgl2')` returns `null` — which it does on some iOS versions, and
after a `WEBGL_lose_context` — the canvas is still claimed by `webgl2`, so the
`getContext('webgl')` fallback returns `null` too. The fallback could never
succeed. On a device that only offers WebGL1, the probe therefore reported
failure, and failure meant 4096.

The probe now uses a **separate canvas per context type**:

```ts
const probe = (type: 'webgl2' | 'webgl') => {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext(type);
    if (!gl) return null;
    const measured = textureLimitOf(gl);
    gl.getExtension('WEBGL_lose_context')?.loseContext();
    return measured;
};
limit = probe('webgl2') ?? probe('webgl');
```

### 3. The result is cached, and the renderer checks its own context

Two things follow from the probe costing a real GL context:

- **Cache it.** Browsers cap live contexts (commonly 16). Probing on every
  `checkTextureBudget` call could exhaust that budget and make the *renderer's*
  own context creation fail — trading a false negative for a real one.
- **Prefer the authoritative answer.** `createLive2DRenderer` now checks
  against `app.renderer.gl`, the context that will actually upload the texture.
  That context exists by then, so its `MAX_TEXTURE_SIZE` is exact. The
  pre-flight probe in `CallOverlay.svelte` stays only as an early, best-effort
  warning before the archive is adopted.

```ts
const app = new Application({ ... });
try {
    checkTextureBudget(source.textureEdge, textureLimitOf(app.renderer.gl));
} catch (error) {
    app.destroy(false, { children: false, texture: false, baseTexture: false });
    throw error;
}
```

### 4. The message was clipped

The error box was `line-clamp-2`, so a full sentence explaining a hardware
limit was cut mid-word and read as a truncated bug. It is now unclamped with
`break-words`, which keeps a long path from overflowing instead.

### What this does and does not fix

- **Fixed:** phones that *can* hold an 8192px texture are no longer refused,
  and "could not measure" is no longer reported as "4096".
- **Not fixed:** a device that truly reports `MAX_TEXTURE_SIZE = 4096` still
  cannot load this model. That is physics, and the only remedy is downscaling
  the texture — which the model's licence forbids without permission.

The distinction matters when reading a bug report: the old message was shown in
both cases, so "I got the 4096 error" never actually proved the device was
incapable.

## Making the avatar bigger on desktop

The model was smaller than the space it was given, for two independent reasons.

**The box had a cap that bound early.** `h-[min(70vh,40rem)]` at every
breakpoint means the `40rem` term wins on any viewport above ~915px, so the
avatar stopped growing exactly where there was room for it. Desktop now gets
`min(78vh,52rem)`:

```
class="relative h-[min(78vh,52rem)] w-full max-h-[calc(100dvh_-_12rem)]
       md:h-[min(82vh,56rem)]"
```

The `calc(100dvh - 12rem)` guard is an absolute length on purpose. This box's
ancestors are sized by content, so `max-h-full` has no definite height to
resolve against and silently does nothing — it cannot protect the control row
on a short window. 12rem covers the `p-3`/`p-6` padding, the status line and
the control row.

**The mobile cap used to bind well below the available space.** It was
`min(70vh,40rem)` at every breakpoint, so on any viewport above ~915px the 40rem
term won. Measured on a reported 651x1017 viewport: the box was 640px inside a
`flex-1` area of ~883px, leaving ~243px idle under the model and drawing the
character at roughly half the screen height. The caps are now `min(78vh,52rem)`
mobile and `min(82vh,56rem)` desktop, and the 12rem guard is unconditional
rather than `md:`-only — a taller box is exactly the case where that guard has
to hold.

**The fit margin is 1.0.** It was a flat 0.92, then per-breakpoint
(`wide.matches ? 1 : 0.94`). The 0.94 branch cost 6% of the model on precisely
the screens where space is scarcest, and bought nothing measurable: the panel
behind the model is a plain white/dark background with no border or content to
collide with. The one real risk it covered — colliding with the control row on a
short window — is handled by the 12rem reservation above. With the margin
constant, the `matchMedia` breakpoint listener is gone too, because there is no
longer a per-breakpoint value to go stale.

```ts
const scale = Math.min(rect.height / baseHeight, rect.width / baseWidth);
```

**Not done: fitting to the drawn artwork.** `internalModel.width/height` is the
model canvas, transparent padding included, so a generously padded model still
leaves room on the table. Trimming to the union of `getDrawableBounds()` would
recover it, but that has to be reconciled with `anchor`, `centeringTransform` and
the Y flip, and that maths needs verifying in a browser rather than inferring.
It is deliberately left out.
