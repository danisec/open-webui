# Voice mode: conversation loop

Open WebUI's voice mode (the classic `callMode = 'current'` path, not the
realtime bridge) is a four-stage loop per turn:

```
mic → VAD → MediaRecorder → STT (Whisper) → LLM (DeepSeek) → TTS (Supertonic-3) → speaker
```

Every stage was measured, and the frontend stages were rebuilt in image
`v0.12.0-voice-vad-arm64-v14`. This document records what was wrong, what
changed, and how to tune it.

## Symptoms before v14

| Symptom | Root cause |
|---|---|
| Turn never ended in a noisy room | Speech was detected as `domainData.some(v => v > 0)` with the analyser floor at `-55 dB`. Room tone, fans and laptop hum all sit above that floor, so the "last sound" timestamp was refreshed on every frame. |
| Turn never started in a quiet room | Same test in the other direction: a quiet room never pushed a frequency bin above zero, so `hasStartedSpeaking` never became true. |
| Voice mode died after a few turns | A new `AudioContext` was created per turn and never closed. Browsers stop granting contexts after a handful, after which detection failed silently. |
| Every turn added another detection loop | The loop guard was `if (!mediaRecorder) return`, but `mediaRecorder` was reassigned in the same synchronous block that stopped it, so a pending frame never observed it as falsy. Each turn left a loop behind, calling `start()`/`stop()` on whatever recorder was current. |
| Microphone stayed dead after one LLM error | `chatCompletionEventHandler` returned early on `message.error` without dispatching `chat:finish`, so `assistantSpeaking` stayed `true` and the mic stayed gated for the rest of the call. |
| Short answers were silent until the end | The TTS chunk threshold was 4 words / 50 characters. A spoken answer like "Ya, tentu." only cleared that at the very end of streaming. |
| 300 ms of dead air between sentences | A 200 ms sleep after each played chunk plus a 100 ms sleep in `playAudio`'s `finish` handler. |
| Phantom turns | A turn in progress was not discarded when the microphone was gated, so a stale silence timer could fire during assistant playback and submit a silent blob. |

## What v14 does

### Detection (A, B, I)

`startVadLoop()` owns one `AudioContext`, one `AnalyserNode` and one
`requestAnimationFrame` loop for the entire call. `stopVadLoop()` cancels the
frame, disconnects the source, closes the context and clears state; it runs
from `stopAudioStream()` and from the teardown path in `stopRecordingCallback`.

Each frame converts RMS to dBFS and compares against a real threshold:

```
rms = sqrt(mean(((sample - 128) / 128)^2))
db  = rms > 0 ? 20 * log10(rms) : -100
```

A turn needs three things to be submitted:

| Guard | Default | Meaning |
|---|---|---|
| `thresholdDb` | `-45` dBFS | louder than this counts as voice |
| `minSpeechMs` | `300` | voice must be sustained; a click is not a sentence |
| `silenceMs` | `1200` | quiet after which the turn is submitted |
| `maxUtteranceMs` | `30000` | hard cap, so a noisy room cannot record forever |

`MediaRecorder` starts on the first loud frame (so the onset is not clipped)
and is stopped either by the silence timer, by the cap, or by the
short-burst discard (250 ms of voice that never reached `minSpeechMs`).

The recorder is created by `createRecorder()`, separate from
`startRecording()`, so a new turn reuses the existing context instead of
building a new one. Turn state (`hasStartedSpeaking`, `speechSince`,
`lastVoiceTime`, `turnStart`, `turnEnding`) is reset in
`stopRecordingCallback` before the next recorder exists.

While the mic is gated (`muted`, or `assistantSpeaking` without
`voiceInterruption`) the loop clears those timestamps and drops any turn in
progress. This is what removes the phantom turns.

### Error path (C)

`chat:finish` is dispatched on the `message.error` branch as well, so one
failed response no longer silences voice mode for the rest of the call.

### Speaking sooner (D, F)

`dispatchCallOverlayAudio` now holds back a fragment only while it is under
one word or under 12 characters, down from 4 words / 50 characters. The
200 ms and 100 ms delays are gone; prefetch already runs in parallel, so
removing them only removes dead air.

## Tuning

All four VAD values are read per frame from user settings, so they can be
changed without rebuilding:

```js
voiceThresholdDb     // -45
voiceMinSpeechMs     // 300
voiceSilenceMs       // 1200
voiceMaxUtteranceMs  // 30000
```

If turns start on background noise, raise `voiceThresholdDb` toward `-35`.
If short answers get cut off, raise `voiceSilenceMs` toward `1800`.

The types live in `src/lib/stores/index.ts` (`Settings`).

## Measured latency (before v14)

| Stage | Cold | Warm |
|---|---|---|
| STT (`whisper small` int8, 2 vCPU) | 7.1 s for 6 s audio; 11.1 s for 17.8 s audio | — |
| TTS Supertonic-3 | 2.44–4.14 s | 0.015 s (cache hit) |

First spoken response was 8–13 s end to end. v14 removes roughly 0.3 s per
sentence of artificial delay and lets the first sentence start earlier, but
STT dominates and is unchanged. Reducing it needs `beam_size=1` and
`WHISPER_VAD_FILTER=True` in the unit environment, or moving STT off the
2 vCPU container — both deliberately out of scope for v14.

## Rollback

```bash
# on the Batam host
cp ~/backups/open-webui.container.bak.<TS> ~/.config/containers/systemd/open-webui.container
systemctl --user daemon-reload
systemctl --user restart open-webui.service
```

The previous image is
`100.100.89.87:5000/open-webui-custom:v0.12.0-rotator-rr-arm64-v43`.

## Files

- `src/lib/components/chat/MessageInput/CallOverlay.svelte` — VAD, recorder lifecycle, playback.
- `src/lib/components/chat/Chat.svelte` — TTS chunk threshold, `chat:finish` on error.
- `src/lib/stores/index.ts` — `Settings` keys for VAD tuning.
