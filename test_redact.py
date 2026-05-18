import json
import tempfile
import unittest
from pathlib import Path

from memory_tool import RedactingMemoryTool
from redact import Redactor


class RedactionTests(unittest.TestCase):
    def test_redactor_uses_stable_tokens_and_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "pii" / "vault.json"
            redactor = Redactor(vault)

            text = "Email ankit@example.com or call 415-555-1212 from 123 Main St."
            redacted = redactor.redact(text)

            self.assertNotIn("ankit@example.com", redacted)
            self.assertNotIn("415-555-1212", redacted)
            self.assertNotIn("123 Main St", redacted)
            self.assertIn("<EMAIL_1>", redacted)
            self.assertIn("<PHONE_1>", redacted)
            self.assertIn("<STREET_ADDRESS_1>", redacted)
            self.assertEqual(redactor.restore(redacted), text)

            second = redactor.redact("Use ankit@example.com again.")
            self.assertEqual(second, "Use <EMAIL_1> again.")

            data = json.loads(vault.read_text(encoding="utf-8"))
            self.assertEqual(data["tokens"]["<EMAIL_1>"], "ankit@example.com")

    def test_memory_wrapper_redacts_writes_and_reads(self):
        class FakeMemory:
            def __init__(self):
                self.commands = []

            def to_dict(self):
                return {"name": "memory"}

            def call(self, command):
                self.commands.append(command)
                return "Stored ankit@example.com"

        with tempfile.TemporaryDirectory() as tmp:
            redactor = Redactor(Path(tmp) / "vault.json")
            fake = FakeMemory()
            memory = RedactingMemoryTool(fake, redactor)

            result = memory.call({
                "command": "create",
                "path": "/memories/contact.md",
                "file_text": "Email ankit@example.com",
            })

            self.assertEqual(fake.commands[0]["file_text"], "Email <EMAIL_1>")
            self.assertEqual(result, "Stored <EMAIL_1>")

    def test_memory_wrapper_can_replace_legacy_raw_content(self):
        class FakeMemory:
            def __init__(self):
                self.commands = []

            def to_dict(self):
                return {"name": "memory"}

            def call(self, command):
                self.commands.append(command)
                if command["old_str"] == "Email <EMAIL_1>":
                    raise ValueError("old_str did not appear verbatim")
                assert command["old_str"] == "Email ankit@example.com"
                assert command["new_str"] == "Email <EMAIL_1> and <PHONE_1>"
                return "Email ankit@example.com and 415-555-1212"

        with tempfile.TemporaryDirectory() as tmp:
            redactor = Redactor(Path(tmp) / "vault.json")
            redactor.redact("Email ankit@example.com")
            fake = FakeMemory()
            memory = RedactingMemoryTool(fake, redactor)

            result = memory.call({
                "command": "str_replace",
                "path": "/memories/contact.md",
                "old_str": "Email <EMAIL_1>",
                "new_str": "Email ankit@example.com and 415-555-1212",
            })

            self.assertEqual(len(fake.commands), 2)
            self.assertEqual(result, "Email <EMAIL_1> and <PHONE_1>")


if __name__ == "__main__":
    unittest.main()
