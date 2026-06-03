import os
import tempfile
import pytest
from cryptography.fernet import InvalidToken
from marsnet.node.crypto import CryptoManager


def test_encrypt_decrypt_roundtrip():
    cm = CryptoManager.generate()
    plaintext = b"hello mars"
    ciphertext = cm.encrypt(plaintext)
    assert ciphertext != plaintext
    assert cm.decrypt(ciphertext) == plaintext


def test_different_ciphertexts_same_plaintext():
    cm = CryptoManager.generate()
    a = cm.encrypt(b"data")
    b = cm.encrypt(b"data")
    assert a != b  # Fernet uses random IV


def test_save_and_load_key():
    cm = CryptoManager.generate()
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    try:
        cm.save(path)
        cm2 = CryptoManager.load(path)
        plaintext = b"test payload"
        assert cm2.decrypt(cm.encrypt(plaintext)) == plaintext
    finally:
        os.unlink(path)


def test_wrong_key_raises():
    a = CryptoManager.generate()
    b = CryptoManager.generate()
    with pytest.raises(InvalidToken):
        b.decrypt(a.encrypt(b"secret"))
