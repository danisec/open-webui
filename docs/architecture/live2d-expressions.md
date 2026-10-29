# Live2D expressions from the reply's emotion

The 2D avatar's face now reacts to what it is saying. This document records how,
why it is built this way, and — importantly — what it cannot do.

## What was already there

Nothing in the Live2D path ever touched expressions. The renderer wrote exactly
seven parameters:

| Parameter | Purpose |
|---|---|
| `ParamMouthOpenY` | lip-sync |
| `ParamMouthForm` | smile while speaking |
| `ParamBreath` | breathing |
| `ParamAngleX/Y/Z`, `ParamBodyAngleX/Z` | idle sway |

`setExpression` / `ExpressionManager` appeared **nowhere** in the Live2D path.
The only `expressionManager` in the codebase was in `voice-avatar-renderer.ts`,
which is the VRM path and is unrelated.

Meanwhile the emotion was already being computed and then discarded. The emoji
task (`/api/v1/tasks/emoji/completions`) prompts an LLM with:

> "Your task is to reflect the speaker's likely facial expression through a
> fitting emoji. Interpret emotions from the message…"

`CallOverlay.svelte` already called it per reply chunk. But the result was only
ever drawn as a large emoji glyph, and only when the user had enabled
`showEmojiInCall`. For a Live2D user the emotional reading was computed and
thrown away.

## The pipeline

```
reply chunk
   -> emoji task (LLM, existing)
   -> moodFromEmoji()          emoji -> happy|sad|angry|surprised|relaxed|null
   -> resolveExpressionName()  mood -> a name the model actually declares
   -> ExpressionManager.setExpression(name)
```

`live2d-expressions.ts` holds the first two steps as pure functions.

**Why five buckets.** The input is one emoji. That is the entire bandwidth of the
signal, so anything finer would be invented granularity. Five buckets is already
generous.

**Why the resolver is fuzzy.** Rigs name expressions inconsistently — `happy`,
`Happy`, `exp_happy`, `happy_01`. The resolver tries exact match, then
case-insensitive, then shortest substring, and returns `null` when nothing
matches. A model that names its expressions `F01`/`F02` gets no expressions,
because guessing which integer means "happy" would be wrong more often than
right.

## The non-obvious hazard: render order

This is the part that would have shipped broken.

`InternalModel.update` runs in this order:

```
motionManager.update(coreModel, now)
saveParameters()
expressionManager.update(model, now)     <-- expression writes parameters HERE
eyeBlink, focus, natural movements, lipSync
physics, pose
emit('beforeModelUpdate')
coreModel.update()                        <-- mesh calculated HERE
```

The renderer writes `ParamMouthOpenY` from its own `render()` loop, which runs
*before* `model.update()` triggers all of the above. So an expression that drives
`ParamMouthOpenY` overwrites the lip-sync value **after** it was written, and the
mouth stops moving for as long as that expression is active.

A still screenshot would look completely fine. Only watching the avatar speak
would reveal it.

The fix re-asserts the mouth parameters from the `beforeModelUpdate` event, which
fires after the expression manager has run and before the mesh is calculated:

```ts
const assertMouthAfterExpression = () => {
    if (!input_speaking) return;                 // never flatten a resting face
    if (mouth) core.setParameterValueById(MOUTH_PARAM, mouthWeight * mouthMax);
    if (mouthForm && mouthFormWeight > 0.001) {
        core.setParameterValueById(MOUTH_FORM, mouthFormWeight);
    }
};
internal.on('beforeModelUpdate', assertMouthAfterExpression);
```

Only the mouth is re-asserted, and only while speaking. Brows, eyes and mouth
*shape* from the expression are left intact — overwriting everything would defeat
the point of having expressions at all. The handler is removed in `dispose()`.

## Deliberate failure behaviour

Every failure path leaves the face unchanged rather than throwing. A broken
expression file must never break a voice call.

| Condition | Behaviour |
|---|---|
| Model declares no `Expressions` | Emoji task not called at all (no cost); face unchanged |
| Emoji maps to no known mood | Face unchanged |
| Mood resolves to a name the rig lacks | Face unchanged |
| `.exp.json` fails to load | Retried, capped at 3 attempts total |
| Mood arrives before the rig has loaded | Held in `pendingMoodEmoji`, replayed from `on:ready` |

