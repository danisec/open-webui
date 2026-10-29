"""Validate the filter against the REAL messages in this deployment's database.

Unit tests use synthetic strings. This replays the 219 real messages that contain the
footer, plus a large sample of messages that merely mention "Free plan", and reports:

  * how many would be cleaned (expected: the 219 with a trailing footer)
  * how many would be MODIFIED but should not be (expected: 0)
  * total characters removed

The second number is the one that matters: deleting real user-visible text would be a
regression far worse than leaving a watermark.
"""
import importlib.util
import json
import os
import sqlite3
import sys

_here = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(_here, '..', '..', '..', 'backend', 'open_webui', 'utils', 'response_filters.py')
MOD = os.path.abspath(MOD)
if not os.path.isfile(MOD):
    MOD = os.path.join(_here, 'response_filters.py')

spec = importlib.util.spec_from_file_location('response_filters', MOD)
rf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rf)
PATTERNS = rf.compile_patterns()

DB = os.environ.get('OPENCODE_DB', '/home/dani/.local/share/opencode/opencode.db')
db = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=15)
db.row_factory = sqlite3.Row


def texts_of(data_blob):
    """Yield every assistant text string in one stored message."""
    try:
        d = json.loads(data_blob)
    except Exception:
        return
    for c in d.get('content') or []:
        if isinstance(c, dict) and c.get('type') == 'text':
            t = c.get('text')
            if isinstance(t, str) and t:
                yield t


print('=== A. messages that CONTAIN the footer (should be cleaned) ===')
rows = db.execute(
    "select id, data from session_message where cast(data as text) like '%Free plan · https://howtofix.id%'"
).fetchall()
cleaned = unchanged = removed_chars = 0
samples = []
for r in rows:
    for t in texts_of(r['data']):
        out = rf.strip_footers(t, PATTERNS, source='db')
        if out != t:
            cleaned += 1
            removed_chars += len(t) - len(out)
            if len(samples) < 4:
                samples.append((t[-90:], out[-60:]))
        else:
            unchanged += 1

print(f'  text parts checked : {cleaned + unchanged}')
print(f'  cleaned            : {cleaned}')
print(f'  left unchanged     : {unchanged}  (a text part that mentions it mid-body)')
print(f'  characters removed : {removed_chars}')
for before, after in samples:
    print(f'\n    before tail: {before!r}')
    print(f'    after  tail: {after!r}')

print('\n=== B. messages that merely MENTION "Free plan" (must NOT change) ===')
rows = db.execute(
    "select id, data from session_message where cast(data as text) like '%Free plan%' "
    "and cast(data as text) not like '%Free plan · https://howtofix.id%' limit 3000"
).fetchall()
checked = modified = 0
bad = []
for r in rows:
    for t in texts_of(r['data']):
        checked += 1
        out = rf.strip_footers(t, PATTERNS, source='db-check')
        if out != t:
            modified += 1
            if len(bad) < 5:
                bad.append((t[-160:], out[-120:]))

print(f'  text parts checked : {checked}')
print(f'  wrongly modified   : {modified}')
if modified:
    print('  REGRESSION — these should have been left alone:')
    for before, after in bad:
        print(f'    before: {before!r}')
        print(f'    after : {after!r}')

print('\n=== verdict ===')
if modified == 0:
    print('  SAFE: no legitimate message was altered.')
else:
    print(f'  UNSAFE: {modified} legitimate message(s) would be altered.')
    sys.exit(1)
