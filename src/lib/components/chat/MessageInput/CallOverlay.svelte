<script lang="ts">
	import { config, models, settings, showCallOverlay, TTSWorker } from '$lib/stores';
	import { onMount, tick, getContext, onDestroy, createEventDispatcher } from 'svelte';

	const dispatch = createEventDispatcher();

	import { blobToFile } from '$lib/utils';
	import { generateEmoji } from '$lib/apis';
	import { synthesizeOpenAISpeech, transcribeAudio } from '$lib/apis/audio';

	import { toast } from 'svelte-sonner';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import VideoInputMenu from './CallOverlay/VideoInputMenu.svelte';
	import VoiceAvatarLive2D from './CallOverlay/VoiceAvatarLive2D.svelte';
	import { KokoroWorker } from '$lib/workers/KokoroWorker';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import type { RealtimeCall } from '$lib/utils/realtime';
	import { checkTextureBudget, filesFromZip, maxTextureSize } from '$lib/utils/live2d-model-files';
	import type { Live2DModelSource } from '$lib/utils/live2d-model-files';
	import { moodFromEmoji, resolveExpressionName } from '$lib/utils/live2d-expressions';
	import { clearStoredAvatar, loadStoredAvatar, storeAvatar } from '$lib/utils/avatar-storage';
	import { audioLevel, prepareAudioLevel } from '$lib/utils/audio-level';

	const i18n: any = getContext('i18n');

	export let eventTarget: EventTarget;
	export let submitPrompt: Function;
	export let stopResponse: Function;
	export let files;
	export let chatId;
	export let modelId;
	export let bridge: RealtimeCall | undefined = undefined;

	let wakeLock = null;

	let model = null;

	let loading = false;
	let confirmed = false;
	let interrupted = false;
	let assistantSpeaking = false;
	let muted = false;

	let emoji = null;
	let camera = false;
	let cameraStream = null;

	let chatStreaming = false;
	let rmsLevel = 0;
	let hasStartedSpeaking = false;
	let mediaRecorder;
	let audioStream = null;
	let audioChunks = [];
	let destroyed = false;

	let videoInputDevices = [];
	let selectedVideoInputDeviceId = null;

	// ── Locally loaded Live2D avatar ─────────────────────────────────────
	// The model is chosen by the user, unpacked in the browser and stored in
	// IndexedDB. It is never uploaded, so the server stores nothing and the
	// model's redistribution terms are never engaged.
	let avatarModel: Live2DModelSource | null = null;
	let avatarError = '';
	let avatarBusy = false;
	let avatarName = '';
	/** Drives lip-sync; 0..1 measured from the shared audio element. */
	let speechLevel = 0;
	let avatarLevelFrame = 0;
	/** Handle to the Live2D component, used to push a mood-driven expression. */
	let avatarComponent: {
		setExpression?: (name: string | null) => boolean;
		getExpressions?: () => string[];
	} | null = null;
	/** Expression names the loaded model declares; empty when it declares none. */
	let avatarExpressions: string[] = [];
	/**
	 * Whether `on:ready` has fired for the current model.
	 *
	 * Needed because `bind:this` and the expression list become available at very
	 * different times: the handle is assigned on mount, while the list only
	 * arrives once the archive has been unpacked and the renderer has read the
	 * model's capabilities. Without this flag an empty list during load is
	 * indistinguishable from a rig that genuinely declares no expressions.
	 */
	let avatarReady = false;
	/** Last expression applied, so an unchanged mood is not re-applied every chunk. */
	let avatarExpression: string | null = null;
	/**
	 * The most recent emoji whose expression could not be applied because the
	 * model had not finished loading. The emoji task and the avatar load race each
	 * other: `fetchAudio` starts as soon as a chunk streams in, while the model is
	 * still being unpacked and rendered. Without holding the value, the first
	 * chunks of a call would silently lose their expression.
	 */
	let pendingMoodEmoji: string | null = null;

	/**
	 * Push the mood carried by an emoji onto the avatar's face.
	 *
	 * The emoji task already asks an LLM to read the emotion of the reply, so this
	 * reuses that signal rather than adding another call.
	 *
	 * Three cases end with the face unchanged, and only one of them is an error:
	 *
	 *   - the mood arrives before the rig has loaded -> held and replayed, so the
	 *     expression is merely late;
	 *   - the rig declares no expressions at all -> genuinely nothing to do;
	 *   - the emoji maps to no known mood -> genuinely nothing to do.
	 *
	 * The expression is keyed to the reply chunk that is currently being spoken,
	 * so the face follows the sentence being heard rather than the whole message.
	 */
	const applyMoodExpression = (emojiForChunk: string | null) => {
		if (!avatarModel) return;
		if (!avatarComponent) {
			// The renderer is not up yet; `on:ready` replays this once it is.
			pendingMoodEmoji = emojiForChunk;
			return;
		}
		// `bind:this` is assigned as soon as the component mounts, which is long
		// before the renderer finishes unpacking a model and reports its
		// capabilities. So "component present, expression list empty" does NOT mean
		// the rig has no expressions -- it usually means `on:ready` has not fired
		// yet. Treating it as final threw away every mood that arrived during the
		// load, which for a 40 MB rig is the opening sentences of the call, the
		// part the user is actually watching.
		//
		// `avatarReady` is what distinguishes the two cases; only once it is set is
		// an empty list genuinely "this rig declares nothing".
		if (!avatarReady) {
			pendingMoodEmoji = emojiForChunk;
			// Worth a line of its own: this is the state that used to be mistaken
			// for "the rig has no expressions", and it is the difference between a
			// face that reacts late and one that never reacts.
			console.info(
				`[live2d] mood held until the model is ready: emoji=${JSON.stringify(emojiForChunk)}`
			);
			return;
		}
		// The renderer is up and the rig really declares no expressions. Nothing
		// can ever be applied, so do not hold the value.
		if (!avatarExpressions.length) return;
		pendingMoodEmoji = null;

		const mood = moodFromEmoji(emojiForChunk);
		const name = mood ? resolveExpressionName(mood, avatarExpressions) : null;

		// `null` clears back to the model's default face when the mood is gone,
		// which is what keeps a happy reply from leaving a smile latched on.
		if (name === avatarExpression) return;
		avatarExpression = name;

		// Each step can fail silently by design -- an unmapped emoji, a mood the rig
		// does not name, a name that does not resolve -- and from the UI all three
		// look like "nothing happened". Logging the intermediate values is what makes
		// a report of "the face never changes" diagnosable without guessing.
		console.info(
			`[live2d] mood: emoji=${JSON.stringify(emojiForChunk)} mood=${mood} ` +
				`name=${JSON.stringify(name)} of [${avatarExpressions.join(', ')}]`
		);

		avatarComponent?.setExpression?.(name);
	};

	const stopAvatarLevel = () => {
		if (avatarLevelFrame) {
			cancelAnimationFrame(avatarLevelFrame);
			avatarLevelFrame = 0;
		}
		speechLevel = 0;
	};

	const startAvatarLevel = async () => {
		if (avatarLevelFrame) return;
		const element = document.getElementById('audioElement') as HTMLAudioElement | null;
		if (!element) return;
		// The graph is only built once the audio context is actually running;
		// routing the element into a suspended context would mute the assistant.
		if (!(await prepareAudioLevel(element))) {
			console.warn('[avatar] lip-sync off: audio graph unavailable (AudioContext not running)');
			return;
		}
		if (!assistantSpeaking) return;

		const tickLevel = () => {
			speechLevel = audioLevel(element, assistantSpeaking);
			avatarLevelFrame = requestAnimationFrame(tickLevel);
		};
		tickLevel();
	};

	// The analyser only needs to run while audio is playing. A permanent 60 Hz
	// loop would keep the audio graph awake for nothing.
	$: if (avatarModel) {
		if (assistantSpeaking) void startAvatarLevel();
		else stopAvatarLevel();
	}

	const adoptAvatar = async (file: File, remember: boolean) => {
		avatarBusy = true;
		avatarError = '';
		try {
			const unpacked = await filesFromZip(file, file.name);
			try {
				checkTextureBudget(unpacked.textureEdge, maxTextureSize());
			} catch (error) {
				// The archive is fine but this device cannot hold its textures;
				// release the object URLs rather than leaking them.
				unpacked.dispose();
				throw error;
			}

			const previous = avatarModel;
			avatarModel = unpacked;
			avatarName = file.name;
			// A different model may declare a different set of expressions, so the
			// cached list and the applied name are dropped rather than carried over.
			// `on:ready` repopulates them once the new model has loaded, which is
			// also when `avatarReady` is set again.
			avatarExpressions = [];
			avatarReady = false;
			avatarExpression = null;
			// Object URLs belong to the source, so the one being replaced is
			// released here and not by the renderer.
			previous?.dispose();

			if (remember && !(await storeAvatar(file))) {
				// Storage is a convenience; the model still works for this call.
				console.info('Avatar could not be stored locally; it will need picking again.');
			}
		} catch (error) {
			// Keep whatever was already loaded; only report the new failure.
			avatarError = error instanceof Error ? error.message : 'Could not read this model.';
		} finally {
			avatarBusy = false;
		}
	};

	const pickAvatar = async (event: Event) => {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		// Allow picking the same file again after a failure.
		input.value = '';
		if (file) await adoptAvatar(file, true);
	};

	const forgetAvatar = async () => {
		avatarModel?.dispose();
		avatarModel = null;
		avatarName = '';
		avatarExpressions = [];
		avatarReady = false;
		avatarExpression = null;
		pendingMoodEmoji = null;
		avatarError = '';
		stopAvatarLevel();
		await clearStoredAvatar();
	};

	$: if (bridge) {
		assistantSpeaking = bridge.speaking;
		muted = bridge.muted;
		loading = bridge.connecting || (bridge.working && !bridge.speaking);
		model = $models.find((m) => m.id === modelId);
	}

	const getVideoInputDevices = async () => {
		const devices = await navigator.mediaDevices.enumerateDevices();
		videoInputDevices = devices.filter((device) => device.kind === 'videoinput');

		if (!!navigator.mediaDevices.getDisplayMedia) {
			videoInputDevices = [
				...videoInputDevices,
				{
					deviceId: 'screen',
					label: ''
				}
			];
		}

		console.log(videoInputDevices);
		if (selectedVideoInputDeviceId === null && videoInputDevices.length > 0) {
			const savedDeviceId = localStorage.getItem('selectedVideoInputDeviceId');
			if (savedDeviceId && videoInputDevices.some((d) => d.deviceId === savedDeviceId)) {
				selectedVideoInputDeviceId = savedDeviceId;
			} else {
				selectedVideoInputDeviceId = videoInputDevices[0].deviceId;
			}
		}
	};

	const startCamera = async () => {
		await getVideoInputDevices();

		if (cameraStream === null) {
			camera = true;
			await tick();
			try {
				await startVideoStream();
			} catch (err) {
				console.error('Error accessing webcam: ', err);
			}
		}
	};

	const startVideoStream = async () => {
		const video = document.getElementById('camera-feed');
		if (video) {
			if (selectedVideoInputDeviceId === 'screen') {
				cameraStream = await navigator.mediaDevices.getDisplayMedia({
					video: {
						cursor: 'always'
					},
					audio: false
				});
			} else {
				cameraStream = await navigator.mediaDevices.getUserMedia({
					video: {
						deviceId: selectedVideoInputDeviceId ? { exact: selectedVideoInputDeviceId } : undefined
					}
				});
			}

			if (cameraStream) {
				await getVideoInputDevices();
				video.srcObject = cameraStream;
				await video.play();
			}
		}
	};

	const stopVideoStream = async () => {
		if (cameraStream) {
			const tracks = cameraStream.getTracks();
			tracks.forEach((track) => track.stop());
		}

		cameraStream = null;
	};

	const takeScreenshot = () => {
		const video = document.getElementById('camera-feed');
		const canvas = document.getElementById('camera-canvas');

		if (!canvas) {
			return;
		}

		const context = canvas.getContext('2d');

		// Make the canvas match the video dimensions
		canvas.width = video.videoWidth;
		canvas.height = video.videoHeight;

		// Draw the image from the video onto the canvas
		context.drawImage(video, 0, 0, video.videoWidth, video.videoHeight);

		// Convert the canvas to a data base64 URL and console log it
		const dataURL = canvas.toDataURL('image/png');
		console.log(dataURL);

		return dataURL;
	};

	const stopCamera = async () => {
		await stopVideoStream();
		camera = false;
	};

	// ── Voice activity detection ──────────────────────────────────────────
	// Detection used to be `domainData.some((value) => value > 0)` with the
	// analyser floor at -55 dB. Room tone, fans and laptop hum all sit above
	// that floor, so a noisy room refreshed the "last sound" timestamp
	// forever and the turn never ended, while a quiet room never triggered at
	// all. Detection now compares the RMS level in dBFS against a real
	// threshold, and a turn needs both a minimum (a click is not a sentence)
	// and a maximum (a noisy room cannot record forever).
	const VISUALIZER_BUFFER_LENGTH = 300;

	const DEFAULT_VAD = {
		thresholdDb: -45, // dBFS; speech must be louder than this
		minSpeechMs: 300, // sustained voice before a turn counts as speech
		silenceMs: 1200, // quiet after which the turn is submitted
		maxUtteranceMs: 30000 // hard cap on a single turn
	};

	// Read per frame so tuning takes effect without restarting the call.
	const vadConfig = () => ({
		thresholdDb: $settings?.voiceThresholdDb ?? DEFAULT_VAD.thresholdDb,
		minSpeechMs: $settings?.voiceMinSpeechMs ?? DEFAULT_VAD.minSpeechMs,
		silenceMs: $settings?.voiceSilenceMs ?? DEFAULT_VAD.silenceMs,
		maxUtteranceMs: $settings?.voiceMaxUtteranceMs ?? DEFAULT_VAD.maxUtteranceMs
	});

	// One analyser and one frame loop per call, not per turn.
	let audioContext = null;
	let vadSource = null;
	let vadAnalyser = null;
	let timeDomainData = null;
	let vadFrame = null;
	let vadRunning = false;

	// Turn state, reset by stopRecordingCallback.
	let speechSince = 0;
	let lastVoiceTime = 0;
	let turnStart = 0;
	let turnEnding = false;

	const transcribeHandler = async (audioBlob) => {
		// Create a blob from the audio chunks
		if (!audioBlob || audioBlob.size < 100) {
			console.log('Audio blob too small or empty, skipping transcription');
			return;
		}

		await tick();
		const file = blobToFile(audioBlob, 'recording.wav');

		const res = await transcribeAudio(
			localStorage.token,
			file,
			$settings?.audio?.stt?.language
		).catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			console.log(res.text);

			if (res.text !== '') {
				const _responses = await submitPrompt(res.text, { _raw: true });
				console.log(_responses);
			}
		}
	};

	const stopRecordingCallback = async (_continue = true) => {
		// $showCallOverlay stays true when the chat page unmounts
		if ($showCallOverlay && !destroyed) {
			// deep copy the audioChunks array
			const _audioChunks = audioChunks.slice(0);

			audioChunks = [];
			mediaRecorder = false;

			// Reset the turn state before the next recorder exists. Leaving it
			// behind made the detector end the following turn immediately,
			// because its timestamps still pointed at the previous utterance.
			hasStartedSpeaking = false;
			speechSince = 0;
			lastVoiceTime = 0;
			turnStart = 0;
			turnEnding = false;

			if (_continue) {
				createRecorder();
			}

			if (confirmed) {
				loading = true;
				emoji = null;

				if (cameraStream) {
					const imageUrl = takeScreenshot();

					files = [
						{
							type: 'image',
							url: imageUrl
						}
					];
				}

				const audioBlob = new Blob(_audioChunks, { type: 'audio/wav' });
				await transcribeHandler(audioBlob);

				confirmed = false;
				loading = false;
			}
		} else {
			audioChunks = [];
			mediaRecorder = false;
			hasStartedSpeaking = false;
			confirmed = false;
			turnEnding = false;

			stopVadLoop();

			if (audioStream) {
				const tracks = audioStream.getTracks();
				tracks.forEach((track) => track.stop());
			}
			audioStream = null;
		}
	};

	const startRecording = async () => {
		if (!$showCallOverlay || destroyed) return;

		if (!audioStream) {
			audioStream = await navigator.mediaDevices.getUserMedia({
				audio: {
					echoCancellation: true,
					noiseSuppression: true,
					autoGainControl: true
				}
			});
		}

		if (!audioStream) return;

		// One AudioContext and one frame loop for the whole call. Creating them
		// per turn leaked a context every time a turn ended; browsers stop
		// granting new contexts after a handful, and detection then dies
		// silently. The old loop also never exited: `mediaRecorder` is
		// reassigned in the same synchronous block, so a pending frame never
		// observed it as falsy and every turn added another loop calling
		// start()/stop() on the current recorder.
		if (!audioContext) {
			audioContext = new AudioContext();
			vadSource = audioContext.createMediaStreamSource(audioStream);
			vadAnalyser = audioContext.createAnalyser();
			vadAnalyser.fftSize = 2048;
			vadSource.connect(vadAnalyser);
			timeDomainData = new Uint8Array(vadAnalyser.fftSize);
		}

		startVadLoop();
		createRecorder();
	};

	const createRecorder = () => {
		if (!audioStream || destroyed) return;
		if (mediaRecorder && mediaRecorder.state !== 'inactive') return;

		mediaRecorder = new MediaRecorder(audioStream);

		mediaRecorder.onstart = () => {
			audioChunks = [];
		};

		mediaRecorder.ondataavailable = (event) => {
			if (hasStartedSpeaking) {
				audioChunks.push(event.data);
			}
		};

		mediaRecorder.onstop = () => {
			stopRecordingCallback();
		};
	};

	const stopAudioStream = async () => {
		stopVadLoop();

		try {
			if (mediaRecorder) {
				// Detach first: the handler would otherwise create a fresh
				// recorder while the call is being torn down.
				mediaRecorder.onstop = null;
				mediaRecorder.stop();
			}
		} catch (error) {
			console.log('Error stopping audio stream:', error);
		}

		mediaRecorder = false;

		if (!audioStream) return;

		audioStream.getAudioTracks().forEach(function (track) {
			track.stop();
		});

		audioStream = null;
	};

	// Function to calculate the RMS level from time domain data
	const calculateRMS = (data: Uint8Array) => {
		let sumSquares = 0;
		for (let i = 0; i < data.length; i++) {
			const normalizedValue = (data[i] - 128) / 128; // Normalize the data
			sumSquares += normalizedValue * normalizedValue;
		}
		return Math.sqrt(sumSquares / data.length);
	};

	const startVadLoop = () => {
		if (vadRunning) return;
		vadRunning = true;

		const frame = () => {
			if (!vadRunning || destroyed || !$showCallOverlay) {
				vadRunning = false;
				return;
			}

			const gated = muted || (assistantSpeaking && !($settings?.voiceInterruption ?? false));

			if (gated) {
				// Mic is intentionally closed. Drop any turn in progress and
				// clear the timestamps, otherwise the silence timer from the
				// previous utterance expires while the assistant is talking
				// and a silent blob is submitted as a user turn.
				rmsLevel = 0;
				speechSince = 0;
				lastVoiceTime = 0;

				if (hasStartedSpeaking) {
					hasStartedSpeaking = false;
					confirmed = false;
					turnEnding = true;

					if (mediaRecorder && mediaRecorder.state === 'recording') {
						mediaRecorder.stop();
					}
				}

				vadFrame = requestAnimationFrame(frame);
				return;
			}

			if (turnEnding) {
				vadFrame = requestAnimationFrame(frame);
				return;
			}

			vadAnalyser.getByteTimeDomainData(timeDomainData);

			const rms = calculateRMS(timeDomainData);
			const db = rms > 0 ? 20 * Math.log10(rms) : -100;
			const config = vadConfig();
			const now = performance.now();

			rmsLevel = rms;

			if (db > config.thresholdDb) {
				lastVoiceTime = now;

				if (!speechSince) {
					speechSince = now;

					// Capture from the first loud frame so the onset is not
					// clipped; the chunk guard still decides whether the turn
					// is worth transcribing.
					if (mediaRecorder && mediaRecorder.state === 'inactive') {
						mediaRecorder.start();
					}
				}

				if (!hasStartedSpeaking && now - speechSince >= config.minSpeechMs) {
					hasStartedSpeaking = true;
					turnStart = now;
					// The user is talking over the assistant: stop playback.
					stopAllAudio();
				}
			} else if (!hasStartedSpeaking && speechSince && now - lastVoiceTime > 250) {
				// A click or a door, not a sentence. Stop and discard.
				speechSince = 0;

				if (mediaRecorder && mediaRecorder.state === 'recording') {
					mediaRecorder.stop();
				}
			}

			if (hasStartedSpeaking) {
				const silentFor = now - lastVoiceTime;
				const turnFor = now - turnStart;

				if (silentFor >= config.silenceMs || turnFor >= config.maxUtteranceMs) {
					confirmed = true;
					turnEnding = true;

					if (mediaRecorder && mediaRecorder.state === 'recording') {
						mediaRecorder.stop();
					}
				}
			}

			vadFrame = requestAnimationFrame(frame);
		};

		vadFrame = requestAnimationFrame(frame);
	};

	const stopVadLoop = () => {
		vadRunning = false;

		if (vadFrame) {
			cancelAnimationFrame(vadFrame);
			vadFrame = null;
		}

		if (vadSource) {
			try {
				vadSource.disconnect();
			} catch (error) {
				console.log('Error disconnecting audio source:', error);
			}
			vadSource = null;
		}

		if (audioContext) {
			audioContext.close().catch((error) => console.log('Error closing AudioContext:', error));
			audioContext = null;
		}

		vadAnalyser = null;
		timeDomainData = null;
		rmsLevel = 0;
	};

	let finishedMessages = {};
	let failedMessages = {};
	let currentMessageId = null;
	let currentUtterance: SpeechSynthesisUtterance | null = null;

	// Get voice: model-specific > user settings > config default
	const getVoiceId = () => {
		// Check for model-specific TTS voice first
		if (model?.info?.meta?.tts?.voice) {
			return model.info.meta.tts.voice;
		}
		// Fall back to user settings or config default
		if ($settings?.audio?.tts?.defaultVoice === $config.audio.tts.voice) {
			return $settings?.audio?.tts?.voice ?? $config?.audio?.tts?.voice;
		}
		return $config?.audio?.tts?.voice;
	};

	const speakSpeechSynthesisHandler = (content) => {
		if ($showCallOverlay) {
			return new Promise((resolve) => {
				let voices = [];
				const getVoicesLoop = setInterval(async () => {
					voices = await speechSynthesis.getVoices();
					if (voices.length > 0) {
						clearInterval(getVoicesLoop);

						const voiceId = getVoiceId();
						const voice = voices?.filter((v) => v.voiceURI === voiceId)?.at(0) ?? undefined;

						currentUtterance = new SpeechSynthesisUtterance(content);
						currentUtterance.rate = $settings.audio?.tts?.playbackRate ?? 1;

						if (voice) {
							currentUtterance.voice = voice;
						}

						speechSynthesis.speak(currentUtterance);
						currentUtterance.onend = async (e) => {
							await new Promise((r) => setTimeout(r, 200));
							resolve(e);
						};
					}
				}, 100);
			});
		} else {
			return Promise.resolve();
		}
	};

	const playAudio = (audio: HTMLAudioElement) => {
		if ($showCallOverlay) {
			return new Promise((resolve) => {
				const audioElement = document.getElementById('audioElement') as HTMLAudioElement;

				if (!audioElement) {
					resolve(null);
					return;
				}

				let settled = false;
				const finish = (e: Event | Error | null = null) => {
					if (settled) {
						return;
					}

					settled = true;
					audioElement.onended = null;
					audioElement.onerror = null;
					audioElement.onpause = null;

					resolve(e);
				};

				audioElement.src = audio.src;
				// stopAllAudio mutes it; unmuting after play() outside a gesture makes WebKit pause it
				audioElement.muted = false;
				audioElement.playbackRate = $settings.audio?.tts?.playbackRate ?? 1;
				audioElement.onended = finish;
				audioElement.onerror = () => finish();
				audioElement.onpause = finish;

				audioElement.play().catch((error) => {
					console.error(error);
					finish(error);
				});
			});
		} else {
			return Promise.resolve();
		}
	};

	const stopAllAudio = async () => {
		if (bridge) return bridge.stopSpeaking();
		assistantSpeaking = false;
		interrupted = true;
		audioAbortController.abort();

		if (chatStreaming) {
			stopResponse();
		}

		if (currentUtterance) {
			speechSynthesis.cancel();
			currentUtterance = null;
		}

		const audioElement = document.getElementById('audioElement') as HTMLAudioElement;
		if (audioElement) {
			audioElement.muted = true;
			audioElement.pause();
			audioElement.currentTime = 0;
		}
	};

	let audioAbortController = new AbortController();

	// Audio cache map where key is the content and value is the Audio object.
	const audioCache = new Map();
	const emojiCache = new Map();

	const fetchAudio = async (content) => {
		const id = currentMessageId;

		if (!audioCache.has(content) && !failedMessages[id]) {
			try {
				// Generate the emoji when either consumer needs it: the overlay draws
				// it as a glyph when `showEmojiInCall` is on, and the Live2D avatar
				// uses it as the reply's emotional reading. Gating this on the glyph
				// setting alone meant a user who hid the emoji also lost every facial
				// expression, even though the expression is a separate feature.
				//
				// The gate tests `avatarModel`, not `avatarExpressions`: at the moment
				// the first chunk arrives the model is usually still loading, so its
				// expression list is empty even for a rig that has plenty. Requiring
				// the list here skipped the emoji task for exactly the early chunks.
				// A rig with no expressions costs one emoji call and then goes quiet,
				// which is a fair trade for not losing the first sentences of a call.
				const needsEmoji = ($settings?.showEmojiInCall ?? false) || avatarModel !== null;

				if (needsEmoji) {
					const emoji = await generateEmoji(localStorage.token, modelId, content, chatId).catch(
						(error) => {
							console.error(error);
							return null;
						}
					);
					if (emoji) {
						emojiCache.set(content, emoji);
						// Apply as soon as the emoji exists, not only when the chunk starts
						// playing. `fetchAudio` is fire-and-forget at its call site, so the
						// TTS audio can be ready before this task answers; relying on the
						// playback path alone meant a chunk whose emoji landed late never
						// got an expression at all.
						applyMoodExpression(emoji);
					}
				}

				if ($settings.audio?.tts?.engine === 'browser-kokoro') {
					const url = await $TTSWorker
						.generate({
							text: content,
							voice: getVoiceId()
						})
						.catch((error) => {
							console.error(error);
							toast.error(`${error}`);
						});

					if (url) {
						audioCache.set(content, new Audio(url));
					}
				} else if ($config.audio.tts.engine !== '') {
					const res = await synthesizeOpenAISpeech(localStorage.token, getVoiceId(), content).catch(
						(error) => {
							console.error(error);
							if (!failedMessages[id]) {
								failedMessages[id] = true;
								toast.error(`${error}`);
							}
							return null;
						}
					);

					if (res) {
						const blob = await res.blob();
						const blobUrl = URL.createObjectURL(blob);
						audioCache.set(content, new Audio(blobUrl));
					}
				} else {
					audioCache.set(content, true);
				}
			} catch (error) {
				console.error('Error synthesizing speech:', error);
			}

			if (!audioCache.has(content)) {
				failedMessages[id] = true;
			}
		}

		return audioCache.get(content);
	};

	let messages = {};

	const monitorAndPlayAudio = async (id, signal) => {
		while (!signal.aborted) {
			if (messages[id] && messages[id].length > 0) {
				// Retrieve the next content string from the queue
				const content = messages[id].shift(); // Dequeues the content for playing

				if (audioCache.has(content)) {
					// If content is available in the cache, play it

					// Set the emoji for the content if available
					if (($settings?.showEmojiInCall ?? false) && emojiCache.has(content)) {
						emoji = emojiCache.get(content);
					} else {
						emoji = null;
					}

					// Drive the avatar's face from the same emoji signal. This reads the
					// cache rather than the `emoji` variable above, because that variable
					// is only populated when the glyph is shown; the face should react
					// regardless of whether the glyph is drawn.
					//
					// No wait here: delaying playback to wait for the emoji task would
					// add exactly the latency this whole feature exists to avoid. The
					// expression is also applied at the point the emoji arrives (see
					// `fetchAudio`), so a chunk whose emoji lands late still gets a face.
					applyMoodExpression(emojiCache.get(content) ?? null);

					if ($config.audio.tts.engine !== '') {
						try {
							console.log(
								'%c%s',
								'color: red; font-size: 20px;',
								`Playing audio for content: ${content}`
							);

							const audio = audioCache.get(content);
							await playAudio(audio); // Here ensure that playAudio is indeed correct method to execute
							console.log(`Played audio for content: ${content}`);
						} catch (error) {
							console.error('Error playing audio:', error);
						}
					} else {
						await speakSpeechSynthesisHandler(content);
					}
				} else if (!failedMessages[id]) {
					// If not available in the cache, push it back to the queue and delay
					messages[id].unshift(content); // Re-queue the content at the start
					console.log(`Audio for "${content}" not yet available in the cache, re-queued...`);
					await new Promise((resolve) => setTimeout(resolve, 200)); // Wait before retrying to reduce tight loop
				}
			} else if (finishedMessages[id] && messages[id] && messages[id].length === 0) {
				// If the message is finished and there are no more messages to process, break the loop
				assistantSpeaking = false;
				break;
			} else {
				// No messages to process, sleep for a bit
				await new Promise((resolve) => setTimeout(resolve, 200));
			}
		}
		console.log(`Audio monitoring and playing stopped for message ID ${id}`);
	};

	const chatStartHandler = async (e) => {
		const { id } = e.detail;

		chatStreaming = true;

		if (currentMessageId !== id) {
			console.log(`Received chat start event for message ID ${id}`);

			currentMessageId = id;
			if (audioAbortController) {
				audioAbortController.abort();
			}
			audioAbortController = new AbortController();

			assistantSpeaking = true;
			// Start monitoring and playing audio for the message ID
			monitorAndPlayAudio(id, audioAbortController.signal);
		}
	};

	const chatEventHandler = async (e) => {
		const { id, content } = e.detail;
		// "id" here is message id
		// if "id" is not the same as "currentMessageId" then do not process
		// "content" here is a sentence from the assistant,
		// there will be many sentences for the same "id"

		if (currentMessageId === id) {
			console.log(`Received chat event for message ID ${id}: ${content}`);

			try {
				if (messages[id] === undefined) {
					messages[id] = [content];
				} else {
					messages[id].push(content);
				}

				console.log(content);

				fetchAudio(content);
			} catch (error) {
				console.error('Failed to fetch or play audio:', error);
			}
		}
	};

	const chatFinishHandler = async (e) => {
		const { id, content } = e.detail;
		// "content" here is the entire message from the assistant
		finishedMessages[id] = true;

		chatStreaming = false;
	};

	const toggleMute = () => {
		if (bridge) return bridge.mute();
		muted = !muted;
		if (muted && hasStartedSpeaking) {
			// Abort the ongoing recording so it doesn't accidentally send a partial sentence
			hasStartedSpeaking = false;
			confirmed = false;
			audioChunks = [];
			if (mediaRecorder && mediaRecorder.state === 'recording') {
				mediaRecorder.stop();
			}
		}
	};

	let wasAssistantSpeaking = false;
	$: if (!bridge) {
		if (assistantSpeaking && !wasAssistantSpeaking) {
			wasAssistantSpeaking = true;
		} else if (!assistantSpeaking && wasAssistantSpeaking) {
			wasAssistantSpeaking = false;
			// Auto unmute when AI finishes speaking
			if (muted) {
				muted = false;
			}
		}
	}

	const handleKeydown = (e: KeyboardEvent) => {
		// Only handle M key when not typing in an input/textarea
		if (e.key === 'm' || e.key === 'M') {
			const target = e.target as HTMLElement;
			if (
				target.tagName !== 'INPUT' &&
				target.tagName !== 'TEXTAREA' &&
				!target.isContentEditable
			) {
				e.preventDefault();
				toggleMute();
			}
		}
	};

	onMount(async () => {
		if (bridge) {
			document.addEventListener('keydown', handleKeydown);
			return;
		}
		const setWakeLock = async () => {
			try {
				wakeLock = await navigator.wakeLock.request('screen');
			} catch (err) {
				// The Wake Lock request has failed - usually system related, such as battery.
				console.log(err);
			}

			if (wakeLock) {
				// Add a listener to release the wake lock when the page is unloaded
				wakeLock.addEventListener('release', () => {
					// the wake lock has been released
					console.log('Wake Lock released');
				});
			}
		};

		if ('wakeLock' in navigator) {
			await setWakeLock();

			document.addEventListener('visibilitychange', async () => {
				// Re-request the wake lock if the document becomes visible
				if (wakeLock !== null && document.visibilityState === 'visible') {
					await setWakeLock();
				}
			});
		}

		model = $models.find((m) => m.id === modelId);

		// Restore a previously picked avatar. This is local-only: no request is
		// made, and a failure (private browsing, cleared storage) is silent
		// because the user can simply pick the file again.
		void (async () => {
			const stored = await loadStoredAvatar();
			if (stored && !destroyed) await adoptAvatar(stored, false);
		})();

		if ($settings.audio?.tts?.engine === 'browser-kokoro' && !$TTSWorker) {
			try {
				await TTSWorker.set(
					new KokoroWorker({
						dtype: $settings.audio?.tts?.engineConfig?.dtype ?? 'fp32'
					})
				);

				await $TTSWorker.init();
			} catch (error) {
				console.error(error);
				toast.error(`${error}`);
			}
		}

		startRecording();

		eventTarget.addEventListener('chat:start', chatStartHandler);
		eventTarget.addEventListener('chat', chatEventHandler);
		eventTarget.addEventListener('chat:finish', chatFinishHandler);

		document.addEventListener('keydown', handleKeydown);

		return async () => {
			await stopAllAudio();

			stopAudioStream();

			eventTarget.removeEventListener('chat:start', chatStartHandler);
			eventTarget.removeEventListener('chat', chatEventHandler);
			eventTarget.removeEventListener('chat:finish', chatFinishHandler);

			document.removeEventListener('keydown', handleKeydown);

			audioAbortController.abort();
			await tick();

			await stopAllAudio();

			await stopRecordingCallback(false);
			await stopCamera();
		};
	});

	onDestroy(async () => {
		if (bridge) {
			document.removeEventListener('keydown', handleKeydown);
			return;
		}
		destroyed = true;
		stopAvatarLevel();
		avatarModel?.dispose();
		avatarModel = null;
		await stopAllAudio();
		await stopRecordingCallback(false);
		await stopCamera();

		await stopAudioStream();
		eventTarget.removeEventListener('chat:start', chatStartHandler);
		eventTarget.removeEventListener('chat', chatEventHandler);
		eventTarget.removeEventListener('chat:finish', chatFinishHandler);

		document.removeEventListener('keydown', handleKeydown);

		audioAbortController.abort();

		await tick();

		await stopAllAudio();
	});
