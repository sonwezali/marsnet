from __future__ import annotations
from cryptography.fernet import Fernet


class CryptoManager:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)
        self._key = key

    def encrypt(self, data: bytes) -> bytes:
        return self._fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        return self._fernet.decrypt(token)

    def save(self, path: str) -> None:
        with open(path, "wb") as f:
            f.write(self._key)

    @classmethod
    def generate(cls) -> CryptoManager:
        return cls(Fernet.generate_key())

    @classmethod
    def load(cls, path: str) -> CryptoManager:
        with open(path, "rb") as f:
            return cls(f.read().strip())  # for possible trailing newlines
