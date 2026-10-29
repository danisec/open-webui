/**
 * Load a Live2D Cubism 4 model entirely from files the user picked on their own
 * device. Nothing is uploaded, so the server never stores or serves the model
 * and the model's redistribution terms are never engaged.
 *
 * The obvious route is the library's own `File[]` source (`FileLoader`), but it
 * is unusable for this model. It validates with
 *
 *     settings.validateFiles(files.map((f) => encodeURI(f.webkitRelativePath)))
 *
 * while `ModelSettings.resolveURL` returns the *raw* path from `utils.url.resolve`.
 * For an archive whose entries are `魔女/魔女.moc3`, `encodeURI` produces
 * `%E9%AD%94%E5%A5%B3/...`, the two sides never match, and loading throws
 * "file is defined in settings, but doesn't exist in given files". `upload()`
 * has the mirror-image problem, comparing raw paths against decoded ones.
 *
 * So this module does not use `FileLoader` at all. It parses the settings JSON,
 * creates one object URL per archive entry, and hands the library a ready-made
 * `Cubism4ModelSettings` whose `resolveURL` is pointed at those object URLs.
 * `Live2DFactory` accepts a `ModelSettings` instance directly, which skips the
 * `File[]` middleware entirely. Paths stay literal end to end.
 *
 * Object URLs are owned by the source object and live as long as it does, not
 * as long as a renderer: the overlay may unmount and remount the canvas for the
 * same model, and revoking on unmount would leave the second mount with dead
 * URLs. `dispose()` is called when the user swaps or forgets the model.
 */

const CORE_URL = '/live2dcubismcore.min.js';

/** Above this the archive will not fit in a tab without stalling the main thread. */
export const LIVE2D_MAX_BYTES = 200 * 1024 * 1024;

export type Live2DModelSource = {
	/** Path of the `.model3.json` inside the archive, for error messages. */
	settingsPath: string;
	/** Parsed settings JSON, ready for `Cubism4ModelSettings`. */
	json: Record<string, any>;
	/** Largest texture edge of the CHOSEN set, used before the GPU rejects it. */
	textureEdge: number;
	/**
	 * True when a reduced texture set was selected because the full one exceeds this
	 * device's limit. Callers use it to tell the admin that quality was traded for
	 * the model actually loading.
	 */
	textureReduced: boolean;
	/** Archive-relative path to a live object URL. Throws when unknown. */
	resolve: (ref: string) => string;
	/** Revokes every object URL this source created. Safe to call twice. */
	dispose: () => void;
};

let corePromise: Promise<void> | null = null;

/**
 * Inject Cubism Core once per page. Resolves when `window.Live2DCubismCore`
 * exists, so the runtime module can be imported safely afterwards: that module
 * reads `window.Live2DCubismCore` at module scope and throws without it.
 */
export function loadCubismCore(): Promise<void> {
	if (typeof window === 'undefined') return Promise.reject(new Error('No browser context.'));
	if (window.Live2DCubismCore) return Promise.resolve();
	if (corePromise) return corePromise;

	corePromise = new Promise<void>((resolve, reject) => {
		const script = document.createElement('script');
		script.src = CORE_URL;
		script.async = true;
		script.onload = () => {
			if (window.Live2DCubismCore) resolve();
			else reject(new Error('Cubism Core loaded but did not initialise.'));
		};
		script.onerror = () => reject(new Error('Could not load Cubism Core.'));
		document.head.appendChild(script);
	}).catch((error) => {
		// Let a later attempt retry instead of caching the failure forever.
		corePromise = null;
		throw error;
	});

	return corePromise;
}

/** Cached probe result. `undefined` = not probed yet, `null` = could not tell. */
let textureLimit: number | null | undefined;

/**
 * Read `MAX_TEXTURE_SIZE` off a context that already exists.
 *
 * This is the authoritative answer and should be preferred whenever a real
 * renderer context is available: a throwaway probe canvas can be refused for
 * reasons that have nothing to do with the limit (too many live contexts, a
 * driver that only allows one, a context-lost state), and then the probe knows
 * nothing while the actual renderer works fine.
 *
 * Typed structurally rather than as `WebGLRenderingContext` so pixi's
 * `IRenderingContext` can be passed without a cast.
 */
export function textureLimitOf(gl: {
	getParameter: (pname: number) => unknown;
	MAX_TEXTURE_SIZE: number;
}): number | null {
	try {
		const raw = gl.getParameter(gl.MAX_TEXTURE_SIZE) as number;
		return Number.isFinite(raw) && raw > 0 ? raw : null;
	} catch {
		return null;
	}
}

/**
 * Ask the real GL context for its texture limit instead of assuming one.
 * Drivers disagree wildly: desktop parts allow 16384, many mobile parts stop at
 * 8192 or 4096, and exceeding the limit fails as a blank canvas rather than an
 * error.
 *
 * Returns `null` when no context could be obtained. That distinction matters:
 * this used to return a hardcoded 4096 on failure, which turned "could not
 * measure" into a confident hardware claim and rejected models the device could
 * in fact load. The result is cached because the probe costs a real GL context,
 * and browsers cap how many may be live at once -- probing per call could
 * exhaust that budget and make the *renderer's* own context fail.
 */
