type AudioQueueEvent = 'stop' | 'empty-queue' | 'id-change' | 'error';

interface AudioQueueStopDetail {
	event: AudioQueueEvent;
	id: string | null;
}

const SILENT_WAV =
	'data:audio/wav;base64,UklGRsQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';

export type OnStoppedCallback = (detail: AudioQueueStopDetail) => void;

export class AudioQueue {
	private audio: HTMLAudioElement;
	private queue: string[] = [];
	private current: string | null = null;
	private waiting = false;
	private producerDone = false;
	private waitTimer: ReturnType<typeof setTimeout> | null = null;
	private readonly _onEnded = () => this.next();
	private readonly _onGesture = () => this.#unlock();

	id: string | null = null;
	onStopped: OnStoppedCallback | null = null;

	/** Chunks buffered before playback starts, so a slow producer cannot starve the player. */
	minBuffer = 2;
	/** How long to wait for the next chunk before giving up (ms). */
	maxWait = 45000;

	constructor(audioElement: HTMLAudioElement) {
		this.audio = audioElement;
		this.audio.addEventListener('ended', this._onEnded);
		document.addEventListener('pointerdown', this._onGesture, true);
		document.addEventListener('keydown', this._onGesture, true);
	}

	setId(newId: string) {
		if (this.id === newId) return;

		this.#halt();
		this.id = newId;
		this.onStopped?.({ event: 'id-change', id: newId });
	}

	setPlaybackRate(rate: number) {
		this.audio.defaultPlaybackRate = rate;
		this.audio.playbackRate = rate;
	}

	enqueue(url: string) {
		this.queue.push(url);

		// Resume a playback that ran dry while the producer was still working.
		if (this.waiting) {
			this.#clearWaitTimer();
			this.waiting = false;
			this.next();
			return;
		}

		// Hold playback until the buffer is filled (or nothing more is coming).
		if (
			this.audio.paused &&
			!this.current &&
			(this.queue.length >= this.minBuffer || this.producerDone)
		) {
			this.next();
		}
	}

	/** Producer finished: no more chunks will arrive for this message. */
	markProducerDone() {
		this.producerDone = true;
		this.#clearWaitTimer();

		if (this.waiting && this.queue.length === 0) {
			this.#finish('empty-queue');
			return;
		}

		if (!this.current && this.audio.paused && this.queue.length > 0) {
			this.next();
		} else if (!this.current && this.queue.length === 0 && this.audio.paused) {
			this.#finish('empty-queue');
		}
	}

	play() {
		if (!this.current && this.queue.length > 0) {
			this.next();
		} else {
			this.audio.play();
		}
	}

	/** Play silence inside the first user gesture so WebKit allows later programmatic playback. */
	#unlock() {
		if (this.current || !this.audio.paused) return;

		this.audio.src = SILENT_WAV;
		this.audio
			.play()
			.then(() => this.#removeGestureListeners())
			.catch(() => {})
			.finally(() => {
				if (this.current) return;

				this.audio.pause();
				if (this.queue.length) this.next();
			});
	}

	next() {
		const previous = this.current;
		this.current = this.queue.shift() ?? null;

		if (this.current) {
			const url = this.current;
			this.audio.src = url;
			this.audio.muted = false;
			this.audio.play().catch((error) => {
				if (this.current !== url) return;

				console.error(error);
				this.#finish('error');
			});
			this.#revoke(previous);
			return;
		}

		this.#revoke(previous);

		if (this.producerDone) {
			this.#finish('empty-queue');
		} else {
			// Audio drained but chunks are still being generated: wait for the next one.
			this.waiting = true;
			this.#startWaitTimer();
		}
	}

	stop() {
		this.#halt();
		this.onStopped?.({ event: 'stop', id: this.id });
	}

	destroy() {
		this.audio.removeEventListener('ended', this._onEnded);
		this.#removeGestureListeners();
		this.#halt();
		this.onStopped = null;
	}

	#startWaitTimer() {
		this.#clearWaitTimer();
		this.waitTimer = setTimeout(() => {
			this.waitTimer = null;
			if (!this.waiting) return;

			this.#finish('empty-queue');
		}, this.maxWait);
	}

	#clearWaitTimer() {
		if (this.waitTimer !== null) {
			clearTimeout(this.waitTimer);
			this.waitTimer = null;
		}
	}

	#removeGestureListeners() {
		document.removeEventListener('pointerdown', this._onGesture, true);
		document.removeEventListener('keydown', this._onGesture, true);
	}

	#revoke(url: string | null) {
		if (!url || !url.startsWith('blob:')) return;

		try {
			URL.revokeObjectURL(url);
		} catch {
			// ignore
		}
	}

	#finish(event: AudioQueueEvent) {
		this.#halt();
		this.onStopped?.({ event, id: this.id });
	}

	/**
	 * Pause audio and clear queue without firing onStopped.
	 * Callers that need the callback should invoke it themselves.
	 */
	#halt() {
		this.#clearWaitTimer();
		this.waiting = false;
		this.producerDone = false;

		this.audio.pause();
		this.audio.currentTime = 0;
		this.audio.removeAttribute('src');
		this.audio.load();

		this.#revoke(this.current);
		for (const url of this.queue) this.#revoke(url);

		this.queue = [];
		this.current = null;
	}
}
