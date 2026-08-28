from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from collections import defaultdict

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .db_models import StudentCredential, utcnow


class InvalidStudentCredentialsError(PermissionError):
    pass


class PasswordPolicyError(ValueError):
    pass


class StudentAuth:
    """Own student password hashing and credential persistence."""

    MIN_PASSWORD_LENGTH = 8
    MAX_PASSWORD_LENGTH = 128
    SALT_BYTES = 32
    HASH_BYTES = 64
    SCRYPT_N = 2**14
    SCRYPT_R = 8
    SCRYPT_P = 1

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.student_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @classmethod
    def _validate_new_password(cls, password: str) -> None:
        if not cls.MIN_PASSWORD_LENGTH <= len(password) <= cls.MAX_PASSWORD_LENGTH:
            raise PasswordPolicyError("password length must be between 8 and 128 characters")

    @classmethod
    def _derive(cls, password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=cls.SCRYPT_N,
            r=cls.SCRYPT_R,
            p=cls.SCRYPT_P,
            dklen=cls.HASH_BYTES,
        )

    async def _matches(self, credential: StudentCredential, password: str) -> bool:
        if not self.MIN_PASSWORD_LENGTH <= len(password) <= self.MAX_PASSWORD_LENGTH:
            return False
        candidate = self._derive(password, bytes(credential.password_salt))
        return hmac.compare_digest(candidate, bytes(credential.password_hash))

    async def authenticate_or_register(self, student_id: str, password: str) -> None:
        """Authenticate an existing student or atomically claim an unregistered ID."""
        async with self.student_locks[student_id]:
            with self.sessions() as database:
                credential = database.get(StudentCredential, student_id)
            if credential is not None:
                if not await self._matches(credential, password):
                    raise InvalidStudentCredentialsError
                return

            self._validate_new_password(password)
            salt = secrets.token_bytes(self.SALT_BYTES)
            password_hash = self._derive(password, salt)
            try:
                with self.sessions() as database:
                    database.add(StudentCredential(
                        student_id=student_id,
                        password_salt=salt,
                        password_hash=password_hash,
                        created_at=utcnow(),
                        updated_at=utcnow(),
                    ))
                    database.commit()
                return
            except IntegrityError:
                # A different Central process may have won the first-registration race.
                # Its password is authoritative; authenticate against that row.
                pass

            await self.authenticate(student_id, password)

    async def authenticate(self, student_id: str, password: str | None) -> None:
        if password is None:
            raise InvalidStudentCredentialsError
        with self.sessions() as database:
            credential = database.get(StudentCredential, student_id)
        if credential is None or not await self._matches(credential, password):
            raise InvalidStudentCredentialsError

    async def change_password(
        self, student_id: str, old_password: str | None, new_password: str
    ) -> None:
        async with self.student_locks[student_id]:
            await self.authenticate(student_id, old_password)
            self._validate_new_password(new_password)
            salt = secrets.token_bytes(self.SALT_BYTES)
            password_hash = self._derive(new_password, salt)
            with self.sessions() as database:
                credential = database.get(StudentCredential, student_id)
                if credential is None:
                    raise InvalidStudentCredentialsError
                credential.password_salt = salt
                credential.password_hash = password_hash
                credential.updated_at = utcnow()
                database.commit()
