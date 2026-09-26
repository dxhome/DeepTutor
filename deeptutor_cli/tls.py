"""Local certificate creation for the optional HTTPS frontend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path

import typer

from deeptutor.runtime.home import get_runtime_home


def register(app: typer.Typer) -> None:
    @app.command("create")
    def create(
        host: str = typer.Option(..., "--host", help="LAN IP or DNS name used by browsers."),
        home: Path | None = typer.Option(None, "--home", help="Runtime workspace root."),
        out: Path | None = typer.Option(None, "--out", help="Output directory for PEM files."),
    ) -> None:
        """Create a local CA and an HTTPS certificate for one LAN host."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        name = host.strip()
        if not name or any(char in name for char in "/:@ \\?#"):
            raise typer.BadParameter("Use a hostname or IPv4 address without a scheme or port.")
        try:
            san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(name))
        except ValueError:
            san = x509.DNSName(name)

        output = (out or get_runtime_home(home) / "data" / "user" / "tls").expanduser().resolve()
        paths = {
            "ca_cert": output / "rootCA.pem",
            "server_key": output / "deeptutor-key.pem",
            "server_cert": output / "deeptutor.pem",
        }
        if any(path.exists() for path in paths.values()):
            raise typer.BadParameter(f"TLS files already exist in {output}; choose an empty --out directory.")

        now = datetime.now(timezone.utc)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DeepTutor Local CA")])
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False,
                ), critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )

        server_key = ec.generate_private_key(ec.SECP256R1())
        server_cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name)
            .public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

        def key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
            return key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )

        output.mkdir(parents=True, exist_ok=True)
        contents = {
            "ca_cert": ca_cert.public_bytes(serialization.Encoding.PEM),
            "server_key": key_pem(server_key),
            "server_cert": server_cert.public_bytes(serialization.Encoding.PEM),
        }
        for label, path in paths.items():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(contents[label])

        typer.echo(f"Server certificate: {paths['server_cert']}")
        typer.echo(f"Server private key: {paths['server_key']}")
        typer.echo(f"Install this CA certificate on each client device: {paths['ca_cert']}")
        typer.echo("The local CA signing key was discarded after issuance.")
