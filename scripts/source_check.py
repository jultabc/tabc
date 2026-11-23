#!/usr/bin/env python3
"""source_check — what a reader sees that a renderer does not.

`link_check` looks between files. `doc_check` looks inside one page's markup.
This looks at what the words say: an internal name, an internal issue number,
or a figure that was true when it was written and is not checkable now.

Usage:  python3 scripts/source_check.py [paths...]      (default: the repo)

These are release hazards rather than defects. A page renders correctly with
all three in it, and the tests pass. What they cost is paid after publication,
when a name cannot be recalled and a reader has no way to tell whether a
number still holds.

🔴 On finding names: the naive approach is a list of names matched anywhere,
and it does not work. Short names are substrings of ordinary words -- in a
Korean codebase a one-syllable name appears inside dozens of common words, so
matching it directly buries twenty real hits under hundreds of false ones, and
a tool that cries wolf gets switched off.

So the search is two-stage. First find the *place* an attribution lives: a
parenthesis containing a date or an issue number, which is the shape these
take. Then look for names only inside that. Ordinary prose never enters the
comparison, and a one-syllable name is unambiguous once the context is fixed.

A date alone is not a finding. Recording when an observation was taken is what
keeps it honest later; recording who took it duplicates what the commit
already knows, and only the comment can go stale.

🔴 The name list is the weak part and cannot be otherwise: a list written by
hand omits somebody. It is a second pass over places the issue-number check
already flags, not the primary net. Adding a name here is cheap; relying on
this file to be complete is not safe.
"""

import hashlib
import os
import pathlib
import re
import subprocess
import sys

# 🔴 HTML is read too. Leaving it out was not a decision — the first version
#    thought of this as a source check, and a published page carries the same
#    names, numbers and paths as a comment does. A rename that reached the code
#    and not the guide is exactly what this missed.

# Report shape. One place each, so the report cannot disagree with itself.
EXCERPT_CHARS = 52          # characters of a finding printed on its line
ROWS_SHOWN = 6              # findings printed per kind; the rest are counted
ATTRIBUTION_MAX_CHARS = 60  # a credit is short; a longer parenthesis is prose
REVIEW_ID_HEX = 8           # the leading characters of a message or commit id
NAME_DIGEST_HEX = 16        # hex characters of sha256 kept per name in NAME_DIGESTS
# Characters of context kept (before, after) a match, so the excerpt shows what it
# is attached to: an issue number needs the words before it, a path the folder
# names around it, a figure the noun after it.
CONTEXT = {"issue": (28, 14), "path": (22, 14), "figure": (24, 10), "credit": (12, 10)}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv"}
BINARY_SUFFIX = (".jar", ".gif", ".png", ".jpg", ".jpeg", ".ico", ".webp", ".pdf", ".zip", ".gz", ".whl")

# A parenthesis carrying a date or an issue number: where an attribution sits.
ATTRIBUTION = re.compile(r"\(([^()\n]{0,%d})\)" % ATTRIBUTION_MAX_CHARS)
STAMP = re.compile(r"20\d\d-\d\d-\d\d|#\d{1,4}\b")

# Names are held as digests, not written out: the list of who works on this
# project is not something a public file should carry. A word is a name when
# the first NAME_DIGEST_HEX hex characters of sha256(word.lower()) are in this set. To add a
# name, add its digest; for a one-off run, TABC_SOURCE_CHECK_NAMES (comma list)
# adds names without editing the file.
NAME_DIGESTS = frozenset({
    "0075870f97c892ba",
    "02fc885b61db44a8",
    "084a4bd8972f8d49",
    "167728dc60e9efc6",
    "2cc4473ea6bd95fc",
    "31611159e7e6ff78",
    "34707c3f40dfa20c",
    "3f73ef1623ee713d",
    "4982030d5297756a",
    "4e04e3938cc9c67e",
    "4f682b71153ffa91",
    "57cda594148cb40c",
    "637bfe7928f524a3",
    "665377dc8a2b154a",
    "6b2858fc96a3e0ff",
    "778c553efa00d3c4",
    "787a6732f255f31a",
    "7dd887dc71b51e81",
    "9871e84da89f2271",
    "b260539511e16996",
    "bfc978e39c7af4d2",
    "c34045c1a1db8d1b",
    "dec8530634613c5c",
    "dfedd225bb659d50",
    "ec4f2dbb3b140095",
    "ffd3d21b48469c85",
})
_EXTRA = {w.strip().lower() for w in os.environ.get("TABC_SOURCE_CHECK_NAMES", "").split(",") if w.strip()}


def is_name(word):
    w = word.lower()
    return w in _EXTRA or hashlib.sha256(w.encode()).hexdigest()[:NAME_DIGEST_HEX] in NAME_DIGESTS


