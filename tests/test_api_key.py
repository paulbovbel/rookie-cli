import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "extract_api_key", Path(__file__).parents[1] / "nix" / "extract-api-key.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ApiKeyTests(unittest.TestCase):
    def test_extract_attribute(self):
        data = b"PE contents\x01\x00\x07API_KEY\x08test-key\x00\x00rest"
        self.assertEqual(module.extract(data), "test-key")

    def test_two_byte_length(self):
        key = b"a" * 128
        data = b"\x01\x00\x07API_KEY\x80\x80" + key + b"\x00\x00"
        self.assertEqual(module.extract(data), key.decode())

    def test_reject_missing_or_ambiguous_attribute(self):
        attribute = b"\x01\x00\x07API_KEY\x03key\x00\x00"
        for data in (b"", attribute * 2):
            with self.subTest(data=data), self.assertRaises(ValueError):
                module.extract(data)

    def test_reject_empty_or_malformed_value(self):
        for value in (b"\x00\x00\x00", b"\x03key", b"\x03a b\x00\x00", b"\xff"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.extract(b"\x01\x00\x07API_KEY" + value)
