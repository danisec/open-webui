# Diagnosing "the Live2D face never changes"

There are seven places this can fail, and from the UI they all look identical:
the face simply does not move. The build from v24 adds console logging so most
of them can be told apart in seconds.

> **The one that is not a code bug: the model denies having a face.** If the
> reply itself says "I don't have a face", "I'm just a voice" or "I'm a
> text-based assistant", the emotion reader scores that reply as flat, and a flat
> score is what it gets. Nothing in the frontend can recover an emotion the reply
> does not carry. Measured on this instance, same model and same question:
>
> | system prompt | reply | emoji | face |
> |---|---|---|---|
> | without an avatar paragraph | "Saya tidak punya wajah … model bahasa berbasis teks" | 😐 | flat |
> | with one | "My brows knit together sharply, my eyes narrow …" | 😠 | angry |
>
> The fix is a prompt change, applied to all 8 chat models by
> `/home/dani/install-avatar-prompt.py`. If the face is still flat, check the
> reply text before checking any code.
>
> **And the reason that fix kept undoing itself:** `model-healthcheck.timer` runs
> every 10 minutes and used to overwrite every model's prompt with a hardcoded
> default. Any prompt edit was reverted within ten minutes. That block now only
> seeds empty prompts. See "Editing model params" below — check it before
> concluding that a prompt change failed.

> **Fixed in v25: the mood that arrived during model load.** Before v25 the
> first sentences of every call silently lost their expression. `bind:this`
> assigns the component handle as soon as the component *mounts*, while the
> expression list only arrives with the `ready` event, seconds later for a large
> rig. The old guard `if (!avatarExpressions.length) return;` treated "list not
> delivered yet" as "this rig declares no expressions" and dropped the mood
> instead of holding it. If you are reading this on v24 or earlier, that is the
> cause, and the symptom is precise: the face only starts reacting after the
> first few sentences, or never, depending on how long the model took to load.

## Open the browser console during a voice call

You should see two lines. Read them in this order.

### Line 1 — how many expressions the model declares

```
[live2d] expressions declared by model: 3  ['happy', 'sad', 'surprised']
```

- **A number above 0** → the model supports expressions. Go to line 2.
- **`0 (none -- this model cannot change expression)`** → stop here. The model's
  `model3.json` has no `FileReferences.Expressions`. Nothing in the application
  can make its face change; this is a property of the file, not a bug.

To fix it you need a model that ships expressions. Most free Live2D samples
(`Haru`, `Hiyori`, `Mao`, `Mark`) include them; the one currently loaded
apparently does not.

### Line 2 — what the mood resolved to

```
[live2d] mood: emoji="😊" mood=happy name="happy" of [happy, sad, surprised]
```

Read the three fields:

| Field | Meaning if it looks wrong |
|---|---|
| `emoji=null` | The emoji task returned nothing for that chunk. Usually a very short reply. |
| `mood=null` | The emoji is not in the mapping table (e.g. `🍕`, `😂`). See the table in `live2d-expressions.ts`. |
| `name=null` | The mood is fine but the rig has no expression whose name contains that word. A rig named `F01`/`F02` lands here. |

If `name` is a real string and the face still does not move, the problem is
below this layer — see "if line 2 shows a valid name" below.

## If neither line appears

The expression path never ran. In order of likelihood:

1. **The avatar is not loaded.** The Live2D model must be picked via the avatar
   button in the call overlay. A model chosen in Workspace → Models is a *VRM*
   avatar for bridge calls and is a different code path entirely.
2. **TTS engine is empty.** `audio.tts.engine` is `openai` here, so this is fine,
   but with no TTS there is no per-chunk playback and therefore no chunk events.
3. **The browser served a cached bundle.** Hard-reload (Ctrl/Cmd+Shift+R) and
   confirm the chunk hash changed.

## Verifying a model works, without the app

The quickest way to separate "my code is broken" from "this model has no usable
expressions" is to drive the renderer directly in a browser and look at the
canvas. That is how the v25 bug was found, and it takes one page:

```html
<script src="/live2dcubismcore.min.js"></script>
<script type="module">
  import { filesFromZip } from '$lib/utils/live2d-model-files';
  import { createLive2DRenderer } from '$lib/utils/live2d-avatar-renderer';

  const source = await filesFromZip(await (await fetch('/model.zip')).blob());
  const canvas = document.createElement('canvas');
  document.body.appendChild(canvas);
  const r = await createLive2DRenderer(canvas, source, new AbortController().signal);

  console.info('declared:', r.capabilities.expressions);

  const input = { speaking: false, listening: false, level: 0, active: true };
  const run = (n) => { for (let i = 0; i < n; i++) r.render(33, {}, input, false); };

  run(120);              // let it settle
  r.setExpression('angry');
  run(120);              // fade-in is 1 s at 30 ms/frame
</script>
```

