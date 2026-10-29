# Whisper language retry: what it fixes, and what it costs

Two separate things were wrong with voice transcription on this deployment, and
they needed different fixes.

1. **English speech came back as Indonesian text.** Fixed per-user by pinning
   Settings → Audio → STT Language = `en`. That is the primary fix and it is
   live for the affected account.
2. **Auto-detection picks the wrong language on short clips.** This is what the
   retry addresses, as a safety net for users who have not pinned a language.

## Why detection fails here

Whisper judges language from the first 30 seconds of audio, then locks that
decision for the whole clip. A voice turn on this deployment is 2.7–4.0 s, so the
decision is made on very little signal. Both directions of error were observed in
production cache:

| Audio | Detected | Decoded as |
|---|---|---|
| English "Can we just chat for a bit?" | `id`, p=0.950 | "Bisa kita berbual flora bit?" |
| Indonesian "Halo, apa kabar hari ini?" | `en` | "Hello, how are you today?" |

`language_probability` is **not** a usable trigger: the worst English clip
reported p=0.950 while being confidently wrong.

## The rule

Trigger on `avg_logprob` (mean per-token log probability of the decode), not on
detection probability. Decodes that agree with the audio measured −0.19 to −0.33;
decodes fighting it measured −0.48 to −1.11.

```
if requested is None and avg_logprob < WHISPER_LANGUAGE_RETRY_LOGPROB:
    for lang in WHISPER_LANGUAGE_FALLBACK:
        if lang == detected: continue          # would reproduce the same decode
        decode(lang); keep if strictly more confident
```

Knobs are env vars, both inert by default. Live values are in "Decision" below.

A pinned language never triggers the retry (`requested is None` is false), so the
per-user fix and the safety net cannot collide.

## Measured on 68 production clips

68 cached clips from the live instance, `vad_filter=True` (production uses VAD,
and VAD changes detection: one clip detects `en` without it and `id` with it).

**16 of 68 produce no text at all** — VAD discards the whole clip, `avg_logprob`
is `None`, and the retry never fires. They are short utterances: "Terima kasih",
"Terima kasih kerana menonton". This is a real limitation, not a regression.

Auto-detection across the 68: `en` 45, `id` 20, `fr` 2, `nn` 1.

### Direction 1 — auto said `id`, retry tries `en`

22 clips actionable. **14 replaced, 0 proven harmful.**

| Clip | auto | en | Δ | Result |
|---|---|---|---|---|
| `744a086d` | −0.933 | −0.257 | +0.68 | "Bisa kita berbual flora bit?" → "Can we just chat for a bit?" |
| `36be9c98` | −0.479 | −0.286 | +0.19 | "Halo, apa kabar hari ini?" → "Hello, how are you today?" |
| `35ebb84d` | −0.758 | −0.370 | +0.39 | "Bagaimana saatnya saya memotifkan…" → "How do I stay motivated…" |
| `20aa92bc` | −1.112 | −0.678 | +0.43 | Indonesian garble → coherent English |

This direction is solid. Note `36be9c98` is English audio — proven by decoding it
without VAD, where it detects `en` — so "Hello, how are you today?" is the correct
transcript, not a regression.

### Direction 2 — auto said `en`, retry tries `id`

18 clips actionable. **6 "rescued", at least 2 of them harmful.**

| Clip | en | id | Verdict | Text chosen |
|---|---|---|---|---|
| `0c9d797e` | −0.923 | −0.832 | rescue | "Hello." → **"Halo."** — audio is English |
| `e18e9850` | −0.689 | −0.595 | rescue | "What's your name?" → **"but your name"** |
| `2812b226` | −0.819 | −0.794 | rescue | "Picking elis" (marginal) |
| `1ca40067` | −0.817 | −0.529 | rescue | "Thank you." → "Terima kasih." |
| `9b9fe2be` | −0.829 | −0.572 | rescue | "Hello." → "Halo" |
| `9ec3e06e` | −0.853 | −0.791 | rescue | "Sorry you" → "Sorry" |

**The earlier claim that the retry "can only add latency, never produce wrong
text" was wrong.** The rule keeps the more confident decode, and a forced `id`
decode of English audio can be *more* confident while producing worse text.
Confidence is not correctness.

Four of the six direction-2 "rescues" are short "Halo"/"Terima kasih"/"Sorry"
clips — the same category whose VAD comes up empty elsewhere. Those decodes are
unstable, and the chosen text only guesses at the language rather than adding
information.

## Decision

The data below was presented, and the deployment first ran `en,id`. It was then
narrowed to **`en` only**, because direction 2 has two proven regressions and
direction 1 has none.

Rationale for narrowing:

- Direction 1 replaces 14 clips with no proven harm. Direction 2 "rescues" 6, of
  which at least 2 are worse than what they replaced (`Hello.` → `Halo.`,
  `What's your name?` → `but your name`), and 4 of the 6 are short unstable
  "Halo"/"Terima kasih"/"Sorry" clips.
- The cost is explicit and accepted: Indonesian speech that auto-detects as
  English is no longer corrected. That is the majority detection outcome (45 of
  68 clips detect `en`), so this is a real gap, not a rounding error.

The narrow setting is also the one that cannot make a good English transcript
worse, which matters because English is what the affected users actually speak.

| Var | Live value |
|---|---|
| `WHISPER_LANGUAGE_FALLBACK` | `en` |
| `WHISPER_LANGUAGE_RETRY_LOGPROB` | `-0.4` |

To restore the two-direction behaviour, no rebuild is needed:

```
~/enable-langretry.sh on en,id -0.4
```

To disable the retry entirely:

```
~/enable-langretry.sh off
```

## Threshold

`-0.4` is close to optimal but not exactly so. Firing counts by threshold over
the 22 direction-1 clips:

| Threshold | Fires | Replaced |
|---|---|---|
| −0.30 | 21/22 | 14 |
| **−0.40** | **17/22** | **14** |
| −0.45 | 16/22 | 13 |
| −0.50 | 13/22 | 10 |
| −0.90 | 6/22 | 0 |

From −0.30 to −0.40 the replacements stay at 14 while fires drop by 4: those four
cost a wasted decode and changed nothing. −0.45 loses one replacement. So −0.4
buys the same result with fewer decodes than a looser threshold, and −0.45 saves
one more decode at the cost of one fix.

## Caveats that must travel with these numbers

- `prod_text` in the corpus is **not ground truth**. It mixes genuine Indonesian
  with English mistranscribed into Indonesian. "14 replaced" means "14 became
  more confident and more coherent", not "14 proven correct".
- **The 68 clips are not 68 independent samples.** Several are the same recording
  under different ids: `744a086d` / `07bb66e5` / `9eaa6ae5` are one clip, and
  `36be9c98` / `0d7020b9` / `a8037a9f` are another.
- All measurements use `vad_filter=True` because production does. VAD changes the
  detection outcome.
- Model `medium` was evaluated and rejected: 4/5 detection versus `small` 3/5,
  still failed the worst clip at p=0.945, 25–28 s per 3 s clip (5×), 2183 MB peak
  RSS. The live unit caps the container at 3 GB.

## Files

| File | Role |
|---|---|
| `backend/open_webui/routers/audio/__init__.py` | `_decode_whisper()`, candidate loop in `_transcribe_whisper` |
| `backend/open_webui/config.py` | `WHISPER_LANGUAGE_FALLBACK`, `WHISPER_LANGUAGE_RETRY_LOGPROB` |
| `~/enable-langretry.sh` | turn the retry on/off, with unit backup and runtime verification |
| `~/deploy-owui.sh` | deploy + feature-marker verification |
