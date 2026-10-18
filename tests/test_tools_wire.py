"""White-box checks for supplier decoding, money, bounds and literal settings."""
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from travel_agent.supplier_wire import complete_sse, decode_wire, dumps, loads, redact, unpack
from travel_agent.transport_summary import _flight_summary, _train_summary
from travel_agent.settings import load_env


class SupplierWire(unittest.TestCase):
    def test_decimal_precision_survives_json_and_missing_tax_stays_unknown(self):
        row = loads('{"basePrice":123.45010001,"totalTax":null}')
        self.assertEqual(row["basePrice"], Decimal("123.45010001"))
        result = _flight_summary(row, "evidence")
        self.assertEqual(result["base_price_cny"], "123.45010001")
        self.assertIsNone(result["price_cny"])
        self.assertEqual(loads(dumps(row))["basePrice"], "123.45010001")

    def test_non_finite_and_boolean_money_never_become_prices(self):
        for value in (True, "NaN", "Infinity", "-1", None):
            with self.subTest(value=value):
                self.assertIsNone(_flight_summary({"basePrice": value}, "e")["base_price_cny"])
        self.assertEqual(_train_summary({"price": {"售罄": 0, "二等座": "123.45010001"}}, "e")["lowest_price_cny"], "123.45010001")
        for text in ('{"x":NaN}', '{"x":Infinity}'):
            with self.assertRaises(ValueError):
                loads(text)

    def test_split_sse_retains_only_complete_events_and_remainder(self):
        prefix = b'data: {"result":1}\r\n\r\n'
        events, rest = complete_sse(prefix + b'data: {"result":')
        self.assertEqual(events, [{"result": 1}])
        self.assertEqual(rest, b'data: {"result":')
        self.assertEqual(decode_wire(b'data: [DONE]\n\n', True), [])

    def test_unpack_keeps_plain_text_and_rejects_malformed_structured_json(self):
        self.assertEqual(unpack({"content": [{"type": "text", "text": "说明"}]}), [{"text": "说明"}])
        with self.assertRaises(ValueError):
            unpack({"content": [{"type": "text", "text": "{bad"}]})

    def test_redaction_covers_nested_keys_and_unicode_escaped_secrets(self):
        value = {"secret": ["secret", r"\u0073ecret", r"\\u0073ecret"]}
        result = redact(value, ["secret"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertIn("REDACTED", json.dumps(result))

    def test_env_is_literal_allowlisted_and_does_not_override_process(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"DEEPSEEK_MODEL": "existing"}, clear=True):
            path = Path(folder) / "settings.env"
            path.write_text('DEEPSEEK_MODEL=other\nAMAP_API_KEY="$(do-not-execute)"\nUNRELATED_TEST_VARIABLE=bad\n', encoding="utf-8")
            load_env(path)
            self.assertEqual(os.environ["DEEPSEEK_MODEL"], "existing")
            self.assertEqual(os.environ["AMAP_API_KEY"], "$(do-not-execute)")
            self.assertNotIn("UNRELATED_TEST_VARIABLE", os.environ)
