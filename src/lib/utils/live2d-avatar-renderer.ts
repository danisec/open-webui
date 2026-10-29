/**
 * Live2D Cubism 4 renderer, matching the surface of `voice-avatar-renderer.ts`
 * so the same `VoiceAvatar.svelte` contract drives either kind of model.
 *
 * Deliberate differences from the VRM renderer:
 *
 *   - Live2D models have no skeleton. Head turn, breath and body sway are
 *     written straight to the parameters the rigger named (`ParamAngleX`,
 *     `ParamBreath`, `ParamBodyAngleZ`), not to bones.
 *   - Lip-sync uses the `LipSync` group the model itself declares in
 *     `model3.json`, which is `ParamMouthOpenY` for most rigs. Blink likewise
 *     uses the declared `EyeBlink` group, so rigs with custom parameter names
 *     keep working.
 *   - There are no authored animation files registered for this model, so
 *     `loadAnimation` and `playGesture` report `unavailable` rather than
 *     pretending.
 *
 * The library is given a ready-made `Cubism4ModelSettings` whose `resolveURL`
 * points at object URLs. That bypasses its `File[]` path, which cannot handle
 * the non-ASCII archive entries this model uses; see `live2d-model-files.ts`.
 */

import type { Live2DModelSource } from './live2d-model-files';
import { checkTextureBudget, loadCubismCore, textureLimitOf } from './live2d-model-files';

export type Live2DAvatarSignals = {
	speaking: boolean;
	listening: boolean;
	level: number;
	active: boolean;
};

const MOUTH_PARAM = 'ParamMouthOpenY';
const MOUTH_FORM = 'ParamMouthForm';

/**
 * Speech RMS sits around 0.03-0.13, so a raw 0..1 level barely moves a mouth
 * parameter. The VRM renderer has always applied a gain here; this path did
 * not, which is why the mouth looked dead. 4 puts the peaks at roughly 40% of
 * the parameter's travel, which is where natural speech reads.
 */
const MOUTH_GAIN = 4;
const MOUTH_FORM_SPEAKING = 0.4;

/** Parameter ids the model may or may not define; writes are guarded. */
const IDLE_PARAMS = [
	'ParamAngleX',
	'ParamAngleY',
	'ParamAngleZ',
	'ParamBodyAngleX',
	'ParamBodyAngleZ'
];

type CoreModel = {
	setParameterValueById: (id: string, value: number, weight?: number) => void;
	getParameterIndex?: (id: string) => number;
	getParameterMaximumValue?: (index: number) => number;
};

