from cryptography.fernet import Fernet

import crypto_utils


def test_encrypt_decrypt_text_roundtrip():
    dek = Fernet.generate_key()
    ciphertext = crypto_utils.encrypt_text(dek, "hola, esto es privado")

    assert crypto_utils.decrypt_text(dek, ciphertext) == "hola, esto es privado"


def test_decrypt_text_with_wrong_key_returns_none():
    dek = Fernet.generate_key()
    other_dek = Fernet.generate_key()
    ciphertext = crypto_utils.encrypt_text(dek, "secreto")

    assert crypto_utils.decrypt_text(other_dek, ciphertext) is None


def test_ciphertext_does_not_contain_plaintext():
    dek = Fernet.generate_key()
    ciphertext = crypto_utils.encrypt_text(dek, "informacion sensible 12345")

    assert b"informacion sensible" not in ciphertext


def test_encrypt_decrypt_bytes_roundtrip():
    dek = Fernet.generate_key()
    data = bytes(range(256))
    ciphertext = crypto_utils.encrypt_bytes(dek, data)

    assert crypto_utils.decrypt_bytes(dek, ciphertext) == data


def test_decrypt_bytes_with_wrong_key_returns_none():
    dek = Fernet.generate_key()
    other_dek = Fernet.generate_key()
    ciphertext = crypto_utils.encrypt_bytes(dek, b"datos binarios de una imagen")

    assert crypto_utils.decrypt_bytes(other_dek, ciphertext) is None


def test_supports_unicode_text():
    dek = Fernet.generate_key()
    text = "áéíóú ñ 日本語 emoji 🔒"
    ciphertext = crypto_utils.encrypt_text(dek, text)

    assert crypto_utils.decrypt_text(dek, ciphertext) == text
