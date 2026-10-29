"""Strip provider-injected attribution footers from model responses.

WHY THIS EXISTS
A provider appends its own advertising to some completions, for example:

    <answer>
    ---
    Free plan · https://howtofix.id

Measured on this deployment: 219 of 36,989 stored messages contained it (0.6%), and
210 of those were tool-call turns, which is why it looks intermittent. It arrives as
ordinary text inside `choices[].message.content` (or a streamed `delta.content`), not
as metadata, so the only way to remove it is to filter the text.

DESIGN RULES -- each one exists because the naive version broke something

1. TRUE END OF MESSAGE ONLY.
   The footer is trailing. A message that discusses a "Free plan" in the middle must
   survive untouched; this deployment's own history has dozens of such messages about
   Cloudflare's free plan. An earlier version compiled with `re.MULTILINE`, which made
   `$` match at every line end and deleted mid-message footers along with their
   context.

2. ANCHORED ON `\\Z`, NOT `$`.
   `$` also matches before a trailing newline, which let a partial footer through.

3. STREAMING WITHHOLDS ONLY WHAT COULD BECOME A FOOTER.
   A footer can be split across chunks. The filter holds back the shortest tail that is
   still a viable footer prefix and releases everything before it. Ordinary prose is
   emitted immediately: an earlier version withheld a fixed 160 characters regardless
   of content, adding latency and -- when `flush()` was missed -- truncating answers.

4. BOUNDED MEMORY.
   The hold-back is a small constant, and the buffer has a hard cap.

5. NEVER RAISES, NEVER DROPS SILENTLY.
   A bad pattern is ignored with a log line; a filter failure passes the frame through.
   When text IS removed, that is logged, because silently deleting model output is
   worse than leaving a watermark.

6. CONFIGURABLE.
   Patterns come from config so another provider can be added without a code change.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

log = logging.getLogger(__name__)

# The footer observed from api.howtofix.id. Anchored to the true end of the text.
DEFAULT_PATTERNS = [
    # A rule line, then the footer. `\s*` between them tolerates a blank line, which
    # the provider sometimes emits (measured: "---\n\nFree plan · ..."). Without it the
    # rule was left behind after stripping.
    r'\s*\n[ \t]*-{1,}[ \t]*\s*\n[ \t]*Free plan[ \t]*[·•\u00b7][ \t]*https?://howtofix\.id[ \t]*\Z',
    r'\s*\n[ \t]*Free plan[ \t]*[·•\u00b7][ \t]*https?://howtofix\.id[ \t]*\Z',
]

# Hard cap on the streaming buffer, so a pathological stream cannot grow it without bound.
_STREAM_BUFFER_LIMIT = 4096


def compile_patterns(patterns: Iterable[str] | None = None) -> list[re.Pattern]:
    """Compile footer patterns, skipping any that are invalid.

    A malformed pattern must not disable the whole filter, and must not raise: this
    runs on the response path where an exception would break chat.

    A trailing `$` is rewritten to `\\Z` and no MULTILINE flag is used, so a match only
    succeeds at the true end of the text.
    """
    compiled: list[re.Pattern] = []
    for raw in patterns if patterns is not None else DEFAULT_PATTERNS:
        if not isinstance(raw, str) or not raw.strip():
            continue
        anchored = re.sub(r'(?<!\\)\$$', r'\\Z', raw)
        try:
            compiled.append(re.compile(anchored))
        except re.error as error:
            log.warning('ignoring invalid response filter pattern %r: %s', raw, error)
    return compiled


def strip_footers(
    text: str,
    patterns: list[re.Pattern] | None = None,
    *,
    source: str = 'response',
) -> str:
    """Remove any configured footer from the END of `text`.

    Returns the text unchanged when nothing matches. Logs when it does remove
    something, so a filter that starts over-matching is visible instead of silent.
    """
    if not isinstance(text, str) or not text:
        return text

    for pattern in patterns if patterns is not None else compile_patterns():
        try:
            new_text, count = pattern.subn('', text)
        except Exception as error:  # never break chat over a filter
            log.warning('response filter failed (%s): %s', source, error)
            continue
        if count:
            log.info(
                'stripped %d provider footer(s) from %s (%d -> %d chars)',
                count,
                source,
                len(text),
                len(new_text),
            )
            text = new_text
    return text


class FooterStreamFilter:
    """Streaming-safe footer stripper.

    Withholds ONLY a tail that could still grow into a footer. Ordinary prose is
    released immediately, so this adds no latency and no truncation risk to normal
    answers. `flush()` must be called on every exit path and releases the remainder.
    """

    # Literal lead-ins a footer can start with.
    _LEADS = ('Free plan', '---')

    # Must be able to contain a WHOLE footer, otherwise the footer is partly released
    # before it is complete and `strip_footers` never sees the finished form. The
    # observed footer is ~44 chars including the blank line before "---", so 64 leaves
    # comfortable slack while still being far smaller than a sentence.
    _MAX_HOLD_BACK = 64

    def __init__(self, patterns: list[re.Pattern] | None = None, hold_back: int | None = None):
        self.patterns = patterns if patterns is not None else compile_patterns()
        self.hold_back = self._MAX_HOLD_BACK if hold_back is None else max(0, hold_back)
        self._buffer = ''
        self._stripped = 0

    def _tail_could_be_footer(self, tail: str) -> bool:
        """True only when `tail` could still become a footer with more input."""
        if not tail:
            return False
        stripped = tail.strip()
        if not stripped:
            # Only whitespace: it may be the blank line before a rule.
            return True
        for lead in self._LEADS:
            if lead.startswith(stripped) or stripped.startswith(lead):
                return True
        return False

    def _footer_block_start(self, buffer: str):
        """Index where a trailing footer-shaped block starts, or None.

        A footer is: optional blank space, a rule line, then the "Free plan" line.
        While that structure is still arriving the whole block must be held, otherwise
        the rule is emitted and the finished footer can no longer be matched.

        Detects PARTIAL anchors too ("Free pl"), because a footer arrives in chunks and
        the first chunks only contain a prefix.
        """
        if not buffer:
            return None

        lowered = buffer.lower()

        # Anchor on a prefix of "free plan" so a partially arrived footer still counts.
        pos = -1
        for probe in ('free plan', 'free pla', 'free pl', 'free p', 'free', 'fre', 'fr'):
            found = lowered.rfind(probe)
            if found != -1:
                pos = found
                break

        if pos != -1:
            head = buffer[:pos]
            # Prefer the rule line immediately before the anchor.
            match = re.search(r'\n[ \t]*-{1,}[ \t]*\n[ \t]*$', head)
            if match:
                return match.start()
            # Otherwise start at the newline that precedes the anchor.
            match = re.search(r'\n[ \t]*$', head)
            return match.start() if match else pos

        # No anchor yet. A rule line may already be complete while the anchor is still
        # arriving ("\n\n---\nF"), so look for a rule followed by OPTIONAL partial
        # anchor text rather than requiring the rule to sit at the very end. Missing
        # this emitted "\n\n---\n" and made the footer unmatchable forever.
        match = re.search(r'\n[ \t]*-{1,}[ \t]*\n[ \t]*(?:[Ff][Rr]?[Ee]?[Ee]?[ \t]*)?$', buffer)
        if match:
            return match.start()

        # A rule still being typed at the very end.
        match = re.search(r'\n[ \t]*-{1,}[ \t]*$', buffer)
        if match:
            return match.start()
        return None

    def feed(self, delta: str) -> str:
        """Add a delta, return the text that is safe to emit right now."""
        if not delta:
            return ''

        self._buffer += delta

        # A complete footer may already be present: strip it before deciding.
        before = self._buffer
        self._buffer = strip_footers(self._buffer, self.patterns, source='stream')
        if self._buffer != before:
            self._stripped += 1

        if len(self._buffer) > _STREAM_BUFFER_LIMIT:
            out = self._buffer[: -self._MAX_HOLD_BACK]
            self._buffer = self._buffer[-self._MAX_HOLD_BACK :]
            return out

        # Decide how much of the tail must stay buffered.
        #
        # If a footer-shaped block is present or partially arrived, hold from the START
        # of that block. Emitting its opening lines makes the footer unmatchable forever
        # -- that is how "---\n" leaked out and the trailing footer then survived.
        #
        # Otherwise hold only a short prefix that could still become a footer, so
        # ordinary prose flows immediately.
        block_start = self._footer_block_start(self._buffer)
        if block_start is not None:
            # A footer is introduced by a blank line; those newlines belong to it.
            while block_start > 0 and self._buffer[block_start - 1] == '\n':
                block_start -= 1
            keep = len(self._buffer) - block_start
        else:
            keep = 0
            limit = min(len(self._buffer), self.hold_back)
            for size in range(1, limit + 1):
                if self._tail_could_be_footer(self._buffer[-size:]):
                    keep = size
                    break

        if keep == 0:
            out, self._buffer = self._buffer, ''
            return out

        out = self._buffer[:-keep]
        self._buffer = self._buffer[-keep:]
        return out

    def flush(self) -> str:
        """Release whatever is left. Must be called on every exit path."""
        before = self._buffer
        remaining = strip_footers(before, self.patterns, source='stream flush')
        self._buffer = ''
        if remaining != before and remaining.endswith('\n'):
            # A footer was removed; its leading blank line may remain. Trim exactly one
            # trailing newline, and only when a footer actually matched.
            remaining = remaining[:-1]
        return remaining