export function maxTextureSize(): number | null {
	if (typeof document === 'undefined') return null;
	if (textureLimit !== undefined) return textureLimit;

	let limit: number | null = null;
	try {
		// Two separate canvases on purpose. A canvas is locked to the first
		// context type requested, so asking the same one for `webgl` after
		// `webgl2` returned null yields null again on Safari -- which is how a
		// perfectly capable iPhone ended up being told its limit was 4096.
		const probe = (type: 'webgl2' | 'webgl') => {
			const canvas = document.createElement('canvas');
			const gl = canvas.getContext(type) as WebGLRenderingContext | null;
			if (!gl) return null;
			const measured = textureLimitOf(gl);
			// Release the probe context immediately; browsers cap how many may be live.
			gl.getExtension('WEBGL_lose_context')?.loseContext();
			return measured;
		};
		limit = probe('webgl2') ?? probe('webgl');
	} catch {
		limit = null;
	}

	textureLimit = limit;
	return limit;
}

/**
 * Reject the one case that fails silently on real hardware: a texture larger
 * than the driver allows.
 *
 * Only rejects on a *measured* limit. When the limit is unknown the model is
 * allowed through: a failed load shows its own error, which is more honest than
 * refusing to try on the strength of a guess.
 */
export function checkTextureBudget(
	textureEdge: number,
	limit: number | null = maxTextureSize()
): void {
	if (limit === null) {
		console.warn(
			`[avatar] could not read this device's texture limit; loading a ${textureEdge}px model without the pre-check`
		);
		return;
	}
	if (textureEdge > limit) {
		throw new Error(
			`This model needs a ${textureEdge}px texture, but this device allows at most ${limit}px. It would load as a blank canvas, so it was not loaded.`
		);
	}
}

function isJunk(path: string): boolean {
	const base = path.split('/').pop() ?? '';
	return (
		path.includes('__MACOSX/') ||
		base === '.DS_Store' ||
		base === 'Thumbs.db' ||
		base.toLowerCase().endsWith('.url')
	);
}

/**
 * Resolve a reference from the settings file against the archive.
 *
 * `new URL()` cannot be used here: it percent-encodes non-ASCII path segments,
 * so `魔女/魔女.moc3` becomes `%E9%AD%94...` and no longer matches the archive
 * keys. Zip entries keep their literal UTF-8 names, so the join is done by hand.
 */
function resolveRelative(settingsPath: string, ref: string): string {
	const segments = settingsPath.split('/').slice(0, -1).concat(ref.split('/'));
	const out: string[] = [];
	for (const segment of segments) {
		if (!segment || segment === '.') continue;
		if (segment === '..') out.pop();
		else out.push(segment);
	}
	return out.join('/');
}

/**
 * Read image dimensions from the PNG or JPEG header. Returns 0 when the format
 * is not recognised, which is not an error: the loader will surface it.
 */
function imageEdge(data: Uint8Array): number {
	// PNG: 8-byte signature, then IHDR with width/height at offsets 16 and 20.
	if (data.length > 24 && data[0] === 0x89 && data[1] === 0x50 && data[2] === 0x4e) {
		const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
		return Math.max(view.getUint32(16), view.getUint32(20));
	}
	// JPEG: walk the segment chain looking for a start-of-frame marker.
	if (data.length > 4 && data[0] === 0xff && data[1] === 0xd8) {
		let offset = 2;
		while (offset + 9 < data.length) {
			if (data[offset] !== 0xff) {
				offset++;
				continue;
			}
			const marker = data[offset + 1];
			const length = (data[offset + 2] << 8) | data[offset + 3];
			if (
				marker >= 0xc0 &&
				marker <= 0xcf &&
				marker !== 0xc4 &&
				marker !== 0xc8 &&
				marker !== 0xcc
			) {
				const height = (data[offset + 5] << 8) | data[offset + 6];
				const width = (data[offset + 7] << 8) | data[offset + 8];
				return Math.max(width, height);
			}
			offset += 2 + length;
		}
	}
	return 0;
}

function mimeFor(path: string): string {
	const ext = path.toLowerCase().split('.').pop() ?? '';
	if (ext === 'png') return 'image/png';
	if (ext === 'jpg' || ext === 'jpeg') return 'image/jpeg';
	if (ext === 'webp') return 'image/webp';
	if (ext === 'json') return 'application/json';
	return 'application/octet-stream';
}

/**
 * Unpack a `.zip` into a source the renderer can use.
 *
 * The archive is walked once. Every entry becomes an object URL so the library
 * can fetch it by path, and the settings JSON is parsed here so the texture
 * budget can be checked before any GPU work happens.
 */
