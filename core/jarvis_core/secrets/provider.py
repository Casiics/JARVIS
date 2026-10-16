"""SecretProvider interface with a SOPS-backed implementation.

Start: SOPS + one age key per domain. An init step decrypts only the caller's own domain file
to tmpfs (/run/jarvis/secrets/<domain>/<key>). Later the provider is swapped for HashiCorp Vault
without changing callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from jarvis_core.contracts.classification import Domain


class SecretAccessError(Exception):
    """Raised when a caller asks for a secret outside its own domain or a secret is missing."""


class SecretProvider(Protocol):
    def get(self, path: str) -> str:
        """Return the secret at '<domain>/<key>'."""
        ...


class UnpackedSopsProvider:
    """Reads secrets that the init step decrypted to tmpfs, scoped to one domain."""

    def __init__(self, domain: Domain, root: Path = Path("/run/jarvis/secrets")) -> None:
        self._domain = domain
        self._root = root

    def get(self, path: str) -> str:
        domain_part, _, key = path.partition("/")
        if domain_part != self._domain.value:
            raise SecretAccessError(f"{self._domain} may not read secrets of {domain_part!r}")
        if not key or "/" in key or key.startswith("."):
            raise SecretAccessError(f"invalid secret key {key!r}")
        file = self._root / self._domain.value / key
        try:
            return file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise SecretAccessError(f"secret {path!r} not found") from exc
