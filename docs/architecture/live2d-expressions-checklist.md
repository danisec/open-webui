# How to check whether your Live2D model can change expression

Paste this in the browser console **while voice mode is open and the avatar is
loaded**. It answers two questions: does the model declare any expressions, and
are they being applied.

## 1. Does the model declare expressions?

The reliable way is to unzip the model on your computer and open the
`.model3.json` inside it. Look for:

```json
"FileReferences": {
  "Expressions": [ { "Name": "happy", "File": "exp/happy.exp3.json" } ]
}
```

- **No `Expressions` key** → this feature cannot do anything for your model. The
  code is live, but there is nothing to trigger. This is a property of the model,
  not a bug — and it can be fixed by authoring expressions for it, see
  "Authoring expressions for a rig that has none" in `live2d-expressions.md`.
- **`Expressions` present** → check whether any `Name` contains one of the mood
  words the resolver looks for: `happy`, `sad`, `angry`, `surprised`, `relaxed`.
  Names like `F01`/`F02` will not match on purpose — guessing which integer means
  "happy" would be wrong more often than right.

You do not have to unzip anything to get the same answer: the app logs it at
load time. Open the console during a voice call and read

```
[live2d] expressions declared by model: 5 [happy, sad, angry, surprised, relaxed]
```

A count of `0` there means the same thing as a missing `Expressions` key.

## 0. Before anything else: does the reply itself admit to having a face?

This is the failure that looks most like a code bug and is not one. Ask the
character something emotional and read what it says. If the reply contains
"I don't have a face", "I'm just a voice", or "text-based assistant", then the
emoji task is being asked to read the emotion of a sentence that has none, and it
correctly answers with a flat emoji. The face stays flat because the signal is
flat.

The check is one line in the console — the second log line shows the emoji:

```
[live2d] mood: emoji="😐" mood=relaxed name="relaxed" of [...]
```

An emoji of 😐 / 😏 / 😑 next to a reply that *denies having a face* is the
signature of this problem. The fix is the system prompt: the model needs to be
told it is displayed as a character with a visible face. See
`/home/dani/install-avatar-prompt.py`, which installs that paragraph on every
chat model.

## 2. Are expressions being applied at runtime?

With voice mode open, watch the console during a reply. The expression is applied
per spoken chunk, so a mood change should appear as the avatar starts a new
sentence.

To see the model's declared names directly:

```js
// Every Live2D model exposes its expression definitions here.
const canvas = document.querySelector('.live2d-canvas');
console.log('canvas present:', !!canvas);
```

The authoritative check is visual: ask something clearly happy ("tell me a joke")
and then something clearly sad ("I lost my job today"), and watch whether the face
changes between them.

## What "no visible change" means

In order of likelihood:

1. **The model has no expressions.** Most common by far. Check step 1. If the rig
   has facial parameters but no `Expressions` block, the model file can be fixed —
   see "Authoring expressions for a rig that has none" in `live2d-expressions.md`.
2. **The expression names do not contain a mood word.** `F01`/`F02`-style rigs
   are never matched.
3. **The emoji task returned an unmapped emoji.** The mapping covers common chat
   emoji; `🍕` yields no mood on purpose.
4. **The reply was too short to have a mood.** A one-word answer may get a neutral
   or absent emoji.
5. **The face only reacts after the first few sentences.** That was a real bug
   through v24 and is fixed in v25: a mood arriving while the model was still
   loading used to be discarded. On v25 the console shows
   `[live2d] mood held until the model is ready: emoji="😊"` and then replays it.
   If you still see a delay on v25, the model is simply slow to unpack.

None of these throw an error; the face simply stays as it was.

## Reading the console during a call

Two lines are the whole diagnosis, in order:

```
[live2d] expressions declared by model: 5 [happy, sad, angry, surprised, relaxed]
[live2d] mood: emoji="😊" mood=happy name="happy" of [happy, sad, angry, ...]
```

- First line `0` → the model declares nothing. Nothing in the app can help.
- First line above `0`, second line `mood=null` → the emoji is not in the table.
- First line above `0`, second line `name=null` → the rig's names do not contain
  the mood word.
- Both fine and the face still does not move → the failure is below this layer;
  run the renderer-only snippet in `live2d-expression-diagnosis.md`.