export async function createLive2DRenderer(
	canvas: HTMLCanvasElement,
	source: Live2DModelSource,
	signal: AbortSignal
) {
	// Core must exist on `window` before the runtime module is evaluated: the
	// module reads `window.Live2DCubismCore` at import time and throws without it.
	await loadCubismCore();
	signal.throwIfAborted();

	const [{ Application }, live2d] = await Promise.all([
		import('pixi.js'),
		import('pixi-live2d-display-lipsyncpatch/cubism4')
	]);
	signal.throwIfAborted();

	const { Live2DModel, Cubism4ModelSettings } = live2d;

	// `url` is only used by the library to derive a display name here; every
	// resource lookup goes through the override below.
	const settings = new Cubism4ModelSettings({ ...source.json, url: source.settingsPath });
	settings.resolveURL = (ref: string) => source.resolve(ref);

	// Drive the model from our own loop; the built-in automator would fight it.
	const app = new Application({
		view: canvas,
		backgroundAlpha: 0,
		antialias: true,
		powerPreference: 'low-power',
		autoStart: false,
		resolution: Math.min(window.devicePixelRatio || 1, 2),
		autoDensity: true
	});

	// Check against the context that will actually upload the texture, not a
	// throwaway probe. This context exists now, so its answer is exact: a probe
	// can be refused for reasons unrelated to the limit and then reports nothing,
	// while this renderer works fine -- which is precisely the false positive
	// that rejected this 8192px model on capable phones.
	try {
		checkTextureBudget(source.textureEdge, textureLimitOf(app.renderer.gl));
	} catch (error) {
		app.destroy(false, { children: false, texture: false, baseTexture: false });
		throw error;
	}

	let model: Awaited<ReturnType<typeof Live2DModel.from>>;
	try {
		model = await Live2DModel.from(settings, { autoInteract: false, autoUpdate: false });
	} catch (error) {
		app.destroy(false, { children: true, texture: false, baseTexture: false });
		throw error;
	}
	signal.throwIfAborted();

	app.stage.addChild(model);
	model.anchor.set(0.5, 0.5);
	model.interactive = false;

	const core = model.internalModel.coreModel as unknown as CoreModel;

	const has = (id: string) => hasParameter(core, id);
	const mouth = has(MOUTH_PARAM);
	const mouthForm = has(MOUTH_FORM);
	const breath = has('ParamBreath');
	const idle = IDLE_PARAMS.filter(has);

	// Expression names come from the model's own `FileReferences.Expressions`,
	// not from a fixed list: a rig is free to name them anything. Reported as a
	// capability so the caller can decide whether expressions are worth using at
	// all, and so it can resolve a mood against what actually exists.
	const expressions: string[] = (
		(source.json?.FileReferences?.Expressions ?? []) as { Name?: string }[]
	)
		.map((definition) => definition?.Name)
		.filter((name): name is string => typeof name === 'string' && name.length > 0);

	const expressionManager = model.internalModel.motionManager.expressionManager;

	// Diagnosis is otherwise impossible from the outside: a rig with no
	// expressions, an emoji that maps to no mood, and an expression name the rig
	// does not declare all look identical from the UI -- the face simply does not
	// move. One line at load time makes the first and most common case obvious
	// without needing the user to unzip their model.
	console.info(
		`[live2d] expressions declared by model: ${expressions.length}`,
		expressions.length ? expressions : '(none -- this model cannot change expression)'
	);

	// The mouth parameter is authored in the rig's own units: 0..1 for most
	// models, 0..2.1 for this one. Writing a normalised 0..1 weight straight in
	// would cap the mouth at 48% of its travel, so the ceiling comes from the
	// model rather than from an assumption. `getParameterIndex` fabricates an
	// index for unknown ids, so the value is validated before it is trusted.
	const mouthIndex = core.getParameterIndex?.(MOUTH_PARAM) ?? -1;
	const rawMouthMax = mouthIndex >= 0 ? core.getParameterMaximumValue?.(mouthIndex) : undefined;
	const mouthMax =
		typeof rawMouthMax === 'number' && Number.isFinite(rawMouthMax) && rawMouthMax > 0
			? rawMouthMax
			: 1;

	// Blinking is driven by the rig's own `EyeBlink` group, which the library
	// reads from the settings JSON; report it as a capability so a caller can
	// decide whether to bother.
	const blink = (source.json?.Groups ?? []).some(
		(group: { Name?: string }) => group?.Name === 'EyeBlink'
	);

	let elapsed = 0;
	let mouthWeight = 0;
	let mouthFormWeight = 0;
	let listeningBlend = 0;
	let disposed = false;

	// Expression state. `wantedExpression` is the name the caller asked for;
	// `appliedExpression` is what the library currently has. They are tracked
	// separately because `setExpression` is async and can legitimately fail on a
	// rig whose expression file is missing or malformed -- the name is still
	// "wanted" so a later frame can retry once the file has loaded.
	let wantedExpression: string | null = null;
	let appliedExpression: string | null = null;
	let expressionPending = false;
	// A name the rig cannot load would otherwise be retried on every frame --
	// 60 promise allocations per second for the rest of the call -- because the
	// reconcile step in `render()` has no other way to know it has already tried.
	// The cap turns a permanent failure into a bounded cost and leaves the face at
	// its default, which is the correct outcome for a broken expression file.
	let expressionAttempts = 0;
	const EXPRESSION_MAX_ATTEMPTS = 3;

	// Refreshed by `render()` each frame, read by the `beforeModelUpdate` handler
	// installed below. The handler runs inside `internalModel.update`, which is
	// invoked from `model.update` in that same call, so the value is always the
	// one from the current frame.
	let input_speaking = false;

	// A mood-driven expression must not freeze the mouth. The library writes
	// expressions *after* the parameters this renderer sets: `InternalModel.update`
	// runs `motionManager.update`, then `saveParameters()`, then
	// `expressionManager.update`. So an expression that drives `ParamMouthOpenY`
	// would overwrite the lip-sync value written earlier in the same frame and the
	// mouth would stop moving while that expression is active.
	//
	// `beforeModelUpdate` fires after the expression manager has run and before
	// the mesh is calculated, which is the one place a correction survives. Only
	// the mouth parameters are re-asserted there, and only while the model is
	// actually speaking: re-writing everything would fight the expression's own
	// intent (brows, eyes, mouth shape).
	const internal = model.internalModel as unknown as {
		on?: (event: string, handler: () => void) => void;
		off?: (event: string, handler: () => void) => void;
	};
	const assertMouthAfterExpression = () => {
		if (!input_speaking) return;
		if (mouth) core.setParameterValueById(MOUTH_PARAM, mouthWeight * mouthMax);
		if (mouthForm && mouthFormWeight > 0.001) {
			core.setParameterValueById(MOUTH_FORM, mouthFormWeight);
		}
	};
	if (typeof internal.on === 'function') {
		internal.on('beforeModelUpdate', assertMouthAfterExpression);
	}

	// Intrinsic size, read once. `model.height` is NOT this: pixi defines it as
	// `scale.y * getLocalBounds().height`, so dividing by it makes `fit()`
	// depend on the scale it is about to set. Two calls then oscillate --
	// 0.029 -> 1.0 -> 0.029 -- and since ResizeObserver always fires once on
	// observe(), the second call is guaranteed and the model lands at scale 1,
	// i.e. a 5167x9410 px drawing clipped to the panel. Live2D models are also
	// authored at wildly different sizes, so this has to come from the model.
	const baseWidth = model.internalModel.width;
	const baseHeight = model.internalModel.height;

	// Live2D art may draw outside its own canvas rectangle (hair, props, long
	// sleeves), so the contain-fit used to hold back a margin. That margin was
	// costing 6% of the model on every screen narrower than 768px -- the case
	// where space is scarcest -- for no measured benefit: the panel is a plain
	// white/dark background with no border or content for the art to collide
	// with. Desktop already used 1.0. The one thing a margin did buy was keeping
	// the model clear of the control row underneath on a short window, and the
	// box now reserves 12rem for exactly that (see CallOverlay.svelte), so the
	// margin is redundant there too.
	//
	// Measuring the canvas every frame would force a layout read at 60 Hz. The
	// size only changes when the window or the panel does, so it is observed.
	const fit = () => {
		const rect = canvas.getBoundingClientRect();
		if (!rect.width || !rect.height || !baseWidth || !baseHeight) return;
		app.renderer.resize(rect.width, rect.height);
		// Contain, not cover: take the limiting axis so a model wider than it is
		// tall still fits, then centre it. Fitting by height alone clipped wide
		// rigs.
		const scale = Math.min(rect.height / baseHeight, rect.width / baseWidth);
		model.scale.set(scale);
		model.position.set(rect.width / 2, rect.height / 2);
	};
	fit();

	const resizeObserver = new ResizeObserver(fit);
	resizeObserver.observe(canvas);

	// Expression application lives in a named function so both the public method
	// and the retry in `render()` call the same code, without re-entering the
	// returned object through `this`.
	//
	// `isRetry` distinguishes the two callers. A fresh request from the caller
	// resets the attempt budget and short-circuits when nothing changed; a retry
	// must do neither, or the cap in `render()` would restore the budget it is
	// supposed to be spending and a broken expression file would cost a promise
	// every frame for the rest of the call.
	const applyExpression = (name: string | null, isRetry = false): boolean => {
		if (disposed || !expressionManager) return false;
		if (!isRetry) {
			if (name === wantedExpression) return appliedExpression === name;
			expressionAttempts = 0;
		}
		wantedExpression = name;

		if (name === null) {
			expressionManager.resetExpression();
			appliedExpression = null;
			return true;
		}

		// `setExpression` is async: it loads the `.exp.json` on first use. Fire and
		// forget here, then reconcile in `render()` so a failure can be retried and
		// a success can be reported on a later frame.
		expressionPending = true;
		expressionAttempts++;
		void expressionManager
			.setExpression(name)
			.then((ok: boolean) => {
				if (disposed || wantedExpression !== name) return;
				appliedExpression = ok ? name : null;
			})
			.catch(() => {
				if (disposed || wantedExpression !== name) return;
				appliedExpression = null;
			})
			.finally(() => {
				expressionPending = false;
			});

		return true;
	};

	return {
		capabilities: { mouth, blink, expressions },
		async loadAnimation() {
			// Gestures would be `.motion3.json` registered under `Motions`; this
			// model declares none, and inventing a mapping would be guesswork.
			return 'unavailable' as const;
		},
		playGesture() {
			return 'unavailable' as const;
		},
		cancelGesture() {
			// Nothing to cancel: no authored clips are played.
		},
		/**
		 * Set the model's expression by name, or clear it with `null`.
		 *
		 * Returns whether the request was accepted. `false` means the rig has no
		 * expression manager or the renderer is gone; a name the rig does not
		 * define is accepted here and simply never becomes current, which the
		 * caller can detect via the `expressions` capability.
		 *
		 * The request is remembered even when a load fails, so a transient failure
		 * is retried on the next `render()` instead of silently disabling
		 * expressions for the rest of the call.
		 */
		setExpression(name: string | null) {
			if (name === wantedExpression) return appliedExpression === name;
			return applyExpression(name);
		},
		render(delta: number, _config: unknown, input: Live2DAvatarSignals, reducedMotion: boolean) {
			if (disposed) return;
			const dt = Math.min(delta, 0.05);
			elapsed += dt;
			input_speaking = input.speaking;

			const smooth = 1 - Math.exp(-dt * 5);
			listeningBlend += ((input.listening ? 1 : 0) - listeningBlend) * smooth;

			if (mouth) {
				// Follow the audio while speaking, and close immediately otherwise
				// so a stopped response never leaves the mouth hanging open.
				const target = input.speaking ? Math.min(1, Math.max(0, input.level) * MOUTH_GAIN) : 0;
				mouthWeight += (target - mouthWeight) * (1 - Math.exp(-dt * 24));
				core.setParameterValueById(MOUTH_PARAM, mouthWeight * mouthMax);
			}
			if (mouthForm) {
				// Decay back to rest instead of leaving the smile latched on after
				// the first response. Writes stop once settled, so expressions that
				// drive the same parameter regain control instead of being
				// overwritten every frame.
				const formTarget = input.speaking ? MOUTH_FORM_SPEAKING : 0;
				mouthFormWeight += (formTarget - mouthFormWeight) * (1 - Math.exp(-dt * 6));
				if (input.speaking || mouthFormWeight > 0.001) {
					core.setParameterValueById(MOUTH_FORM, mouthFormWeight);
				}
			}
			if (breath) core.setParameterValueById('ParamBreath', (Math.sin(elapsed * 1.1) + 1) / 2);

			if (!reducedMotion && idle.length) {
				// Idle sway, nudged toward the user while listening.
				const headX = Math.sin(elapsed * 0.43) * 4 + listeningBlend * 6;
				const values: Record<string, number> = {
					ParamAngleX: headX,
					ParamAngleY: Math.sin(elapsed * 0.32) * 8,
					ParamAngleZ: Math.sin(elapsed * 0.27) * 3,
					ParamBodyAngleX: headX * 0.25,
					ParamBodyAngleZ: Math.sin(elapsed * 0.21) * 2
				};
				for (const id of idle) core.setParameterValueById(id, values[id] ?? 0);
			}

			model.update(dt * 1000);
			app.renderer.render(app.stage);

			// A failed expression load is retried here rather than being abandoned:
			// the library loads `.exp.json` lazily and can fail transiently, and a
			// mood that silently never applies is worse than one that applies late.
			// One retry in flight at a time, and at most `EXPRESSION_MAX_ATTEMPTS`
			// in total, so a permanently broken expression cannot cost a promise per
			// frame for the rest of the call.
			if (
				wantedExpression &&
				appliedExpression !== wantedExpression &&
				!expressionPending &&
				expressionAttempts < EXPRESSION_MAX_ATTEMPTS
			) {
				applyExpression(wantedExpression, true);
			}
		},
		dispose() {
			if (disposed) return;
			disposed = true;
			resizeObserver.disconnect();
			if (typeof internal.off === 'function') {
				internal.off('beforeModelUpdate', assertMouthAfterExpression);
			}
			// Destroy the model once, with its textures, then the stage without
			// children so the same display object is not torn down twice. The
			// object URLs belong to the source and are revoked by its own
			// `dispose()`, so a remount can still read them.
			model.destroy({ children: true, texture: true, baseTexture: true });
			app.destroy(false, { children: false, texture: false, baseTexture: false });
		}
	};
}

/**
 * Cubism exposes parameters by index, not by name, so a rig that renamed its
 * mouth parameter is detected rather than assumed.
 *
 * This is a weak check by design of the library: `getParameterIndex` never
 * returns -1, it registers unknown ids and hands back a synthetic index past
 * the end of the real table. So a missing parameter still reports `true` here.
 * The value is only used to decide whether to write; the write itself is
 * harmless on a rig that lacks the id, and `mouthMax` falls back to 1.
 */
function hasParameter(core: { getParameterIndex?: (id: string) => number }, id: string): boolean {
	if (typeof core.getParameterIndex !== 'function') return false;
	try {
		return core.getParameterIndex(id) >= 0;
	} catch {
		return false;
	}
}
