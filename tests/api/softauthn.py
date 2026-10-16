"""A software WebAuthn authenticator (ES256, attestation "none") for tests only."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_AT = 0x40


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftAuthenticator:
    def __init__(self, rp_id: str, origin: str, *, user_verified: bool = True) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.user_verified = user_verified
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.counter = 0

    def _flags(self, extra: int = 0) -> int:
        return FLAG_UP | (FLAG_UV if self.user_verified else 0) | extra

    def _client_data(self, kind: str, challenge_b64: str, origin: str | None) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": challenge_b64,
                "origin": origin or self.origin,
                "crossOrigin": False,
            }
        ).encode()

    def register(self, options: dict[str, Any]) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        cose_key = cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([self._flags(FLAG_AT)])
            + self.counter.to_bytes(4, "big")
            + b"\x00" * 16
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + cose_key
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = self._client_data("webauthn.create", options["challenge"], None)
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation),
            },
            "clientExtensionResults": {},
        }

    def assert_(
        self, options: dict[str, Any], *, origin: str | None = None, rp_id: str | None = None
    ) -> dict[str, Any]:
        self.counter += 1
        auth_data = (
            hashlib.sha256((rp_id or self.rp_id).encode()).digest()
            + bytes([self._flags()])
            + self.counter.to_bytes(4, "big")
        )
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": b64url(b"owner"),
            },
            "clientExtensionResults": {},
        }
