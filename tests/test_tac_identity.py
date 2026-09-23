#!/usr/bin/env python3
"""Tac identifiers and display names.

Pins:
- new_id writes the canonical lowercase hyphenated UUID; is_uuid_id accepts only that
  spelling, so uppercase, braces, urn:uuid: and the 32-character form are refused.
- name_key folds NFC, case, and NFC again: look-alike names share one key, and folding
  that leaves NFC is normalized back.
- NFC, not NFKC: full-width and half-width names stay different names.
- A name is refused when it is blank or carries control, format, surrogate, private-use
  or unassigned characters, or whitespace other than an ordinary space.
- The name a caller sent is never rewritten here; only the key is folded.
"""

import os
import sys
import unicodedata
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tabus import tac_identity as ti  # noqa: E402

NFC_CAFE = "caf\u00e9"
NFD_CAFE = "cafe\u0301"
NFC_ANGSTROM = "\u00c5ngstr\u00f6m"
NFD_ANGSTROM = "A\u030angstro\u0308m"


class Identifier(unittest.TestCase):
    def test_new_id_is_canonical_and_accepted(self):
        seen = {ti.new_id() for _ in range(50)}
        self.assertEqual(len(seen), 50, "identifiers are not reused")
        for value in seen:
            self.assertTrue(ti.is_uuid_id(value), value)
            self.assertEqual(value, value.lower())
            self.assertEqual(len(value), 36)
            self.assertEqual(value.count("-"), 4)

    def test_only_the_canonical_spelling_is_accepted(self):
        canonical = "0b3f1f2e-8a3c-4d5e-9f10-1a2b3c4d5e6f"
        self.assertTrue(ti.is_uuid_id(canonical))
        # 🔴 Every one of these names the same UUID to Python's parser, and none of them
        #    is what this server writes. Accepting them would make one tac addressable
        #    under several spellings.
        for other in (canonical.upper(), "{%s}" % canonical, "urn:uuid:" + canonical,
                      canonical.replace("-", ""), " " + canonical, canonical + " "):
            self.assertFalse(ti.is_uuid_id(other), other)
        for other in ("", "not-a-uuid", "beacon-alert", None, 7, ["x"]):
            self.assertFalse(ti.is_uuid_id(other), repr(other))


class NameKey(unittest.TestCase):
    def test_look_alike_names_share_one_key(self):
        for a, b in ((NFC_CAFE, NFD_CAFE), (NFC_ANGSTROM, NFD_ANGSTROM)):
            self.assertNotEqual(a, b, "the two spellings differ as text")
            self.assertNotEqual(a.casefold(), b.casefold(), "case folding alone is not enough")
            self.assertEqual(ti.name_key(a), ti.name_key(b))

    def test_case_and_ligatures_fold(self):
        self.assertEqual(ti.name_key("BEACON-ALERT"), ti.name_key("beacon-alert"))
        self.assertEqual(ti.name_key("\u1e9e"), ti.name_key("ss"))  # capital sharp s
        self.assertEqual(ti.name_key("\ufb01le"), ti.name_key("file"))  # fi ligature

    def test_full_width_stays_a_different_name(self):
        # NFKC would fold these together; NFC does not, and that is the decision.
        self.assertNotEqual(ti.name_key("\uff34\uff25\uff21\uff2d"), ti.name_key("team"))

    def test_dotted_capital_i_is_its_own_name(self):
        self.assertNotEqual(ti.name_key("\u0130stanbul"), ti.name_key("istanbul"))

    def test_the_key_is_normalized_after_folding(self):
        # 🔴 U+1FD3 folds into a sequence that is not NFC; without the second pass the
        #    key of a name would depend on which spelling arrived.
        key = ti.name_key("\u1fd3")
        self.assertEqual(key, unicodedata.normalize("NFC", key))
        self.assertEqual(key, ti.name_key("\u0390"))

    def test_a_composed_name_and_its_decomposition_share_one_key(self):
        # 🔴 Folding before normalizing is not enough here: this name has to be normalized
        #    first. woo found the case; a single character cannot show it, it needs a base
        #    with a second combining mark (U+1F80 + U+0308 against its NFD form).
        composed = "\u1f80\u0308"
        decomposed = unicodedata.normalize("NFD", composed)
        self.assertNotEqual(composed, decomposed)
        self.assertNotEqual(unicodedata.normalize("NFC", composed.casefold()),
                            unicodedata.normalize("NFC", decomposed.casefold()),
                            "folding first leaves the two spellings apart")
        self.assertEqual(ti.name_key(composed), ti.name_key(decomposed))

    def test_the_key_does_not_change_the_name(self):
        for name in (NFD_CAFE, "  spaced  ", "BEACON-ALERT"):
            before = name
            ti.name_key(name)
            self.assertEqual(name, before)


class NameRules(unittest.TestCase):
    def test_usable_names(self):
        for name in ("beacon-alert", "tac uuid", NFD_CAFE, "\ud55c\uae00 tac", "a"):
            self.assertIsNone(ti.check_name(name), repr(name))

    def test_blank_names_are_refused(self):
        for name in ("", "   ", "\t", None, 7, ["beacon"]):
            self.assertIsNotNone(ti.check_name(name), repr(name))

    def test_invisible_characters_are_refused(self):
        for char in ("\x07", "\u200b", "\ud800", "\ue000"):
            self.assertIsNotNone(ti.check_name("tac" + char), unicodedata.category(char))

    def test_unassigned_code_points_are_refused(self):
        # 🔴 Refused because assignment can add a canonical decomposition later: U+105C9 is
        #    unassigned through Unicode 14 and decomposes in Unicode 16, so two names on one
        #    version are one name on another. These three are unassigned on 3.9, 3.11 and 3.14
        #    alike, so this test says the same thing on every version we run.
        for char in ("\U000e0000", "\U000fffff"):
            self.assertIsNotNone(ti.check_name("tac" + char), unicodedata.category(char))

    def test_other_whitespace_is_refused(self):
        for char in ("\u00a0", "\u3000", "\n", "\u2028"):
            self.assertIsNotNone(ti.check_name("tac" + char + "name"), repr(char))
        self.assertIsNone(ti.check_name("tac name"))


if __name__ == "__main__":
    unittest.main()
