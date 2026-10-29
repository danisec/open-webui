"""Unit tests for provider-footer filtering.

The dangerous failure mode is not "footer survives" -- it is "real content is deleted".
This deployment's own chat history contains dozens of legitimate messages about
Cloudflare's free plan, so most of these tests guard against over-matching.

Run: python3 test-response-filters.py
"""
import importlib.util
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
# Works from the repo (docs/architecture/tests -> backend) and from /tmp where the
# module is copied side by side for a container run.
BACKEND = os.environ.get('OWUI_BACKEND', os.path.abspath(os.path.join(_here, '..', '..', '..', 'backend')))
MOD = os.path.join(BACKEND, 'open_webui', 'utils', 'response_filters.py')
if not os.path.isfile(MOD):
    MOD = os.path.join(BACKEND, 'response_filters.py')
if not os.path.isfile(MOD):
    MOD = os.path.join(_here, 'response_filters.py')

spec = importlib.util.spec_from_file_location('response_filters', MOD)
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)

failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}" + ('' if ok else f'\n         got  = {got!r}\n         want = {want!r}'))


def check_true(name, cond, detail=''):
    global failures
    if not cond:
        failures += 1
        print(f'  FAIL  {name}  {detail}')
    else:
        print(f'  OK    {name}')


BANNER = '\n\n---\nFree plan · https://howtofix.id'
PATTERNS = rf.compile_patterns()

print('=== 1. the exact footer is removed ===')
check('banner only', rf.strip_footers(BANNER, PATTERNS), '')
check('banner after text', rf.strip_footers('Hello there.' + BANNER, PATTERNS), 'Hello there.')
check('banner without rule', rf.strip_footers('Done.\nFree plan · https://howtofix.id', PATTERNS), 'Done.')
check('banner with trailing spaces', rf.strip_footers('Done.' + BANNER + '   ', PATTERNS), 'Done.')
check('single dash rule', rf.strip_footers('Done.\n-\nFree plan · https://howtofix.id', PATTERNS), 'Done.')
check('long dash rule', rf.strip_footers('Done.\n-----\nFree plan · https://howtofix.id', PATTERNS), 'Done.')
check('http scheme', rf.strip_footers('Done.\nFree plan · http://howtofix.id', PATTERNS), 'Done.')
check('bullet separator', rf.strip_footers('Done.\nFree plan • https://howtofix.id', PATTERNS), 'Done.')

print('\n=== 2. legitimate content is NOT touched (the important part) ===')
KEEP = [
    'The Cloudflare Free plan allows 50 users.',
    'Free plan · Cloudflare Zero Trust',
    'On the free plan, rate limits are lower.',
    'I checked the free plan details for you.\n\nLet me know if you need more.',
    'Free plan · https://howtofix.id is the provider.',   # mid-text, not a footer
    'Free plan · https://howtofix.id\n\nAnd here is more text after it.',
    'The plan is free.',
    '',
]
for text in KEEP:
    check(f'kept: {text[:44]!r}', rf.strip_footers(text, PATTERNS), text)

print('\n=== 3. mid-message footer is preserved, end footer removed ===')
combined = 'Free plan · https://howtofix.id is mentioned here.\n\nAnswer body.' + BANNER
check(
    'only the trailing one goes',
    rf.strip_footers(combined, PATTERNS),
    'Free plan · https://howtofix.id is mentioned here.\n\nAnswer body.',
)

print('\n=== 4. no patterns -> no change ===')
check('empty pattern list is a no-op', rf.strip_footers('x' + BANNER, []), 'x' + BANNER)
check('None falls back to defaults', rf.strip_footers('x' + BANNER, None), 'x')

print('\n=== 5. invalid patterns are ignored, not fatal ===')
mixed = rf.compile_patterns(['[invalid(regex', r'\nFree plan.*$'])
check('one valid pattern survives', len(mixed), 1)
check('invalid pattern does not raise', rf.strip_footers('a\nFree plan x', mixed), 'a')

print('\n=== 6. streaming: footer split across chunks ===')
f = rf.FooterStreamFilter(PATTERNS, hold_back=40)
chunks = ['Hello', ' wor', 'ld', '\n\n--', '-\nFree pl', 'an · https://howto', 'fix.id']
out = ''.join(f.feed(c) for c in chunks)
out += f.flush()
check('split footer removed', out, 'Hello world')

print('\n=== 7. streaming: ordinary text is emitted, not delayed forever ===')
f = rf.FooterStreamFilter(PATTERNS, hold_back=40)
out = ''.join(f.feed(c) for c in ['The quick ', 'brown fox ', 'jumps over ', 'the lazy dog.'])
out += f.flush()
check('plain text intact', out, 'The quick brown fox jumps over the lazy dog.')

print('\n=== 8. streaming: a footer that never completes is flushed, not lost ===')
f = rf.FooterStreamFilter(PATTERNS, hold_back=40)
out = ''.join(f.feed(c) for c in ['Answer.', '\n\n---', '\nFree pl'])
out += f.flush()
check_true('partial footer is released on flush', 'Answer.' in out, out)

print('\n=== 9. streaming: hold-back is bounded ===')
f = rf.FooterStreamFilter(PATTERNS, hold_back=40)
total = ''
for _ in range(500):
    total += f.feed('x' * 100)
total += f.flush()
check('no unbounded buffering, all text delivered', len(total), 500 * 100)

print('\n=== 10. streaming: many small feeds equal one big feed ===')
text = 'Some answer text that is long enough to matter.' + BANNER
f1 = rf.FooterStreamFilter(PATTERNS, hold_back=40)
one = f1.feed(text) + f1.flush()
f2 = rf.FooterStreamFilter(PATTERNS, hold_back=40)
many = ''.join(f2.feed(text[i:i + 3]) for i in range(0, len(text), 3)) + f2.flush()
check('chunked == whole', many, one)
check('both removed the footer', many, 'Some answer text that is long enough to matter.')

print('\n=== 10b. regression: blank line between rule and footer ===')
# The live provider emitted "---\n\nFree plan ...", which the old pattern left behind.
for label, text in (
    ('one newline', 'Answer.\n\n---\nFree plan \u00b7 https://howtofix.id'),
    ('blank line', 'Answer.\n\n---\n\nFree plan \u00b7 https://howtofix.id'),
    ('two blank lines', 'Answer.\n\n---\n\n\nFree plan \u00b7 https://howtofix.id'),
    ('no rule', 'Answer.\nFree plan \u00b7 https://howtofix.id'),
):
    check(f'cleaned: {label}', rf.strip_footers(text, PATTERNS), 'Answer.')

print('\n=== 10c. regression: a real markdown rule mid-text is untouched ===')
for text in (
    'Section one.\n\n---\n\nSection two.',
    'Intro\n\n---\n\n## Heading\n\nBody text.',
    'A\n\n---\n\nFree plan comparison table below.',
):
    check(f'preserved: {text[:26]!r}', rf.strip_footers(text, PATTERNS), text)

print('\n=== 11. strip_footers is idempotent ===')
once = rf.strip_footers('Body' + BANNER, PATTERNS)
twice = rf.strip_footers(once, PATTERNS)
check('second pass changes nothing', twice, once)

print()
if failures == 0:
    print('ALL PASS')
else:
    print(f'{failures} FAILED')
    sys.exit(1)
