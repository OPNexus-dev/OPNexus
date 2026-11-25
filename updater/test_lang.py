"""Language of the updater's own texts (updater/lang.py): English unless the locale or OPNEXUS_LANG says German."""
import os
import unittest
from unittest import mock

import ha_agent
import lang


def env(**values):
    return mock.patch.dict(os.environ, {k: v for k, v in values.items()}, clear=True)


class UpdaterLanguage(unittest.TestCase):
    def test_default_and_locales(self):
        with env():
            self.assertEqual(lang.language(), "en")
        with env(LANG="de_DE.UTF-8"):
            self.assertEqual(lang.language(), "de")
        with env(LANG="fr_FR.UTF-8"):
            self.assertEqual(lang.language(), "en")
        with env(LANG="de_DE.UTF-8", OPNEXUS_LANG="en"):
            self.assertEqual(lang.language(), "en")
        with env(LANG="C", OPNEXUS_LANG="de"):
            self.assertEqual(lang.language(), "de")

    def test_t_picks_the_text(self):
        with env(OPNEXUS_LANG="de"):
            self.assertEqual(lang.t("Hallo", "Hello"), "Hallo")
        with env(OPNEXUS_LANG="en"):
            self.assertEqual(lang.t("Hallo", "Hello"), "Hello")

    def test_an_agent_error_is_english_without_a_german_locale(self):
        with env(OPNEXUS_LANG="en"):
            with self.assertRaises(ha_agent.CodeError) as caught:
                ha_agent.decode_code("not-a-code")
        self.assertIn("pairing code", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
