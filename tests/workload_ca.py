"""A throwaway workload CA for tests: root, intermediate and leaf certificates with SPIFFE IDs.

Writes the same layout pki-issue does (tls.crt with leaf and intermediate, tls.key,
root_ca.crt), so tests exercise the files real consumers read.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


@dataclass
class _Issuer:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate


class WorkloadCa:
    def __init__(self, name: str = "Test Workload CA") -> None:
        now = dt.datetime.now(dt.UTC)
        root_key = ec.generate_private_key(ec.SECP256R1())
        root = (
            x509.CertificateBuilder()
            .subject_name(_name(f"{name} Root"))
            .issuer_name(_name(f"{name} Root"))
            .public_key(root_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False
            )
            .sign(root_key, hashes.SHA256())
        )
        inter_key = ec.generate_private_key(ec.SECP256R1())
        inter = (
            x509.CertificateBuilder()
            .subject_name(_name(f"{name} Intermediate"))
            .issuer_name(root.subject)
            .public_key(inter_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(inter_key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
                critical=False,
            )
            .sign(root_key, hashes.SHA256())
        )
        self.root = root
        self._inter = _Issuer(inter_key, inter)

    def issue(
        self,
        directory: Path,
        domain: str,
        service: str,
        *,
        dns: str | None = None,
        expired: bool = False,
        mode: int = 0o644,
        rotate: bool = False,
    ) -> Path:
        """Write tls.crt, tls.key and root_ca.crt for spiffe://jarvis.home/<domain>/<service>.

        rotate: pki-issue's layout instead, a new version directory behind a `current`
        symlink that is swapped with one rename, which is what Envoy watches for.
        """
        if rotate:
            directory.mkdir(parents=True, exist_ok=True)
            version = f".v{len(list(directory.glob('.v*')))}"
            self.issue(directory / version, domain, service, dns=dns, expired=expired, mode=mode)
            (directory / version).chmod(0o755)
            (directory / "current.new").symlink_to(version)
            (directory / "current.new").replace(directory / "current")
            for name in ("tls.crt", "tls.key", "root_ca.crt"):
                if not (directory / name).is_symlink():
                    (directory / name).symlink_to(f"current/{name}")
            return directory
        now = dt.datetime.now(dt.UTC)
        start, end = (
            (now - dt.timedelta(hours=26), now - dt.timedelta(hours=2))
            if expired
            else (now - dt.timedelta(minutes=5), now + dt.timedelta(hours=24))
        )
        key = ec.generate_private_key(ec.SECP256R1())
        names: list[x509.GeneralName] = [
            x509.UniformResourceIdentifier(f"spiffe://jarvis.home/{domain}/{service}")
        ]
        if dns:
            names.append(x509.DNSName(dns))
        leaf = (
            x509.CertificateBuilder()
            .subject_name(_name(f"{service}.{domain}.jarvis.home"))
            .issuer_name(self._inter.cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(end)
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
                ),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._inter.key.public_key()),
                critical=False,
            )
            .sign(self._inter.key, hashes.SHA256())
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tls.crt").write_bytes(_pem(leaf) + _pem(self._inter.cert))
        (directory / "tls.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        (directory / "root_ca.crt").write_bytes(_pem(self.root))
        for path in directory.iterdir():
            path.chmod(mode)
        return directory
