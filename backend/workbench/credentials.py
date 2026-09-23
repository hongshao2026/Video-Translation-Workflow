"""Secret storage abstraction.

Provider keys are never stored in SQLite.  A profile only contains an opaque
credential reference.  Production installations use the operating-system
keyring; tests and explicitly ephemeral sessions use the in-memory backend.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Protocol

SERVICE_NAME = "dub-workbench"


class CredentialStore(Protocol):
    persistent: bool

    def set(self, reference: str, secret: str) -> None: ...
    def get(self, reference: str) -> str | None: ...
    def delete(self, reference: str) -> None: ...


class MemoryCredentialStore:
    persistent = False

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = threading.Lock()

    def set(self, reference: str, secret: str) -> None:
        normalized = secret.strip()
        if not normalized:
            raise ValueError("Credential cannot be empty")
        with self._lock:
            self._values[reference] = normalized

    def get(self, reference: str) -> str | None:
        with self._lock:
            return self._values.get(reference)

    def delete(self, reference: str) -> None:
        with self._lock:
            self._values.pop(reference, None)


class KeyringCredentialStore:
    persistent = True

    def __init__(self) -> None:
        import keyring  # type: ignore[import-not-found]

        backend = keyring.get_keyring()
        try:
            priority = float(backend.priority)
        except Exception as exc:
            raise RuntimeError("操作系统密钥链后端不可用") from exc
        if priority <= 0:
            raise RuntimeError("没有可用的操作系统密钥链后端")
        self._keyring = keyring

    def set(self, reference: str, secret: str) -> None:
        normalized = secret.strip()
        if not normalized:
            raise ValueError("Credential cannot be empty")
        self._keyring.set_password(SERVICE_NAME, reference, normalized)

    def get(self, reference: str) -> str | None:
        return self._keyring.get_password(SERVICE_NAME, reference)

    def delete(self, reference: str) -> None:
        if self.get(reference) is None:
            return
        # Do not hide a backend failure: callers must not unlink the profile's
        # credential reference while the secret may still exist in the OS
        # keychain. A concurrent deletion may raise and is safe to retry.
        self._keyring.delete_password(SERVICE_NAME, reference)


@dataclass(slots=True)
class CredentialBroker:
    store: CredentialStore

    @classmethod
    def from_environment(cls) -> CredentialBroker:
        requested = os.environ.get("DUB_CREDENTIAL_BACKEND", "keyring").lower()
        if requested == "memory":
            return cls(MemoryCredentialStore())
        if requested != "keyring":
            raise ValueError("DUB_CREDENTIAL_BACKEND 只能是 keyring 或 memory")
        try:
            return cls(KeyringCredentialStore())
        except Exception as exc:
            raise RuntimeError(
                "操作系统密钥链不可用；请安装 keyring，或仅在临时测试中显式设置 "
                "DUB_CREDENTIAL_BACKEND=memory"
            ) from exc

    @staticmethod
    def reference(profile_id: str) -> str:
        return f"provider:{profile_id}"

    def save(self, profile_id: str, secret: str) -> str:
        reference = self.reference(profile_id)
        self.store.set(reference, secret)
        return reference

    def resolve(self, reference: str | None) -> str | None:
        return self.store.get(reference) if reference else None

    def remove(self, reference: str | None) -> None:
        if reference:
            self.store.delete(reference)

    def status(self, reference: str | None) -> dict[str, object]:
        return {
            "configured": bool(reference and self.resolve(reference)),
            "persistent": bool(self.store.persistent),
            "backend": "os_keyring" if self.store.persistent else "process_memory",
        }
