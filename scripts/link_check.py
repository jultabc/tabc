#!/usr/bin/env python3
"""link_check — find links that point at nothing.

Walks every HTML and Markdown file in the repository, pulls out href, src, and
markdown-style links, and checks that each target actually exists.

Why a script rather than reading the diff: the count you get by eye is the count
of distinct URLs, and the count that matters is the number of places to fix. The
same broken path appearing three times is three edits. A report of "2 broken
links" once turned out to be five occurrences of three URLs, and the two nobody
had noticed were the ones introduced by moving a file.

Anchors (#section) are stripped before checking, so this verifies the file
exists but not that the anchor within it does.

Usage
  python3 scripts/link_check.py            # whole repository
  python3 scripts/link_check.py docs/      # a subtree
  python3 scripts/link_check.py --strict   # exit 1 when something is broken

Exit code is 0 by default even with findings — this reports, it does not gate.
Pass --strict to use it in CI, where an exit code is the whole point.
"""

import argparse
import os
import re
import sys

HTML_LINK = re.compile(r'(?:href|src)\s*=\s*"([^"]+)"')
MD_LINK = re.compile(r"\]\(([^)\s]+)")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
EXTS = {".html", ".md"}


def is_external(url):
    return url.startswith(("http://", "https://", "mailto:", "data:", "//")) or url.startswith("#")


def walk(roots):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if os.path.splitext(name)[1] in EXTS:
                    yield os.path.join(dirpath, name)


def check(paths):
    """Returns (broken, external) as lists of (file, url) pairs."""
    broken, external = [], []
    for path in walk(paths):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for url in HTML_LINK.findall(text) + MD_LINK.findall(text):
            if is_external(url):
                external.append((path, url))
                continue
            target = os.path.normpath(os.path.join(os.path.dirname(path), url.split("#")[0]))
            if not os.path.exists(target):
                broken.append((path, url))
    return broken, external


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="*", default=["."])
    ap.add_argument("--strict", action="store_true", help="exit 1 if anything is broken")
    ap.add_argument("--external", action="store_true", help="also list external links")
    args = ap.parse_args()

    broken, external = check(args.paths or ["."])

    print(f"broken links: {len(broken)}")
    for path, url in broken:
        print(f"  {path}  ->  {url}")
    if args.external:
        print(f"\nexternal links: {len(external)}")
        for path, url in external:
            print(f"  {path}  ->  {url}")

    return 1 if (args.strict and broken) else 0


if __name__ == "__main__":
    sys.exit(main())