# A review reference: the start of a message or commit id. The id alone is common
# in code; a name next to it is what makes it ours.
REVIEW_ID = re.compile(r"\b[0-9a-f]{%d}\b" % REVIEW_ID_HEX)
WORD = re.compile(r"[A-Za-z]+|[\uac00-\ud7a3]+")
# Words that only ever sit beside a credit, so "(<name> review)" or
# "(<name>'s finding)" counts like "(<name>)" alone.
CREDIT_WORDS = {"review", "reviewed", "found", "finding", "findings", "contract", "criteria", "against", "and", "s"}
# A credit anywhere on a line: "<name> <id>", "<name> found", "<name>'s finding",
# "per <name>'s ...", "<name> and <name> reproduced", "<name>, <date>:".
LINE_SHAPES = (
    re.compile(r"\b([A-Za-z]+)(?:'s)?\s+(?:[0-9a-f]{%d}\b|found|finding|findings|review(?:ed)?|reproduced|spotted|noticed|caught|CR|criteria|contract)\b" % REVIEW_ID_HEX),
    re.compile(r"\b([A-Za-z]+)'s\b"),
    re.compile(r"\b([A-Za-z]+)\s+and\s+[A-Za-z]+\s+(?:found|reproduced|sent)\b"),
    re.compile(r"\b([A-Za-z]+),\s+20\d\d-\d\d-\d\d"),
)


def credit_in_parenthesis(inner):
    """A parenthesis credits someone when it names them and is otherwise a date,
    an id, short labels (M3) or credit words — not a sentence that mentions a name."""
    words = WORD.findall(inner)
    if not any(is_name(w) for w in words):
        return False
    if STAMP.search(inner) or REVIEW_ID.search(inner):
        return True
    return all(is_name(w) or w.lower() in CREDIT_WORDS or len(w) == 1 for w in words)


# An internal issue number. `#s2` and `#fff` do not match: those are an anchor
# and a colour, and both carry a letter.
ISSUE = re.compile(r"(?<![\w&])#\d{1,4}\b")

# Counting something of ours, which changes. `32 bytes` and `status 200` do not
# match: a key length and a status code are fixed by a specification, not by
# how much data we happen to hold.
COUNTED = (
    "nodes", "messages", "rows", "files", "lines", "tokens", "tacs",
    "deliveries", "keys", "senders", "commits", '\ub178\ub4dc', '\uba54\uc2dc\uc9c0', '\ud1b5',
)
# Korean puts the noun before the number: the noun, a particle, then the count
# and its unit — where English puts the number first. A pattern reading only
# number-then-noun catches the English comment and misses the Korean one beside
# it, so both orders are matched.
#
# 🔴 The examples that belonged here were themselves findings: writing the
#    Korean and English forms out with a number in them made this file report
#    two counted figures, in its own explanation. Describing the shape instead
#    of exhibiting it is the same rule this check exists to enforce.
COUNTED_KO = ('\ub178\ub4dc', '\uba54\uc2dc\uc9c0', '\ud589', '\uc904', '\ud1b5', '\uac74', '\ud30c\uc77c', '\ud0a4', '\ub178\ub4dc\ud0a4')
UNIT_KO = '\uac1c|\uac74|\ud1b5|\uc904|\uba85|\uac00\uc9c0'
FIGURE = re.compile(
    '(?:\\b(?:is|are|has|have|of|was|were)\\s+)?\\b(\\d{2,7})\\s*(?:\uac1c|\uac74)?\\s*('
    + "|".join(COUNTED)
    + r")\b"
    + r"|(" + "|".join(COUNTED_KO) + ')[\uac00\uc774\uc740\ub294\uc744\ub97c\uc758]?\\s*(\\d{2,7})\\s*(?:' + UNIT_KO + r")"
)


# A path under someone's home directory, or one naming a home that is not this
# project's. `~/.tabc` is where this installation keeps state and belongs; a
# path naming any other directory under a home is a setting that happened to
# work on the machine it was written on, and reaches nobody else.
#
# 🔴 This one is not about disclosure so much as breakage: a reader who
#    installs from a release gets a path that does not exist and a script that
#    fails on the line that uses it.
# 🔴 The exclusions are a hand-written list, and that is acceptable here where
#    it would not be for names. A missing name means a leak goes out; a missing
#    convention here means one noisy line a reader dismisses. The failure runs
#    the safe way, so the list can be short and grow when something trips it.
HOME_CONVENTIONS = ("bin", ".local", ".config", ".cache", ".ssh", ".claude", ".tabc")
_EXC = "|".join(x.replace(".", r"\.") for x in HOME_CONVENTIONS)
# 🔴 `$HOME/` counts as much as `~/`. A shell recipe writes the first because a
#    tilde does not expand inside double quotes, so a check that only knew the
#    tilde read every such recipe and saw nothing.
LOCAL_PATH = re.compile(
    r"/(?:Users|home)/[a-z][\w.-]*"
    r"|(?:~|\$HOME)/(?!(?:" + _EXC + r")(?:/|\b))[\w.-]+"
)


