"""The shared SQL definition of a reservation's effective status (SPEC-6 D7).

A hold has no expiry column: whether a stored ``held`` row still counts as held
is derived from ``created_at`` and ``HOLD_MINUTES`` (SPEC-4 D2). The user's
reservation list filters on that *effective* status (SPEC-5 D4) and the admin
list of SPEC-6 needs the identical condition, so the SQL form is defined here
once and imported by both (SPEC-6 D7 and Section 10, DRY check). The Python
form of the same rule stays in
:func:`src.reservations.rules.effective_status`.

This module builds one SQL condition and nothing else: no connection, no
query, no HTTP. ``db`` sits below ``reservations`` in the import graph, so this
module imports only ``core`` (the status vocabulary) and the shared table
definitions in :mod:`src.db.tables`, never a service, schema or router.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ColumnElement, and_, or_

from src.core.enums import (
    DEFAULT_RESERVATION_STATUS,
    RESERVATION_STATUSES,
    ReservationStatus,
)
from src.db.tables import reservations

# The two statuses the derived condition compares, taken from the single
# vocabulary in ``core/enums.py`` (SPEC-1 Section 9) rather than restated as
# literals: a reservation starts as ``held``, and ``RESERVATION_STATUSES``
# declares the four values in the order held, confirmed, cancelled, expired.
_HELD_STATUS: ReservationStatus = DEFAULT_RESERVATION_STATUS
_EXPIRED_STATUS: ReservationStatus = RESERVATION_STATUSES[-1]


def effective_status_condition(
    status: ReservationStatus,
    cutoff: datetime,
) -> ColumnElement[bool]:
    """Build the SQL condition selecting rows of the given effective status.

    This is the single definition of the effective-status filter (SPEC-5 D4,
    SPEC-6 FR-10), shared by the user's reservation list and the admin list. A
    stored ``held`` row at or past ``cutoff`` is selected as ``expired`` while
    the stored row is left unchanged: reads never write (SPEC-4 Section 2.5).

    Args:
        status: The effective status to select: ``held``, ``confirmed``,
            ``cancelled`` or ``expired``.
        cutoff: The hold-expiry instant, timezone-aware, computed once per
            request by :func:`src.reservations.rules.hold_cutoff`. A row counts
            as held only while ``created_at > cutoff``; at exactly ``cutoff``
            it is expired (SPEC-4 FR-5). The ``confirmed`` and ``cancelled``
            comparisons read the stored value and ignore it.

    Returns:
        ColumnElement[bool]: A condition on the ``reservations`` table for use
        in ``WHERE``: ``status = 'held' AND created_at > cutoff`` for ``held``;
        ``status = 'expired' OR (status = 'held' AND created_at <= cutoff)``
        for ``expired``; plain equality with the stored value for ``confirmed``
        and ``cancelled``. A new expression is built on every call; the
        function keeps no state.

    Raises:
        ValueError: If ``status`` is not one of the four shared statuses. An
            unknown value would otherwise become an equality condition that can
            never match, hiding a programming error.
    """
    if status not in RESERVATION_STATUSES:
        raise ValueError(f"unknown reservation status: {status!r}")

    if status == _HELD_STATUS:
        return and_(
            reservations.c.status == _HELD_STATUS,
            reservations.c.created_at > cutoff,
        )
    if status == _EXPIRED_STATUS:
        return or_(
            reservations.c.status == _EXPIRED_STATUS,
            and_(
                reservations.c.status == _HELD_STATUS,
                reservations.c.created_at <= cutoff,
            ),
        )
    return reservations.c.status == status
