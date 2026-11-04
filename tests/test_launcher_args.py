"""The launcher may load .env only for an effective real-model mode."""
import unittest

from travel_agent.__main__ import wants_env


class WantsEnvTests(unittest.TestCase):
    def test_offline_is_the_default_and_does_not_load_env(self):
        self.assertFalse(wants_env([]))
        self.assertFalse(wants_env(["--port", "8765"]))
        self.assertFalse(wants_env(["--mode", "offline"]))

    def test_deepseek_mode_loads_env_in_both_spellings(self):
        self.assertTrue(wants_env(["--mode", "deepseek"]))
        self.assertTrue(wants_env(["--mode=deepseek"]))
        self.assertTrue(wants_env(["--mode", "deepseek", "--port", "9000"]))

    def test_other_arguments_are_not_mistaken_for_the_mode(self):
        self.assertFalse(wants_env(["--mode", "offline", "--data", "deepseek"]))
        self.assertFalse(wants_env(["--data", "C:/stores/deepseek", "--mode", "offline"]))
        self.assertFalse(wants_env(["--mode", "deepseek", "--mode", "offline"]))
        self.assertFalse(wants_env(["--mode=deepseek", "--mode=offline"]))
        self.assertFalse(wants_env(["--mode"]))

    def test_mode_is_case_sensitive_like_the_node_host(self):
        self.assertFalse(wants_env(["--mode", "DeepSeek"]))


if __name__ == '__main__':
    unittest.main()
