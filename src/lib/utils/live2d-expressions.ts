/**
 * Map an emoji produced by the emoji-generation task onto a Live2D expression.
 *
 * Why this exists. The emoji task already asks an LLM to read the emotion of a
 * reply ("reflect the speaker's likely facial expression through a fitting
 * emoji", see DEFAULT_EMOJI_GENERATION_PROMPT_TEMPLATE in the backend). Until
 * now that signal only ever became a large emoji glyph drawn over the call
 * overlay. The Live2D renderer ignored it completely, so the character's face
 * never moved. This module turns the same signal into an expression name.
 *
 * The mapping is intentionally coarse. The LLM returns one emoji, so the most
 * that can be recovered is a handful of emotional buckets. Pretending otherwise
 * would mean inventing granularity that the input does not carry.
 *
 * Expression names follow the conventions Cubism riggers actually use. The
 * caller is expected to match these names against the model's own
 * `FileReferences.Expressions` and fall back when a name is absent, because a
 * rig is free to name things however it likes.
 */

/** Emotional buckets, ordered from most to least likely to be seen in a chat. */
export const EXPRESSION_MOODS = ['happy', 'sad', 'angry', 'surprised', 'relaxed'] as const;

export type ExpressionMood = (typeof EXPRESSION_MOODS)[number];

/**
 * Emoji -> mood. Grouped by bucket rather than listed as one flat table so the
 * intent of each entry is obvious and a new emoji has an obvious home.
 *
 * This covers the emoji an LLM actually tends to emit for chat replies. It is
 * not, and does not try to be, the whole Unicode set: an unmapped emoji simply
 * yields `null` and the face keeps its current expression, which is the safe
 * direction to fail in.
 */
const MOOD_EMOJI: Record<ExpressionMood, string[]> = {
	happy: [
		'😊',
		'😀',
		'😃',
		'😄',
		'😁',
		'🙂',
		'😆',
		'😍',
		'🥰',
		'😘',
		'😎',
		'🤗',
		'😋',
		'😸',
		'🙌',
		'👏',
		'✨',
		'🎉',
		'💖',
		'❤️',
		'😇',
		'🤩',
		'😌',
		'☺️',
		'😺',
		// 😅 (grinning face with sweat) is a nervous *smile*, which this rig can
		// actually draw: eye-smile plus a mouth-form curve. It is deliberately the
		// only laughter-adjacent emoji here. 😂/🤣 are full laughter and are left
		// unmapped on purpose -- the rig has no open-mouth laugh pose, so mapping
		// them would put a polite smile on a laughing message, which reads worse
		// than no change at all. See the unit test that pins this.
		'😅'
	],
	sad: [
		'😢',
		'😭',
		'😞',
		'😔',
		'😟',
		'🙁',
		'☹️',
		'😿',
		'💔',
		'😥',
		'😰',
		'🥺',
		'😓',
		'😪',
		// Distress and pain read as sadness on a face. 🫤 (face with diagonal
		// mouth) is mild but unambiguously negative.
		'😨',
		'😖',
		'😣',
		'😩',
		'😫',
		'🫤'
	],
	angry: ['😠', '😡', '🤬', '😤', '💢', '👿', '😾', '🗯️'],
	surprised: ['😮', '😲', '😯', '😱', '🤯', '😳', '🙀', '⁉️', '❗', '😵', '🤔', '🧐'],
	relaxed: [
		'😴',
		'🥱',
		'😑',
		'😐',
		'😶',
		'🙃',
		'😏',
		'😉',
		'😜',
		'🤪',
		// Flat, unbothered, or unimpressed. A neutral face is not "happy"; it is
		// the absence of expression, which is what `relaxed` is here for.
		'😒',
		'🙄'
	]
};

const EMOJI_TO_MOOD = new Map<string, ExpressionMood>();
for (const mood of EXPRESSION_MOODS) {
	for (const emoji of MOOD_EMOJI[mood]) {
		// First writer wins, so a glyph listed under two moods keeps the earlier
		// (more common) reading instead of depending on iteration accidents.
		if (!EMOJI_TO_MOOD.has(emoji)) EMOJI_TO_MOOD.set(emoji, mood);
	}
}

/**
 * Variation selectors (U+FE0F) and zero-width joiners change how an emoji is
 * rendered but not what it means, and models emit them inconsistently. Strip
 * them before lookup so `❤️` and `❤` agree.
 */
const stripVariation = (value: string) => value.replace(/[\uFE0E\uFE0F]/g, '');

const NORMALISED = new Map<string, ExpressionMood>();
for (const [emoji, mood] of EMOJI_TO_MOOD) {
	const key = stripVariation(emoji);
	if (!NORMALISED.has(key)) NORMALISED.set(key, mood);
}

/**
 * Resolve an emoji to a mood, or `null` when it carries no recognised emotion.
 *
 * The input is whatever the emoji task returned. It is usually a single glyph,
 * but the endpoint is an LLM call, so a stray space, a variation selector or an
 * unexpected extra character is normal rather than exceptional -- the first
 * recognised emoji in the string wins, and anything unrecognised yields `null`.
 */
export function moodFromEmoji(emoji: string | null | undefined): ExpressionMood | null {
	if (!emoji) return null;
	const cleaned = stripVariation(String(emoji)).trim();
	if (!cleaned) return null;

	const direct = NORMALISED.get(cleaned);
	if (direct) return direct;

	// Fall back to scanning by code point. `Array.from` splits by code point, so
	// a surrogate pair stays one entry; `for..of` on the string would do the same,
	// but this keeps the substring available for the lookup.
	const points = Array.from(cleaned);
	for (const point of points) {
		const mood = NORMALISED.get(point);
		if (mood) return mood;
	}

	// Multi-code-point emoji (flags, skin tones, family ZWJ sequences) are not
	// worth reconstructing here: none of them are chat-reply emotions.
	return null;
}

/**
 * Pick the expression name to use for a mood.
 *
 * Rigs name expressions inconsistently -- `happy`, `Happy`, `exp_happy`,
 * `F01` -- so an exact match is tried first, then a case-insensitive one, then
 * a loose containment match. Returns `null` when the model has no expression
 * that plausibly represents this mood, which the caller should treat as "leave
 * the face alone" rather than as an error.
 */
export function resolveExpressionName(
	mood: ExpressionMood,
	available: readonly string[]
): string | null {
	if (!available.length) return null;

	if (available.includes(mood)) return mood;

	const lower = available.map((name) => name.toLowerCase());
	const exactLower = lower.indexOf(mood);
	if (exactLower !== -1) return available[exactLower];

	// `exp_happy`, `happy_01`, `HappyExpression` all read as the same intent.
	// Prefer the shortest match so `happy` beats `happy_extra_long_name`.
	let best: string | null = null;
	for (let i = 0; i < lower.length; i++) {
		if (lower[i].includes(mood)) {
			if (best === null || available[i].length < best.length) best = available[i];
		}
	}
	return best;
}
