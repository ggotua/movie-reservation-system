"""Unit tests for the password policy (SPEC-2 Section 5, test 5).

The policy lives in :func:`src.auth.service.validate_password` and counts UTF-8
BYTES, not characters: bcrypt operates on the encoded password, so 24 Georgian
letters (three bytes each) are exactly the 72-byte limit and allowed, while 25
of them are 75 bytes and rejected (SPEC-2 Section 2.3).

No database and no HTTP are involved; the function is called directly.
"""

from __future__ import annotations

import pytest

from src.auth.service import (
    PASSWORD_MAX_BYTES,
    PASSWORD_MIN_LENGTH,
    validate_password,
)

# One Georgian letter (U+10D0) encodes to exactly three bytes in UTF-8, which is
# what makes 24 of them the longest all-Georgian password the policy accepts.
# Written as an escape sequence so this file stays ASCII-only.
_GEORGIAN_LETTER = "\u10d0"

# The two boundary lengths that the byte rule and the character rule disagree
# about: 24 Georgian letters are more characters than 8 but still within the
# byte limit.
_GEORGIAN_THAT_FITS = _GEORGIAN_LETTER * 24
_GEORGIAN_THAT_DOES_NOT_FIT = _GEORGIAN_LETTER * 25


def test_password_policy_boundaries() -> None:
    """Accept 8 characters and 72 bytes; reject 7 characters and 73 bytes.

    The Georgian cases pin the rule to bytes rather than characters: 24 letters
    are accepted although they are 24 characters, and 25 are rejected although
    they are only 25. Counting characters instead would invert both answers.
    """
    # 7 characters: one short of the minimum. Rejected by the character rule.
    with pytest.raises(ValueError, match="at least 8 characters"):
        validate_password("a" * (PASSWORD_MIN_LENGTH - 1))

    # Exactly 8 characters: the smallest accepted password.
    validate_password("a" * PASSWORD_MIN_LENGTH)

    # Exactly 72 bytes: the largest accepted password, and bcrypt's own limit.
    validate_password("a" * PASSWORD_MAX_BYTES)

    # 73 bytes: one over the byte limit, rejected by the byte rule.
    with pytest.raises(ValueError, match="at most 72 bytes"):
        validate_password("a" * (PASSWORD_MAX_BYTES + 1))

    # 24 Georgian letters are exactly 72 bytes, so they are accepted.
    assert len(_GEORGIAN_THAT_FITS.encode("utf-8")) == PASSWORD_MAX_BYTES
    validate_password(_GEORGIAN_THAT_FITS)

    # 25 Georgian letters are 75 bytes, so they are rejected even though the
    # character count is nowhere near the byte limit.
    assert len(_GEORGIAN_THAT_DOES_NOT_FIT.encode("utf-8")) == 75
    with pytest.raises(ValueError, match="at most 72 bytes"):
        validate_password(_GEORGIAN_THAT_DOES_NOT_FIT)