class TargetError(RuntimeError):
    """The list of files to check could not be made. Nothing was checked."""


def is_checked(f):
    return f.is_file() and not f.name.lower().endswith(BINARY_SUFFIX) and not (SKIP_DIRS & set(f.parts))


def targets(argv):
    """Files to check. Paths given: every non-binary file under them, whatever its
    name (LICENSE has no suffix). No paths: every file git tracks, except binaries.

    🔴 If git cannot list the files, that is a failure, not a reason to walk by
    suffix: a fallback that skipped LICENSE reported 0 findings for a credit in it.
    Outside a git checkout, pass the paths to check."""
    if argv:
        for a in argv:
            p = pathlib.Path(a)
            if p.is_dir():
                yield from sorted(f for f in p.rglob("*") if is_checked(f))
            elif p.is_file():
                yield p
            else:
                raise TargetError(f"no such file or directory: {a}")
        return
    try:
        r = subprocess.run(["git", "ls-files", "-z"], capture_output=True)
    except OSError as e:
        raise TargetError(f"git ls-files could not run ({e}); pass the paths to check") from None
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip()[:EXCERPT_CHARS]
        raise TargetError(f"git ls-files exited {r.returncode} ({msg}); pass the paths to check")
    for name in r.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if name and not name.lower().endswith(BINARY_SUFFIX):
            yield pathlib.Path(name)


def excerpt(s, m, kind):
    before, after = CONTEXT[kind]
    return re.sub(r"\s+", " ", s[max(0, m.start() - before) : m.end() + after]).strip()[:EXCERPT_CHARS]


def scan(path):
    """Findings in one file as (kind, line, excerpt)."""
    try:
        s = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # Not read is not clean: report it, so 0 findings never covers a file nobody saw.
        return [("unreadable", 0, type(e).__name__)]
    out = []
    for m in ATTRIBUTION.finditer(s):
        inner = m.group(1)
        if credit_in_parenthesis(inner):
            out.append(("name", s[: m.start()].count("\n") + 1, m.group(0)[:EXCERPT_CHARS]))
    for shape in LINE_SHAPES:
        for m in shape.finditer(s):
            if is_name(m.group(1)):
                out.append(("name", s[: m.start()].count("\n") + 1, excerpt(s, m, "credit")))
    for m in ISSUE.finditer(s):
        line = s[: m.start()].count("\n") + 1
        out.append(("issue", line, excerpt(s, m, "issue")))
    for m in LOCAL_PATH.finditer(s):
        line = s[: m.start()].count("\n") + 1
        out.append(("path", line, excerpt(s, m, "path")))
    # 🔴 Counted figures are read in source and not in prose — for now, and the
    #    reason matters more than the rule.
    #
    #    The axis is tense, not file type. A number saying what is true *now* goes
    #    stale; a number recording what was measured *then* is a closed quantity
    #    and reporting it is noise. Comments mostly do the first and documents
    #    mostly do the second, so file type stands in for tense here — but it is a
    #    proxy, and a proxy has exceptions. This repository had one: a README
    #    sentence in the present tense, carrying a count that had already gone
    #    stale once.
    #
    #    So this line is not "documents cannot rot." It is "the figure check is
    #    not on for documents yet, and this is where that leaves a gap."
    if path.suffix.lower() != ".html":
        for m in FIGURE.finditer(s):
            line = s[: m.start()].count("\n") + 1
            out.append(("figure", line, excerpt(s, m, "figure")))
    return out


LABEL = {
    "name": "internal name in an attribution — the commit already records who",
    "issue": "internal issue number — meaningless outside this repository",
    "figure": "a counted figure — state how to count it instead",
    "path": "a path under a home directory — nobody else has it",
    "unreadable": "a file that could not be read — not checked",
}

found = {}
files = 0
try:
    chosen = list(targets(sys.argv[1:]))
except TargetError as e:
    print(f"  🔴 not checked: {e}")
    sys.exit(2)
for f in sorted(chosen):
    files += 1
    for kind, line, text in scan(f):
        found.setdefault(kind, []).append((f, line, text))

for kind in ("name", "issue", "figure", "path", "unreadable"):
    rows = found.get(kind, [])
    head = "  OK      " if not rows else "  FOUND   "
    print(f"{head}{LABEL[kind]}: {len(rows)}")
    for f, line, text in rows[:ROWS_SHOWN]:
        print(f"          {f}:{line}  {text}")
    if len(rows) > ROWS_SHOWN:
        print(f"          … and {len(rows) - ROWS_SHOWN} more")

total = sum(len(v) for v in found.values())
print(f"\n  {files} files checked, {total} findings")
if total:
    print("  🔴 A name list written by hand omits somebody. Treat a zero here as")
    print("     'nothing matched the patterns', not as 'nothing is left'.")
sys.exit(1 if total else 0)
