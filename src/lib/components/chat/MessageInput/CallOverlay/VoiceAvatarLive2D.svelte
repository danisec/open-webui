<script lang="ts">
	import { createEventDispatcher, onMount } from 'svelte';
	import type { Live2DModelSource } from '$lib/utils/live2d-model-files';
	import type { createLive2DRenderer } from '$lib/utils/live2d-avatar-renderer';

	/**
	 * Live2D counterpart of `VoiceAvatar.svelte`.
	 *
	 * Same contract: the parent keys this component by model identity, the
	 * canvas fills its container, and rendering pauses when the tab is hidden or
	 * the canvas scrolls out of view. The renderer is loaded lazily so PixiJS
	 * and Cubism Core stay out of the initial bundle.
	 */
	export let source: Live2DModelSource;
	export let speaking = false;
	export let listening = false;
	export let level = 0;
	export let active = true;

	const dispatch = createEventDispatcher();
	let canvas: HTMLCanvasElement;
	let loading = true;
	let failed = false;
	let renderer: Awaited<ReturnType<typeof createLive2DRenderer>> | undefined;

	/**
	 * Apply an expression by name, or clear it with `null`.
	 *
	 * Exposed as a component method so the call overlay can drive the face from
	 * the emoji the reply already produces. Returns whether the request was
	 * accepted; a rig with no expression manager simply ignores it.
	 */
	export function setExpression(name: string | null) {
		if (!active || document.hidden) return false;
		return renderer?.setExpression(name) ?? false;
	}

	/** Expression names this model declares, for resolving a mood against it. */
	export function getExpressions(): string[] {
		return renderer?.capabilities?.expressions ?? [];
	}

	onMount(() => {
		const abort = new AbortController();
		let frame = 0;
		let previous = 0;
		let visible = true;
		const motion = window.matchMedia('(prefers-reduced-motion: reduce)');

		const animate = (now: number) => {
			if (abort.signal.aborted || document.hidden || !visible) {
				frame = 0;
				previous = 0;
				return;
			}
			try {
				renderer?.render(
					previous ? (now - previous) / 1000 : 0,
					null,
					{ active, speaking, listening, level },
					motion.matches
				);
				previous = now;
				frame = requestAnimationFrame(animate);
			} catch (error) {
				frame = 0;
				dispatch('error', error);
			}
		};

		const resume = () => {
			if (!frame && renderer && visible && !document.hidden) frame = requestAnimationFrame(animate);
		};

		const observer = new IntersectionObserver(([entry]) => {
			visible = entry.isIntersecting;
			resume();
		});
		observer.observe(canvas);
		document.addEventListener('visibilitychange', resume);

		const contextLost = (event: Event) => {
			event.preventDefault();
			dispatch('error', new Error('Avatar graphics context was lost.'));
		};
		canvas.addEventListener('webglcontextlost', contextLost);

		void (async () => {
			try {
				const { createLive2DRenderer } = await import('$lib/utils/live2d-avatar-renderer');
				abort.signal.throwIfAborted();
				renderer = await createLive2DRenderer(canvas, source, abort.signal);
				if (abort.signal.aborted) {
					renderer.dispose();
					return;
				}
				loading = false;
				dispatch('ready', renderer.capabilities);
				resume();
			} catch (error) {
				if (abort.signal.aborted) return;
				loading = false;
				failed = true;
				dispatch('error', error);
			}
		})();

		return () => {
			abort.abort();
			cancelAnimationFrame(frame);
			observer.disconnect();
			document.removeEventListener('visibilitychange', resume);
			canvas.removeEventListener('webglcontextlost', contextLost);
			renderer?.dispose();
			// Clear the handle: a disposed renderer must not be reachable from
			// `setExpression` after the component tears down, and `{#key}` remounts
			// the component when the model changes.
			renderer = undefined;
		};
	});
</script>

{#if loading}
	<div class="absolute inset-0 grid place-items-center">
		<span class="size-6 animate-spin rounded-full border-2 border-gray-300 border-t-transparent"
		></span>
		<span class="sr-only">Loading avatar...</span>
	</div>
{/if}

{#if failed}
	<div class="absolute inset-0 grid place-items-center px-6 text-center text-xs text-gray-500">
		Avatar could not be rendered on this device.
	</div>
{/if}

<canvas bind:this={canvas} aria-hidden="true" class="live2d-canvas" class:hidden-canvas={failed}
></canvas>

<style>
	.live2d-canvas {
		display: block;
		width: 100%;
		height: 100%;
		pointer-events: none;
	}

	.hidden-canvas {
		visibility: hidden;
	}
</style>
