#!/usr/bin/env python3
"""doc_check — check an HTML page for the defects that survive a read-through.

Nine checks: the three theme layers, an explicit body background, colour
literals written outside the token set, token definitions whose value was
damaged, var() references with no definition, tags left unbalanced, and
links pointing at nothing.

Usage:  python3 scripts/doc_check.py <page>.html

Every check here exists because something got through review without it.
That history is kept in the docstrings rather than a changelog, because the
reason a check exists is what tells you whether it still earns its place.

Counting is not judging. A naive scan for colour literals reports every
token definition and every issue number in the prose, so a clean page can
show dozens of "violations" and a reader learns to ignore the tool. The
scans below narrow to the two places a colour can actually take effect --
inside <style>, and in a style="..." attribute -- and subtract the
definitions. On a set of pages that scanned as 30 to 74 hits each, the real
count was zero every time.

What this does not look at: fill= and stroke= on SVG elements. There are
none in this repository today. Adding them means widening this script.
"""

import re, sys, pathlib


def token_values(s):
    """Colour values that are defined as tokens.

    A swatch showing a palette repeats its own token value, and that is the
    point of a swatch. Without this exception a page could not document its
    own colours. A swatch that drifts from the token still gets caught.
    """
    vals = set()
    for m in re.finditer(r"--[\w-]+\s*:\s*(#[0-9a-fA-F]{3,8})\b", s):
        vals.add(m.group(1).lower())
    return vals


def literal_colors(path):
    s = pathlib.Path(path).read_text(encoding="utf-8")
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", s, re.S))
    inline = "\n".join(re.findall(r'style="([^"]*)"', s))
    known = token_values(s)
    hits = []
    for chunk, where in ((css, "style"), (inline, "inline")):
        # A token definition is the thing that should be there. Not a hit.
        cleaned = re.sub(r"--[\w-]+\s*:[^;}]*", "", chunk)
        for m in re.finditer(r"#[0-9a-fA-F]{3,8}\b", cleaned):
            if m.group(0).lower() in known:
                continue
            line = re.sub(
                r"\s+", " ", cleaned[max(0, m.start() - 40) : m.end()]
            ).strip()
            hits.append((where, line[-46:]))
    return hits


def broken_tokens(s):
    """Token definitions whose value no longer looks like a colour.

    A wide substitution can eat the value it was not aiming at. Deleting
    issue numbers with a #\\d{3} pattern turned `--bg: #131519` into
    `--bg: 519`, and the colour checks stayed green because they only look
    outside token definitions. This check looks inside them.
    """
    bad = []
    for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;}]+)", s):
        name, val = m.group(1), m.group(2).strip()
        if name in ("--sans", "--mono", "--shadow"):
            continue
        if re.fullmatch(r"[0-9a-fA-F]{2,8}", val):  # the # was eaten
            bad.append((name, val))
    return bad


def undefined_vars(s):
    """var() references with no matching definition -- that rule does nothing."""
    used = set(re.findall(r"var\((--[\w-]+)\)", s))
    defined = set(re.findall(r"(--[\w-]+)\s*:", s))
    return sorted(used - defined)


def unclosed_tags(s):
    """Tags whose opening and closing do not pair up.

    By order, not by count. A regex that removed links once left three
    opening <a> tags behind and deleted their closers; the totals still
    matched because two orphaned closers elsewhere cancelled them out. A
    count comparison cannot see that. A depth walk can.
    """
    bad = []
    for tag in ("a", "code", "b", "strong", "span", "div", "table", "pre"):
        depth = 0
        orphan_close = 0
        # `<b` also matches `<br`. Require that no name character follows.
        for m in re.finditer(rf"<{tag}(?![a-zA-Z-])|</{tag}\s*>", s):
            if m.group(0).startswith("</"):
                depth -= 1
                if depth < 0:
                    orphan_close += 1
                    depth = 0
            else:
                depth += 1
        if depth or orphan_close:
            bad.append((tag, depth, orphan_close))
    return bad


