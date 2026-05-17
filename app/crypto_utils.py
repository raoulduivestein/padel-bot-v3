from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_rsa_keypair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key_b64 = base64.b64encode(public_der).decode("ascii")
    return private_pem, public_key_b64


def decrypt_hmac_key(private_pem: str, encrypted_key_b64: str) -> bytes:
    private_key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    encrypted = base64.b64decode(encrypted_key_b64)

    paddings = (
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA1(),
            label=None,
        ),
        padding.PKCS1v15(),
    )
    last_error: Exception | None = None
    for candidate in paddings:
        try:
            return private_key.decrypt(encrypted, candidate)
        except Exception as exc:  # noqa: BLE001 - try known RSA paddings until one matches the app backend.
            last_error = exc
    raise ValueError("Could not decrypt HMAC key with supported RSA paddings") from last_error
