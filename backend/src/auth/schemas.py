"""Pydantic request and response models for the auth endpoints.

These are the wire formats fixed by SPEC-2 Section 2.2. They only shape and
validate input and output: the rules themselves live in
:mod:`src.auth.service`, so the request models call
:func:`~src.auth.service.validate_password` instead of restating the policy,
and no response model can carry a password hash.

Import direction: ``schemas`` imports ``service`` (never the other way round),
so there is no cycle (SPEC-2 Section 11).

This module requires ``email-validator`` at runtime, which ``pydantic[email]``
installs (SPEC-2 Section 8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.auth.service import EMAIL_MAX_LENGTH, validate_password

# Login applies no password *policy* (a password outside the signup policy must
# fail as INVALID_CREDENTIALS, not as 422), but the body still needs an upper
# bound so a hostile client cannot make the server work on a megabyte string
# (SPEC-2 Section 3, "Very long inputs").
_LOGIN_PASSWORD_MAX_LENGTH = 1024


class SignupRequest(BaseModel):
    """The body of ``POST /auth/signup`` (SPEC-2 Section 2.2).

    Extra fields are forbidden, so a client that also sends ``"role"`` gets a
    422 and cannot promote itself; new accounts are always ``role = user``
    (SPEC-2 Section 2.3).

    Attributes:
        email: The requested address. Checked for syntax by ``EmailStr``
            (:class:`pydantic.EmailStr`) and bounded to
            :data:`~src.auth.service.EMAIL_MAX_LENGTH` characters before it is
            normalized.
        password: The plain-text password, checked against SPEC-2's policy.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=EMAIL_MAX_LENGTH)
    password: str

    @field_validator("password")
    @classmethod
    def _reject_password_outside_policy(cls, value: str) -> str:
        """Run the service-layer password policy as a Pydantic check.

        Args:
            value: The submitted password.

        Returns:
            str: The unchanged password when it satisfies the policy.

        Raises:
            ValueError: If the password is outside the policy. Pydantic turns a
                ``ValueError`` raised in a validator into a validation error,
                which the API reports as 422 ``VALIDATION_ERROR`` (SPEC-2
                FR-3). Re-raising keeps the policy's own wording and never
                includes the submitted value.
        """
        try:
            validate_password(value)
        except ValueError as error:
            raise ValueError(str(error)) from error
        return value


class LoginRequest(BaseModel):
    """The body of ``POST /auth/login`` (SPEC-2 Section 2.2).

    Deliberately applies no password policy: a password that would be rejected
    at signup (too short, or past bcrypt's 72-byte limit) must still reach
    :func:`~src.auth.service.authenticate`, where it simply fails and the
    answer stays the single ``INVALID_CREDENTIALS`` 401 (SPEC-2 FR-5,
    Section 2.3). The length is bounded only against abuse.

    Extra fields are forbidden.

    Attributes:
        email: The submitted address; same constraints as at signup.
        password: The plain-text password, at most
            :data:`_LOGIN_PASSWORD_MAX_LENGTH` characters.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=EMAIL_MAX_LENGTH)
    password: str = Field(max_length=_LOGIN_PASSWORD_MAX_LENGTH)


class UserOut(BaseModel):
    """The public view of an account (SPEC-2 Section 2.2).

    There is deliberately no ``password_hash`` field.

    Attributes:
        id: The account's primary key.
        email: The stored, normalized address.
        role: The stored role, ``"user"`` or ``"admin"``.
        created_at: Row creation time, in UTC.
    """

    id: int
    email: str
    role: str
    created_at: datetime


class TokenOut(BaseModel):
    """The response body of ``POST /auth/login`` (SPEC-2 Section 2.2).

    Attributes:
        access_token: The signed JWT to send back as
            ``Authorization: Bearer <token>``.
        token_type: Always ``"bearer"``; carried so the Swagger UI
            ``HTTPBearer`` scheme and clients can be generic.
        expires_in: The token's lifetime in seconds.
    """

    access_token: str
    token_type: Literal["bearer"]
    expires_in: int