That last cap matters. The library loads expression files lazily and asynchronously,
and the reconcile step runs inside the render loop. Without a cap, a permanently
broken expression file would allocate a promise **every frame** — 60 per second
for the duration of the call. `EXPRESSION_MAX_ATTEMPTS = 3` turns a permanent
failure into a bounded cost. A fresh request from the caller resets the budget; a
retry does not.

The `pendingMoodEmoji` row is the fix for the bug that made this feature appear
completely dead through v24. The trap is that `bind:this` and the expression list
become available at very different moments: the handle is assigned when the
component *mounts*, the list only when `ready` fires, after the archive has been
unpacked. An empty list during that window means "not yet", not "never", and
treating the two the same discards every mood that arrives while a large rig
loads — which is the opening of the call. `avatarReady` is the flag that keeps
them apart.

It is deliberately a **single slot**, not a queue. If four chunks arrive during
the load, only the last one is replayed. That is the correct outcome rather than
a shortcut: by the time the rig is ready, the earlier chunks have already been
spoken, so replaying them in sequence would walk the face through expressions for
sentences the user can no longer hear. Verified in the harness: four moods fed
during load produced `held: ["😊"]` and `applied: ["happy","angry","sad","surprised"]`
— the three that arrived *after* readiness applied normally, and the one that was
held replayed once.

## Authoring expressions for a rig that has none

A rig with no `FileReferences.Expressions` cannot be fixed from the application
side, but the model file can be fixed. `/home/dani/add-expressions.py` does this
for one specific model and is a useful template:

1. Read the rig's own `*.vtube.json` if present. It usually lists the output range
   of every parameter (`OutputRangeLower`/`Upper`), which is the only reliable way
   to know that e.g. `ParamMouthOpenY` runs `0..2.1` and not `0..1`. Guessing
   here produces expressions that are technically applied and visually invisible.
2. Drive real facial parameters, not the rig's custom effect switches. On the
   witch rig the twelve shipped `.exp3.json` files each pushed a single custom
   parameter (`Param64`–`Param72`) to `30` — those toggle sparkle/icon art, not
   muscles. Registering them produces decoration, not a smile.
3. Use `Blend: "Overwrite"` for facial parameters so switching moods cannot stack
   a previous pose underneath. `Add` accumulates.
4. Register the files under `FileReferences.Expressions` with names containing the
   mood word, since the resolver matches on the name.

Values are a starting point, not a measurement. Whether `ParamBrowLY = -0.7`
reads as "sad" or as "sulking" is a visual judgement on somebody else's artwork,
and only looking at the render can settle it.

## What this cannot do

- **It cannot invent expressions.** If the uploaded `model3.json` has no
  `FileReferences.Expressions`, nothing here can make the face move. This is a
  property of the model, not of the code. Check with:
  `Application → IndexedDB → open-webui-avatar → models → live2d`, inspect the
  `.model3.json` for an `Expressions` array.
- **It cannot read nuance.** Five moods from one emoji. Sarcasm, wistfulness and
  "politely disagreeing" all collapse into the same bucket.
- **It reacts per chunk, not per sentence meaning.** The expression follows the
  audio chunk currently being spoken, which is what makes it feel synced, but it
  means a chunk boundary can flip the face mid-thought.
- **It costs one extra LLM call per chunk when the avatar has expressions** —
  the same call that already happened when the emoji glyph was enabled. The gate
  is now `showEmojiInCall || avatarModel !== null`, so a user who hid the glyph
  but kept the avatar still pays for it, which is the point.

## Files

| File | Role |
|---|---|
| `src/lib/utils/live2d-expressions.ts` | emoji → mood → expression name (pure, unit-tested) |
| `src/lib/utils/live2d-avatar-renderer.ts` | `setExpression()`, expression capability, mouth-order correction |
| `src/lib/components/chat/MessageInput/CallOverlay/VoiceAvatarLive2D.svelte` | exposes `setExpression` / `getExpressions` |
| `src/lib/components/chat/MessageInput/CallOverlay.svelte` | applies the mood per spoken chunk |

Tests: `/home/dani/test-live2d-expressions.mts` (30 assertions, all pure).

The renderer and the component were also verified end-to-end in headless Chrome
against a real 40 MB rig, by screenshotting the canvas at each expression and
looking at the face. That harness is what located the v25 bug: the pipeline above
was correct at every step, and the mood had already been discarded before it
reached step one. Reproduce it with the snippet in
`live2d-expression-diagnosis.md`.