def dead_links(path, s):
    """Anchors and relative paths that point at nothing."""
    base = pathlib.Path(path).parent
    # HTML5 allows unquoted attribute values. A generator writing
    # <h2 id=deliveries> once made this report eleven working links as dead.
    ids = (
        set(re.findall(r'id="([^"]+)"', s))
        | set(re.findall(r"id='([^']+)'", s))
        | set(re.findall(r"id=([A-Za-z][\w:.-]*)", s))
    )
    dead = [a for a in re.findall(r'href="#([^"]+)"', s) if a not in ids]
    for href in re.findall(r'href="([^"#][^"]*)"', s):
        if href.startswith(("http://", "https://", "mailto:")):
            continue
        if not (base / href).exists():
            dead.append(href)
    return dead


def theme_layers(s):
    """The three states a reader can be in: default, system setting, explicit.

    A page that defines its colours in only one of them renders one theme's
    text on the other theme's ground for whoever is in the missing state.

    This checks that the blocks exist, not that each one redefines the
    tokens. It also cannot tell a page that deliberately commits to a single
    theme from a page that forgot one, so such a page reports here. A reader
    makes that call.
    """
    return {
        "default (:root)": bool(re.search(r":root\s*\{", s)),
        "system (prefers-color-scheme)": "prefers-color-scheme" in s,
        "explicit ([data-theme=dark])": bool(re.search(r"\[data-theme=[\"']?dark", s)),
    }


def body_background(s):
    """A transparent body borrows the host's ground and can bury the text."""
    m = re.search(r"body\s*\{[^}]*\}", s, re.S)
    return bool(m and "background" in m.group(0))


fail = 0
for f in sys.argv[1:]:
    # These checks read CSS and markup. Pointed at anything else -- a README,
    # a script -- every one of them reports a missing theme and an absent body
    # background, and that reads exactly like a real failure. Skipping is not
    # silent: the file is named, so a mistyped glob is visible rather than
    # buried in nine lines of red.
    if pathlib.Path(f).suffix.lower() not in (".html", ".htm"):
        print(f"{f}\n  SKIP    not an HTML file")
        continue
    src = pathlib.Path(f).read_text(encoding="utf-8")
    print(f)
    for name, ok in theme_layers(src).items():
        print(("  OK      " if ok else "  MISSING ") + "theme -- " + name)
        fail += 0 if ok else 1
    ok = body_background(src)
    print(("  OK      " if ok else "  MISSING ") + "explicit body background")
    fail += 0 if ok else 1
    h = literal_colors(f)
    print(("  OK      " if not h else "  FAIL    ") + f"colour literals outside tokens: {len(h)}")
    for w, l in h[:3]:
        print(f"          [{w}] ...{l}")
    fail += len(h)

    b = broken_tokens(src)
    print(("  OK      " if not b else "  FAIL    ") + f"tokens with a damaged value: {len(b)}")
    for name, val in b[:3]:
        print(f"          {name}: {val}   (missing #?)")
    fail += len(b)

    u = undefined_vars(src)
    print(("  OK      " if not u else "  FAIL    ") + f"var() with no definition: {len(u)}")
    for name in u[:3]:
        print(f"          {name}")
    fail += len(u)

    tg = unclosed_tags(src)
    print(("  OK      " if not tg else "  FAIL    ") + f"tag kinds left unbalanced: {len(tg)}")
    for tag, depth, orph in tg[:3]:
        print(f"          <{tag}> unclosed {depth} - orphan closers {orph}")
    fail += len(tg)

    d = dead_links(f, src)
    print(("  OK      " if not d else "  FAIL    ") + f"links pointing at nothing: {len(d)}")
    for x in d[:3]:
        print(f"          {x}")
    fail += len(d)

sys.exit(1 if fail else 0)