Screenshot before and after. If the face changes here but not in the app, the
fault is in the glue, not the model. If it does not change here either, compare
the declared names against the names the resolver produces.

Note that comparing screenshots by pixel count does not work on its own: the
idle animation (breath, hair physics) moves far more pixels between two
"neutral" frames than an expression does. Crop to the face and look at it.

## If line 2 shows a valid name but the face still does not move

Then the failure is inside the renderer, and there are two candidates:

1. **The expression file is missing from the archive.** `setExpression` returns
   `false` when the library cannot load the `.exp3.json`. The renderer retries
   three times and then gives up silently by design. Check that the archive
   contains the files listed in `FileReferences.Expressions`.
2. **`prefers-reduced-motion` is on.** `setExpression` returns `false`
   immediately when the OS asks for reduced motion, matching how gestures are
   handled elsewhere. Check
   `window.matchMedia('(prefers-reduced-motion: reduce)').matches`.

## Manual check without the app

Unzip the model and look at the `.model3.json`:

```json
"FileReferences": {
  "Expressions": [
    { "Name": "happy", "File": "exp/happy.exp3.json" }
  ]
}
```

The resolver looks for these words inside the `Name`: `happy`, `sad`, `angry`,
`surprised`, `relaxed`. Matching is exact, then case-insensitive, then
shortest-substring. `exp_happy` and `happy_01` both match `happy`; `F01` does not.

## Why the mapping is coarse on purpose

The signal is a single emoji produced by one LLM call. Five buckets is already
the most that can be recovered from it. Anything finer would be invented
precision — and a wrong expression is worse than no expression.

Two emoji are deliberately left **unmapped** even though they are common:

- 😂 🤣 — full laughter. The rig has no open-mouth laugh pose; mapping them would
  put a polite closed smile on a laughing message, which reads worse than no
  change. Pinned by a unit test.
- 😷 🤒 🤕 🤢 🤐 — illness and injury carry no facial pose this rig can draw.

😅 is mapped to `happy` because it is a nervous *smile*, which the rig can draw.

## Editing model params: what actually works

The prompt is stored per model in `model.params.system`. Three traps cost real
time here and are worth writing down.

**There is a timer that rewrites every prompt every 10 minutes.** This is the one
that made every previous fix look like it had failed. `model-healthcheck.timer`
runs `~/.local/bin/model-healthcheck.sh`, which used to end with:

```python
if p.get("system") != sysprompt or p.get("temperature") != temp:
    p["system"] = sysprompt          # hardcoded Indonesian default
    p["temperature"] = temp
    c.execute("update model set params=? where id=?", (json.dumps(p), mid))
```

So any edit to any model's prompt was silently reverted within ten minutes, and
`model_history` grew a new entry with `commit_message = NULL` on every run. That
is why an English translation and two avatar-prompt installs all appeared to do
nothing. The block now only **seeds an empty prompt** and never overwrites an
existing one; verify with:

```bash
systemctl --user start model-healthcheck.service
podman exec open-webui python /tmp/dump-prompts.py   # must still show your text
```

**The prompt lives in two places, and they must agree.** The database holds the
live prompt per model; `DEFAULT_SYSTEM` inside that same script holds the text
used to seed models added later. Change only the database and a model added
tomorrow gets the old text. Both are now driven from one file,
`~/avatar-prompt/system-prompt-final-en.txt`, by
`~/avatar-prompt/restore-avatar-prompt.sh`, which is idempotent and also re-runs
the timer to prove the result survives.

If a prompt change "does not stick", check this timer before doubting the API.

**Write through the API, never SQL.** `POST /api/v1/models/model/update` keeps
`model_history` and `version_id` consistent. A direct `UPDATE` leaves the live row
pointing at a snapshot that no longer describes it.

**A write can succeed and still look like it failed.** After an update, verify
against the live DB row, not the API echo — the endpoint returns the value it just
wrote, which is not proof it persisted:

```python
# inside the container
params = json.loads(db.execute("select params from model where id=?", (mid,)).fetchone()[0])
assert "[[AVATAR-BEGIN]]" in params["system"]
```

If the live row and its own `version_id` snapshot disagree, the write did not land
even though the response said 200.

**Never send `is_active`.** `_write_model` preserves the current value when the
field is absent, but sending the default `True` silently re-enables models an
admin had disabled. Four of the eight models here are inactive; getting this wrong
would have published them.

**Refresh the cache.** `models.base_models_cache = true` with no Redis means
`app.state.MODELS` is in-process. A model update does **not** invalidate it;
`GET /api/models?refresh=true` does. Without that call the change appears to do
nothing until the next restart.

**Keep the prompt paragraph short.** A long, heavily-worded draft was ignored by
the model while a short direct one worked on the same question.