</script>

{#if $showCallOverlay}
	<div class="max-w-xl w-full h-full max-h-[100dvh] flex flex-col justify-between p-3 md:p-6">
		{#if camera}
			<button
				type="button"
				class="flex justify-center items-center w-full h-20 min-h-20"
				on:click={() => {
					if (assistantSpeaking) {
						stopAllAudio();
					}
				}}
			>
				{#if emoji}
					<div
						class="  transition-all rounded-full"
						style="font-size:{rmsLevel * 100 > 4
							? '4.5'
							: rmsLevel * 100 > 2
								? '4.25'
								: rmsLevel * 100 > 1
									? '3.75'
									: '3.5'}rem;width: 100%; text-align:center;"
					>
						{emoji}
					</div>
				{:else if loading || assistantSpeaking}
					<svg
						class="size-12 text-gray-900 dark:text-gray-400"
						viewBox="0 0 24 24"
						fill="currentColor"
						xmlns="http://www.w3.org/2000/svg"
						><style>
							.spinner_qM83 {
								animation: spinner_8HQG 1.05s infinite;
							}
							.spinner_oXPr {
								animation-delay: 0.1s;
							}
							.spinner_ZTLf {
								animation-delay: 0.2s;
							}
							@keyframes spinner_8HQG {
								0%,
								57.14% {
									animation-timing-function: cubic-bezier(0.33, 0.66, 0.66, 1);
									transform: translate(0);
								}
								28.57% {
									animation-timing-function: cubic-bezier(0.33, 0, 0.66, 0.33);
									transform: translateY(-6px);
								}
								100% {
									transform: translate(0);
								}
							}
						</style><circle class="spinner_qM83" cx="4" cy="12" r="3" /><circle
							class="spinner_qM83 spinner_oXPr"
							cx="12"
							cy="12"
							r="3"
						/><circle class="spinner_qM83 spinner_ZTLf" cx="20" cy="12" r="3" /></svg
					>
				{:else}
					<div
						class=" {rmsLevel * 100 > 4
							? ' size-[4.5rem]'
							: rmsLevel * 100 > 2
								? ' size-16'
								: rmsLevel * 100 > 1
									? 'size-14'
									: 'size-12'}  transition-all rounded-full bg-cover bg-center bg-no-repeat"
						style={`background-image: url('${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}&voice=true');`}
					/>
				{/if}
				<!-- navbar -->
			</button>
		{/if}

		<div class="flex justify-center items-center flex-1 h-full w-full max-h-full">
			{#if !camera}
				<button
					type="button"
					aria-label={assistantSpeaking ? $i18n.t('Stop speaking') : $i18n.t('Voice call')}
					on:click={() => {
						if (assistantSpeaking) {
							stopAllAudio();
						}
					}}
				>
					{#if avatarModel}
						<!-- Height follows the viewport up to a cap.
						     The caps were raised because they were binding well below the
						     space actually available: on a 651x1017 viewport the mobile cap
						     was 40rem (640px) inside a flex-1 area of roughly 883px, so
						     ~243px sat idle under the model and the character occupied only
						     about half the screen. 78vh/52rem resolves to 793px there.
						     The `max-h-[calc(100dvh_-_12rem)]` term is what keeps a tall
						     model from pushing the status line and the control row off
						     screen on a short window. It is an absolute length, so it
						     resolves without needing a definite ancestor height -- which
						     `max-h-full` does need, and cannot get here because this
						     box's ancestors are sized by content (so `max-h-full` was
						     silently doing nothing). 12rem covers the p-3/p-6 padding, the
						     status line and the control row, with slack. The renderer
						     contains the model inside whatever box this ends up being. -->
						<div
							class="relative h-[min(78vh,52rem)] w-full max-h-[calc(100dvh_-_12rem)] md:h-[min(82vh,56rem)]"
						>
							{#key avatarModel}
								<VoiceAvatarLive2D
									bind:this={avatarComponent}
									source={avatarModel}
									speaking={assistantSpeaking}
									listening={!muted && !assistantSpeaking}
									level={speechLevel}
									active={!destroyed}
									on:ready={(event) => {
										// The model declares its own expression names, and a rig
										// that declares none can never react. The capabilities
										// arrive on the event, so this does not depend on
										// `bind:this` having been assigned yet.
										const declared = event.detail?.expressions;
										avatarExpressions = Array.isArray(declared) ? declared : [];
										avatarExpression = null;
										// Set before the replay below, so `applyMoodExpression`
										// no longer mistakes "list not delivered yet" for "this rig
										// has no expressions".
										avatarReady = true;

										// Replay any mood that arrived while the model was still
										// loading. `bind:this` is assigned before this event fires,
										// so the renderer can now accept a request.
										if (pendingMoodEmoji !== null) {
											const replay = pendingMoodEmoji;
											pendingMoodEmoji = null;
											applyMoodExpression(replay);
										}
									}}
									on:error={(event) => {
										avatarError = event.detail?.message ?? 'Avatar could not be rendered.';
									}}
								/>
							{/key}
						</div>
					{:else if emoji}
						<div
							class="  transition-all rounded-full"
							style="font-size:{rmsLevel * 100 > 4
								? '13'
								: rmsLevel * 100 > 2
									? '12'
									: rmsLevel * 100 > 1
										? '11.5'
										: '11'}rem;width:100%;text-align:center;"
						>
							{emoji}
						</div>
					{:else if loading || assistantSpeaking}
						<svg
							class="size-44 text-gray-900 dark:text-gray-400"
							viewBox="0 0 24 24"
							fill="currentColor"
							xmlns="http://www.w3.org/2000/svg"
							><style>
								.spinner_qM83 {
									animation: spinner_8HQG 1.05s infinite;
								}
								.spinner_oXPr {
									animation-delay: 0.1s;
								}
								.spinner_ZTLf {
									animation-delay: 0.2s;
								}
								@keyframes spinner_8HQG {
									0%,
									57.14% {
										animation-timing-function: cubic-bezier(0.33, 0.66, 0.66, 1);
										transform: translate(0);
									}
									28.57% {
										animation-timing-function: cubic-bezier(0.33, 0, 0.66, 0.33);
										transform: translateY(-6px);
									}
									100% {
										transform: translate(0);
									}
								}
							</style><circle class="spinner_qM83" cx="4" cy="12" r="3" /><circle
								class="spinner_qM83 spinner_oXPr"
								cx="12"
								cy="12"
								r="3"
							/><circle class="spinner_qM83 spinner_ZTLf" cx="20" cy="12" r="3" /></svg
						>
					{:else}
						<div
							class=" {rmsLevel * 100 > 4
								? ' size-52'
								: rmsLevel * 100 > 2
									? 'size-48'
									: rmsLevel * 100 > 1
										? 'size-44'
										: 'size-40'} transition-all rounded-full bg-cover bg-center bg-no-repeat"
							style={`background-image: url('${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model?.id}&lang=${$i18n.language}&voice=true');`}
						/>
					{/if}
				</button>
			{:else}
				<div class="relative flex video-container w-full max-h-full pt-2 pb-4 md:py-6 px-2 h-full">
					<!-- svelte-ignore a11y-media-has-caption -->
					<video
						id="camera-feed"
						autoplay
						class="rounded-2xl h-full min-w-full object-cover object-center"
						playsinline
					/>

					<canvas id="camera-canvas" style="display:none;" />

					<div class=" absolute top-4 md:top-8 left-4">
						<button
							type="button"
							aria-label={$i18n.t('Stop camera')}
							class="p-1.5 text-white cursor-pointer backdrop-blur-xl bg-black/10 rounded-full"
							on:click={() => {
								stopCamera();
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 16 16"
								fill="currentColor"
								class="size-6"
							>
								<path
									d="M5.28 4.22a.75.75 0 0 0-1.06 1.06L6.94 8l-2.72 2.72a.75.75 0 1 0 1.06 1.06L8 9.06l2.72 2.72a.75.75 0 1 0 1.06-1.06L9.06 8l2.72-2.72a.75.75 0 0 0-1.06-1.06L8 6.94 5.28 4.22Z"
								/>
							</svg>
						</button>
					</div>
				</div>
			{/if}
		</div>

		<div class="flex flex-col items-center gap-4 pb-4 w-full">
			<button
				type="button"
				class="z-10"
				on:click={() => {
					if (assistantSpeaking) {
						stopAllAudio();
					}
				}}
			>
				<div class="line-clamp-1 text-sm font-normal" role="status" aria-live="polite">
					{#if bridge?.connecting}
						{$i18n.t('Connecting')}
					{:else if bridge?.approval}
						{$i18n.t('Waiting for approval')}
					{:else if loading}
						{$i18n.t('Thinking...')}
					{:else if muted}
						{$i18n.t('Muted')}
					{:else if assistantSpeaking}
						{$i18n.t('Tap to interrupt')}
					{:else}
						{$i18n.t('Listening...')}
					{/if}
				</div>
				{#if avatarError}
					<!-- Not clamped. The texture message is a full sentence with the
					     reason and the consequence; `line-clamp-2` cut it mid-word,
					     which read as a truncated error rather than an explanation.
					     `break-words` keeps a long path from overflowing instead. -->
					<div class="mt-1 max-w-xs text-center text-xs break-words text-red-500" role="alert">
						{avatarError}
					</div>
				{/if}
			</button>

			<div class="flex items-center justify-center gap-4 z-10">
				{#if camera}
					<VideoInputMenu
						devices={videoInputDevices.map((device) =>
							device.deviceId === 'screen' ? { ...device, label: $i18n.t('Screen Share') } : device
						)}
						on:change={async (e) => {
							console.log(e.detail);
							selectedVideoInputDeviceId = e.detail;
							localStorage.setItem('selectedVideoInputDeviceId', e.detail);
							await stopVideoStream();
							await startVideoStream();
						}}
					>
						<button
							aria-label={$i18n.t('Switch camera')}
							class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
							type="button"
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								viewBox="0 0 20 20"
								fill="currentColor"
								class="size-5"
							>
								<path
									fill-rule="evenodd"
									d="M15.312 11.424a5.5 5.5 0 0 1-9.201 2.466l-.312-.311h2.433a.75.75 0 0 0 0-1.5H3.989a.75.75 0 0 0-.75.75v4.242a.75.75 0 0 0 1.5 0v-2.43l.31.31a7 7 0 0 0 11.712-3.138.75.75 0 0 0-1.449-.39Zm1.23-3.723a.75.75 0 0 0 .219-.53V2.929a.75.75 0 0 0-1.5 0V5.36l-.31-.31A7 7 0 0 0 3.239 8.188a.75.75 0 1 0 1.448.389A5.5 5.5 0 0 1 13.89 6.11l.311.31h-2.432a.75.75 0 0 0 0 1.5h4.243a.75.75 0 0 0 .53-.219Z"
									clip-rule="evenodd"
								/>
							</svg>
						</button>
					</VideoInputMenu>
				{:else if !bridge}
					<Tooltip content={$i18n.t('Camera')}>
						<button
							aria-label={$i18n.t('Camera')}
							class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
							type="button"
							on:click={async () => {
								await navigator.mediaDevices.getUserMedia({ video: true });
								startCamera();
							}}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="1.5"
								stroke="currentColor"
								class="size-5"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M6.827 6.175A2.31 2.31 0 0 1 5.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 0 0 2.25 2.25h15A2.25 2.25 0 0 0 21.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 0 0-1.134-.175 2.31 2.31 0 0 1-1.64-1.055l-.822-1.316a2.192 2.192 0 0 0-1.736-1.039 48.774 48.774 0 0 0-5.232 0 2.192 2.192 0 0 0-1.736 1.039l-.821 1.316Z"
								/>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M16.5 12.75a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0ZM18.75 10.5h.008v.008h-.008V10.5Z"
								/>
							</svg>
						</button>
					</Tooltip>
				{/if}

				{#if !bridge}
					<Tooltip content={avatarModel ? $i18n.t('Change avatar') : $i18n.t('Load avatar')}>
						<label
							class="p-3 rounded-full bg-gray-50 dark:bg-gray-900 cursor-pointer {avatarError
								? 'text-red-500'
								: ''}"
							aria-label={avatarModel ? $i18n.t('Change avatar') : $i18n.t('Load avatar')}
							title={avatarError || avatarName || undefined}
						>
							<input
								type="file"
								accept=".zip,application/zip"
								class="sr-only"
								disabled={avatarBusy}
								on:change={pickAvatar}
							/>
							{#if avatarBusy}
								<span
									class="block size-5 animate-spin rounded-full border-2 border-current border-t-transparent"
								></span>
							{:else}
								<svg
									xmlns="http://www.w3.org/2000/svg"
									fill="none"
									viewBox="0 0 24 24"
									stroke-width="1.5"
									stroke="currentColor"
									class="size-5"
								>
									<path
										stroke-linecap="round"
										stroke-linejoin="round"
										d="M15.182 15.182a4.5 4.5 0 0 1-6.364 0M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM9.75 9.75c0 .414-.168.75-.375.75S9 10.164 9 9.75s.168-.75.375-.75.375.336.375.75Zm-.375 0h.008v.015h-.008V9.75Zm5.625 0c0 .414-.168.75-.375.75s-.375-.336-.375-.75.168-.75.375-.75.375.336.375.75Zm-.375 0h.008v.015h-.008V9.75Z"
									/>
								</svg>
							{/if}
						</label>
					</Tooltip>

					{#if avatarModel}
						<Tooltip content={$i18n.t('Remove avatar')}>
							<button
								aria-label={$i18n.t('Remove avatar')}
								class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
								type="button"
								on:click={forgetAvatar}
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									fill="none"
									viewBox="0 0 24 24"
									stroke-width="1.5"
									stroke="currentColor"
									class="size-5"
								>
									<path
										stroke-linecap="round"
										stroke-linejoin="round"
										d="M5.25 7.5 12 14.25 18.75 7.5M4.5 6h15M6.75 6l.75 12.75a1.5 1.5 0 0 0 1.5 1.5h6a1.5 1.5 0 0 0 1.5-1.5L17.25 6"
									/>
								</svg>
							</button>
						</Tooltip>
					{/if}
				{/if}

				<Tooltip content={muted ? $i18n.t('Unmute') + ' (M)' : $i18n.t('Mute') + ' (M)'}>
					<button
						class="p-3 rounded-full transition-colors duration-200 {muted
							? 'bg-red-500 text-white'
							: 'bg-gray-50 dark:bg-gray-900'}"
						type="button"
						aria-label={muted ? $i18n.t('Unmute') : $i18n.t('Mute')}
						on:click={toggleMute}
					>
						{#if muted}
							<!-- Mic Off icon -->
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="1.5"
								stroke="currentColor"
								class="size-5"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M12 18.75a6 6 0 0 0 6-6v-1.5m-6 7.5a6 6 0 0 1-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 0 1-3-3V4.5a3 3 0 1 1 6 0v8.25a3 3 0 0 1-3 3Z"
								/>
								<line
									x1="3"
									y1="3"
									x2="21"
									y2="21"
									stroke="currentColor"
									stroke-width="1.5"
									stroke-linecap="round"
								/>
							</svg>
						{:else}
							<!-- Mic On icon -->
							<svg
								xmlns="http://www.w3.org/2000/svg"
								fill="none"
								viewBox="0 0 24 24"
								stroke-width="1.5"
								stroke="currentColor"
								class="size-5"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d="M12 18.75a6 6 0 0 0 6-6v-1.5m-6 7.5a6 6 0 0 1-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 0 1-3-3V4.5a3 3 0 1 1 6 0v8.25a3 3 0 0 1-3 3Z"
								/>
							</svg>
						{/if}
					</button>
				</Tooltip>

				{#if bridge?.working}
					<Tooltip content={$i18n.t('Stop')}>
						<button
							type="button"
							aria-label={$i18n.t('Stop')}
							class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
							on:click={() => bridge?.stopBackend()}
						>
							<svg class="size-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
								<rect x="4" y="4" width="12" height="12" rx="2" />
							</svg>
						</button>
					</Tooltip>
				{/if}

				<button
					aria-label={$i18n.t('End call')}
					class="p-3 rounded-full bg-gray-50 dark:bg-gray-900"
					on:click={async () => {
						if (bridge) bridge.end();
						else {
							await stopAudioStream();
							await stopVideoStream();
						}

						showCallOverlay.set(false);
						dispatch('close');
					}}
					type="button"
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 20 20"
						fill="currentColor"
						class="size-5"
					>
						<path
							d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z"
						/>
					</svg>
				</button>
			</div>
		</div>
	</div>
{/if}
