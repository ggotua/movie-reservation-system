"""Movie- and genre-specific API error codes (SPEC-3 Section 2.7).

The codes a route can return for a genre or movie failure are declared exactly
once here, and each is paired with a small builder that produces the finished
:class:`~src.core.errors.ApiError`. Keeping both the code string and the
human-readable message in one place means the service layer never repeats a
literal and a client-visible message cannot drift between two call sites
(SPEC-3 Section 2.7, "Constants live in one place per module"; Section 10 DRY
check, "Error codes: constants per module, not repeated strings").

Only movies/genres codes live here. Screen and showtime codes belong to the
``showtimes`` module (SPEC-3 Section 11.1: ``movies`` and ``showtimes`` do not
import each other).

This module imports nothing from ``movies`` itself, so it sits at the bottom of
the module's dependency graph and can be imported by ``service`` without a
cycle.
"""

from __future__ import annotations

from src.core.errors import ApiError

# The codes from SPEC-3 Section 2.7 that belong to genres and movies. Each is a
# stable, machine-readable SCREAMING_SNAKE_CASE string that clients branch on
# (``docs/steering/conventions.md``).
GENRE_NOT_FOUND = "GENRE_NOT_FOUND"
MOVIE_NOT_FOUND = "MOVIE_NOT_FOUND"
GENRE_ALREADY_EXISTS = "GENRE_ALREADY_EXISTS"
GENRE_IN_USE = "GENRE_IN_USE"
MOVIE_HAS_SHOWTIMES = "MOVIE_HAS_SHOWTIMES"


def genre_not_found() -> ApiError:
    """Build the 404 returned when a referenced genre id does not exist.

    Returns:
        ApiError: ``GENRE_NOT_FOUND`` with status 404 (SPEC-3 FR-5, decision
        D6).
    """
    return ApiError(GENRE_NOT_FOUND, "No genre with that id exists.", 404)


def movie_not_found() -> ApiError:
    """Build the 404 returned when a referenced movie id does not exist.

    Returns:
        ApiError: ``MOVIE_NOT_FOUND`` with status 404 (SPEC-3 FR-5, decision
        D6).
    """
    return ApiError(MOVIE_NOT_FOUND, "No movie with that id exists.", 404)


def genre_already_exists() -> ApiError:
    """Build the 409 returned when a genre name is already taken.

    The check is case-insensitive (SPEC-3 FR-8): ``"Action"`` and ``"action"``
    are the same genre for this purpose.

    Returns:
        ApiError: ``GENRE_ALREADY_EXISTS`` with status 409.
    """
    return ApiError(
        GENRE_ALREADY_EXISTS,
        "A genre with that name already exists.",
        409,
    )


def genre_in_use() -> ApiError:
    """Build the 409 returned when deleting a genre that still has movies.

    Returns:
        ApiError: ``GENRE_IN_USE`` with status 409 (SPEC-3 FR-8).
    """
    return ApiError(
        GENRE_IN_USE,
        "This genre is attached to one or more movies and cannot be deleted.",
        409,
    )


def movie_has_showtimes() -> ApiError:
    """Build the 409 returned when deleting a movie that still has showtimes.

    Any showtime counts -- past or future -- because ``showtimes.movie_id`` uses
    ``ON DELETE RESTRICT`` and the stricter rule keeps the history of past
    showtimes intact (SPEC-3 FR-7, decision D3).

    Returns:
        ApiError: ``MOVIE_HAS_SHOWTIMES`` with status 409.
    """
    return ApiError(
        MOVIE_HAS_SHOWTIMES,
        "This movie has showtimes and cannot be deleted.",
        409,
    )
