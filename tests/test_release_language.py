"""English fallback with only the release catalog available; no live services."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tabus import lang


class ReleaseLanguage(unittest.TestCase):
    def test_english_only_catalog(self):
        english = Path(lang._LANG_DIR, "en.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "en.json").write_text(english, encoding="utf-8")
            with patch.object(lang, "_LANG_DIR", directory), patch.object(lang, "_CACHE", {}):
                for selected in ("", "ko", "zz", "en"):
                    with self.subTest(language=selected), patch.dict(os.environ, {"TABC_LANG": selected}):
                        self.assertEqual(lang.t("doorbell.ring", unread=2, who="alice"),
                                         "[dm] 2 unread · from: alice")
                        self.assertEqual(lang.t("doorbell.ring_tac", tac="planning", unread=1, who="bob"),
                                         "[tac] planning · 1 unread · from: bob")
                self.assertEqual(lang.t("missing", lang="ko"), "missing")
                self.assertEqual(lang.t("doorbell.ring", lang="ko"),
                                 json.loads(english)["doorbell.ring"])


if __name__ == "__main__":
    unittest.main()