export async function filesFromZip(
	zip: File | Blob,
	name = 'model.zip'
): Promise<Live2DModelSource> {
	if (zip.size > LIVE2D_MAX_BYTES) {
		throw new Error(
			`This archive is ${(zip.size / 1024 / 1024).toFixed(1)} MiB. Live2D models must be under ${LIVE2D_MAX_BYTES / 1024 / 1024} MiB.`
		);
	}

	const { unzipSync } = await import('fflate');
	const bytes = new Uint8Array(await zip.arrayBuffer());

	let entries: Record<string, Uint8Array>;
	try {
		entries = unzipSync(bytes, {
			filter: (file) => !file.name.endsWith('/') && !isJunk(file.name)
		});
	} catch {
		throw new Error(`"${name}" is not a readable zip archive.`);
	}

	const paths = Object.keys(entries);
	const settingsPath = paths.find((path) => /\.model3?\.json$/i.test(path));
	if (!settingsPath) {
		throw new Error('No .model3.json found. Pick a Live2D model archive.');
	}

	let json: Record<string, any>;
	try {
		json = JSON.parse(new TextDecoder().decode(entries[settingsPath]));
	} catch {
		throw new Error(`${settingsPath} is not valid JSON.`);
	}

	const refs = json?.FileReferences;
	const mocRef = refs?.Moc;
	const textureRefs: string[] = Array.isArray(refs?.Textures) ? refs.Textures : [];
	if (typeof mocRef !== 'string' || textureRefs.length === 0) {
		throw new Error('This model3.json has no Moc or no Textures; it is not a Cubism 4 model.');
	}

	// Fail before the GPU is involved if a declared resource is missing. Reduced
	// textures are checked too, so a broken fallback is caught at upload time rather
	// than at load time on a low-end device.
	const reducedForCheck: string[] = Array.isArray(refs?.TexturesReduced)
		? refs.TexturesReduced
		: [];
	const required = [mocRef, ...textureRefs, ...reducedForCheck, refs?.Physics, refs?.Pose].filter(
		(ref): ref is string => typeof ref === 'string'
	);
	const missing = required.filter((ref) => !entries[resolveRelative(settingsPath, ref)]);
	if (missing.length) {
		throw new Error(`This model archive is incomplete: missing ${missing.join(', ')}.`);
	}

	// Pick the texture set this device can actually upload, before any GPU work.
	//
	// A model may ship more than one set: `Textures` is full quality and
	// `TexturesReduced` is a smaller fallback. The measured limit decides, because
	// spec sheets are not reliable -- a Mali-G72 that should report 8192 reported
	// 4096 on the device that reported this bug, and the 8192 model was rejected.
	const reducedRefs: string[] = Array.isArray(refs?.TexturesReduced) ? refs.TexturesReduced : [];
	const limit = maxTextureSize();

	const edgeOf = (list: string[]): number => {
		let edge = 0;
		for (const ref of list) {
			const data = entries[resolveRelative(settingsPath, ref)];
			if (!data) continue;
			edge = Math.max(edge, imageEdge(data));
		}
		return edge;
	};

	let chosenRefs = textureRefs;
	let textureEdge = edgeOf(textureRefs);
	let textureReduced = false;

	if (reducedRefs.length > 0) {
		const reducedEdge = edgeOf(reducedRefs);
		// Only switch when the full set does not fit AND the reduced one does.
		// When the limit is unknown, keep the full set: guessing "small" would
		// degrade quality on capable hardware, and checkTextureBudget already
		// warns when it cannot measure.
		if (limit !== null && textureEdge > limit && reducedEdge <= limit) {
			chosenRefs = reducedRefs;
			textureEdge = reducedEdge;
			textureReduced = true;
			console.info(
				`[avatar] textures reduced ${edgeOf(textureRefs)}px -> ${reducedEdge}px ` +
					`(this device allows ${limit}px)`
			);
		} else if (limit !== null && textureEdge > limit && reducedEdge > limit) {
			console.warn(
				`[avatar] neither texture set fits: full ${textureEdge}px, ` +
					`reduced ${reducedEdge}px, device ${limit}px`
			);
		}
	}

	// Point the settings at the chosen set. Done on a shallow copy so the parsed
	// archive JSON is not mutated, keeping `dispose()` and re-reads predictable.
	if (textureReduced) {
		json = {
			...json,
			FileReferences: { ...refs, Textures: chosenRefs }
		};
	}

	const urls = new Map<string, string>();
	for (const path of paths) {
		urls.set(path, URL.createObjectURL(new Blob([entries[path]], { type: mimeFor(path) })));
	}

	let released = false;
	const dispose = () => {
		if (released) return;
		released = true;
		for (const url of urls.values()) URL.revokeObjectURL(url);
		urls.clear();
	};

	return {
		settingsPath,
		json,
		textureEdge,
		textureReduced,
		resolve: (ref) => {
			const resolved = resolveRelative(settingsPath, ref);
			const url = urls.get(resolved);
			if (!url) throw new Error(`Cannot find "${ref}" in this archive.`);
			return url;
		},
		dispose
	};
}

declare global {
	interface Window {
		Live2DCubismCore?: unknown;
	}
}
