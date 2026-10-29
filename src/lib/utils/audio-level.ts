/**
 * Lip-sync level for the classic (non-realtime) call path.
 *
 * The realtime path gets its level from the server's PCM stream. This path has
 * no such stream: audio goes straight into the shared `#audioElement` declared
 * in `Chat.svelte`, so the level has to be measured from that element.
 *
 * Two browser rules shape this file.
 *
 * 1. `createMediaElementSource` may be called only once per element. After the
 *    call the element's output is permanently routed through the node, so a
 *    node that is not connected to `destination` means silence. Calling it a
 *    second time throws.
 * 2. An `AudioContext` created outside a user gesture starts `suspended`, and a
 *    suspended context produces no sound. Routing the element into one would
 *    mute the assistant.
 *
 * Therefore the graph is built only after `resume()` has actually put the
 * context in `running`, and it is then kept for the life of the page. If the
 * context cannot run, nothing is created and the element keeps playing
 * normally; lip-sync degrades to a closed mouth instead of breaking audio.
 */

type Graph = {
	element: HTMLAudioElement;
	context: AudioContext;
	analyser: AnalyserNode;
	buffer: Uint8Array;
};

let graph: Graph | null = null;
let pending: Promise<boolean> | null = null;

type AudioContextCtor = typeof AudioContext;

function audioContextCtor(): AudioContextCtor | undefined {
	if (typeof window === 'undefined') return undefined;
	return (
		window.AudioContext ??
		(window as unknown as { webkitAudioContext?: AudioContextCtor }).webkitAudioContext
	);
}

/**
 * Build the analyser graph for `element`, or report that it cannot be built on
 * this device. Safe to call repeatedly: the work happens once.
 */
export function prepareAudioLevel(element: HTMLAudioElement): Promise<boolean> {
	if (graph?.element === element) return Promise.resolve(true);
	if (pending) return pending;

	pending = (async () => {
		const Ctor = audioContextCtor();
		if (!Ctor) return false;

		const context = new Ctor();
		try {
			await context.resume();
		} catch {
			// Autoplay policy refused; the state check below decides.
		}
		if (context.state !== 'running') {
			await context.close().catch(() => {});
			return false;
		}

		const source = context.createMediaElementSource(element);
		const analyser = context.createAnalyser();
		analyser.fftSize = 1024;
		analyser.smoothingTimeConstant = 0.6;
		// The source now feeds the speakers through the graph, so this connection
		// is what keeps the audio audible.
		source.connect(analyser);
		analyser.connect(context.destination);

		graph = { element, context, analyser, buffer: new Uint8Array(analyser.fftSize) };
		return true;
	})().finally(() => {
		pending = null;
	});

	return pending;
}

/** RMS of the currently playing audio, 0..1. Returns 0 when nothing plays. */
export function audioLevel(element: HTMLAudioElement | null, active: boolean): number {
	if (!element || !active || element.paused || element.muted) return 0;
	if (graph?.element !== element) return 0;
	try {
		graph.analyser.getByteTimeDomainData(graph.buffer);
		let sum = 0;
		for (let i = 0; i < graph.buffer.length; i++) {
			const sample = (graph.buffer[i] - 128) / 128;
			sum += sample * sample;
		}
		return Math.sqrt(sum / graph.buffer.length);
	} catch {
		// A detached element cannot be analysed; lip-sync is cosmetic, so
		// degrade to a closed mouth rather than breaking the call.
		return 0;
	}
}
