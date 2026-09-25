"""Cifrado simetrico del contenido del usuario (mensajes, documentos,
imagenes) con su DEK de sesion. Siempre Fernet (AES-128-CBC + HMAC,
autenticado) de la libreria `cryptography` - nunca cifrado hecho a mano.
Ver ROADMAP.md, punto 0."""

from cryptography.fernet import Fernet, InvalidToken


def encrypt_text(dek: bytes, plaintext: str) -> bytes:
    return Fernet(dek).encrypt(plaintext.encode("utf-8"))


def decrypt_text(dek: bytes, ciphertext: bytes) -> str | None:
    """None si no se puede descifrar (DEK equivocada - por ejemplo,
    contenido de una key_generation anterior a un reset de contraseña, que
    queda huerfano a proposito). Quien llama debe mostrar un aviso, no
    fallar con una excepcion."""
    try:
        return Fernet(dek).decrypt(ciphertext).decode("utf-8")
    except InvalidToken:
        return None


def encrypt_bytes(dek: bytes, data: bytes) -> bytes:
    return Fernet(dek).encrypt(data)


def decrypt_bytes(dek: bytes, ciphertext: bytes) -> bytes | None:
    try:
        return Fernet(dek).decrypt(ciphertext)
    except InvalidToken:
        return None
