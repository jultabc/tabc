#!/usr/bin/env python3
"""tabuslang — a small English-base message catalogue with optional translations.

🔴 t() never raises. The doorbell and the CLI both call it, and a missing key or
   a mismatched format argument returns the best available string instead of an
   exception. A convenience feature must not be able to take down a live path —
   reading a column that did not exist once killed the doorbell exactly that way.

Language comes from TABC_LANG. A key missing in the active language falls back
to the base language, and a key missing everywhere returns the key itself, which
is loud rather than silent.

Catalogues live in locales/*.json as key to string, with {placeholder} formatting.
Release packages include English only; unavailable languages fall back to English.

🔴 The base language is the source. New strings go there first; the other
   language is an overlay and inherits anything it does not define.
"""

import json
import os

# 🔴 A package resource, not user state: the catalogues ship with the code and
#    a user never edits them, so this stays relative to the module.
_LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
_CACHE = {}
BASE_LANG = "en"


def _catalog(lang):
    """Load a catalogue. A missing or malformed file yields an empty dict, which
    falls through to the fallback rather than raising."""
    if lang not in _CACHE:
        try:
            with open(os.path.join(_LANG_DIR, f"{lang}.json"), encoding="utf-8") as f:
                data = json.load(f)
            _CACHE[lang] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _CACHE[lang] = {}
    return _CACHE[lang]


def current_lang():
    """The active language. Read on every call so it can change at runtime."""
    return (os.environ.get("TABC_LANG") or BASE_LANG).strip().lower()


def t(key, lang=None, **kwargs):
    """Resolve a key in the active language, falling back to the base language and
    then to the key itself.

    🔴 A mismatched format argument returns the raw template rather than raising,
       so a caller on a live path stays up."""
    lang = (lang or current_lang()).strip().lower()
    tmpl = _catalog(lang).get(key)
    if tmpl is None and lang != BASE_LANG:
        tmpl = _catalog(BASE_LANG).get(key)  # partial translation: fill from base
    if tmpl is None:
        return key  # in no catalogue: return the key, loudly, not an empty string
    try:
        return tmpl.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return tmpl  # bad format arguments: the template, not a crash
