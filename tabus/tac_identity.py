"""Tac identifiers and display names.

A tac is identified by a UUID the server mints. The name is what people type and
read; it is stored exactly as it arrived. Comparison — both "is this name taken"
and "find the tac with this name" — happens on a folded key, never on the stored
text and never on SQL's lower(), which folds ASCII only.
"""

import unicodedata
from uuid import UUID, uuid4

# 🔴 Categories refused in a name: control, format, surrogate, private use and unassigned.
#    The first four are invisible or private, so two names can look identical and compare
#    differently. Unassigned is refused for a different reason: a code point that is
#    unassigned today can be assigned later with a canonical decomposition, and then two
#    names that were different become one. Measured: U+105C9 is unassigned through Unicode
#    14 and decomposes to U+105D2 U+0307 in Unicode 16, so "\U000105c9" and
#    "\U000105d2\u0307" are two names on Python 3.11 and one name on 3.14.
#    Which names this refuses therefore follows the interpreter running the daemon. That
#    is a known difference, and it is the narrower one: it decides a name at the moment it
#    is created, while letting these through would move the difference into the key, where
#    it keeps deciding for names already stored.
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Cn")


def new_id():
    """A new tac identifier: a UUID4 in canonical lowercase hyphenated form."""
    return str(uuid4())


def is_uuid_id(value):
    """True only for the canonical spelling this server writes.

    🔴 Uppercase, braces, a urn:uuid: prefix and the 32-character form are all
    refused. A tac addressed in another spelling would otherwise read as missing.
    """
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def name_key(name):
    """The comparison key for a name: NFC, case-folded, NFC again.

    🔴 Case folding alone lets look-alike names through: NFC "café" and NFD
    "café" fold to different strings. Normalizing first makes them one key.
    Folding can leave the result outside NFC (U+1FD3 is one of 26 such code
    points), so the key is normalized again.

    🔴 NFC, not NFKC. NFKC would also fold full-width "ＴＥＡＭ" into "team",
    which is a different name rather than the same one written differently.
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def check_name(value):
    """Return None for a usable display name, or the reason it is refused."""
    if not isinstance(value, str) or not value.strip():
        return "a tac name must be text with at least one visible character"
    name = value.strip()
    if any(unicodedata.category(char) in INVISIBLE_CATEGORIES for char in name):
        return "a tac name must not contain control, format, surrogate, private-use or unassigned characters"
    if any(char.isspace() and char != " " for char in name):
        return "a tac name must use ordinary spaces between words"
    return None
