import hashlib
from pathlib import Path
import tempfile
import unittest

from travel_agent.real_tools import RealTools


POLICY_HASH = "14e89ab8c38fa908dd9652cff0ba5310dd491f6d070736c70a27ce418555c49d"


class PolicyContract(unittest.TestCase):
    def test_original_policy_hash_and_each_query_evidence_are_preserved(self):
        tools = RealTools("policy")
        first = tools.execute("get_policy_snapshot", {})
        second = tools.execute("get_policy_snapshot", {})
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["data"]["content_hash"], POLICY_HASH)
        self.assertNotEqual(first["evidence_refs"], second["evidence_refs"])
        self.assertEqual(len(tools.seen), 2)
        self.assertEqual(tools.http_requests, 0)
        self.assertNotIn("experiments", first["data"]["source_ref"])

    def test_missing_or_corrupt_policy_is_explicit_without_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy.md"
            for content in (None, b"not a regulation"):
                if content is not None:
                    path.write_bytes(content)
                result = RealTools("policy", policy_path=path).execute("get_policy_snapshot", {})
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["type"], "PolicyReadError")
                self.assertEqual(result["evidence_refs"], [])

    def test_changed_source_bytes_are_detectable(self):
        tools = RealTools("original")
        with tempfile.TemporaryDirectory() as folder:
            content = tools.policy_path.read_bytes() + b"\n"
            path = Path(folder) / "changed.md"
            path.write_bytes(content)
            result = RealTools("changed", policy_path=path).execute("get_policy_snapshot", {})
        self.assertEqual(result["data"]["content_hash"], hashlib.sha256(content).hexdigest())
        self.assertNotEqual(result["data"]["content_hash"], POLICY_HASH)
