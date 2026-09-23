from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from backend.workbench.credentials import (
    SERVICE_NAME,
    KeyringCredentialStore,
    MemoryCredentialStore,
)


class _Backend:
    def __init__(self, priority):
        self.priority = priority


class CredentialStoreTests(unittest.TestCase):
    def test_memory_store_normalizes_outer_whitespace(self) -> None:
        store = MemoryCredentialStore()
        store.set("provider:test", "  local-secret\n")
        self.assertEqual(store.get("provider:test"), "local-secret")

    def test_keyring_store_rejects_fail_backend_at_startup(self) -> None:
        fake = types.SimpleNamespace(get_keyring=lambda: _Backend(0))
        with (
            patch.dict(sys.modules, {"keyring": fake}),
            self.assertRaisesRegex(RuntimeError, "没有可用"),
        ):
            KeyringCredentialStore()

    def test_keyring_delete_does_not_hide_backend_failure(self) -> None:
        class FakeKeyring:
            @staticmethod
            def get_password(service, reference):
                self.assertEqual((service, reference), (SERVICE_NAME, "provider:test"))
                return "configured"

            @staticmethod
            def delete_password(service, reference):
                del service, reference
                raise OSError("keychain locked")

        store = KeyringCredentialStore.__new__(KeyringCredentialStore)
        store._keyring = FakeKeyring()
        with self.assertRaisesRegex(OSError, "keychain locked"):
            store.delete("provider:test")

    def test_keyring_delete_is_idempotent_when_reference_is_absent(self) -> None:
        calls = []

        class FakeKeyring:
            @staticmethod
            def get_password(service, reference):
                del service, reference

            @staticmethod
            def delete_password(service, reference):
                calls.append((service, reference))

        store = KeyringCredentialStore.__new__(KeyringCredentialStore)
        store._keyring = FakeKeyring()
        store.delete("provider:missing")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
