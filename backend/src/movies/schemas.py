"""Pydantic request and response models for the genre and movie endpoints.

These are the wire formats fixed by SPEC-3 Sections 2.3 and 2.4. They only
shape and validate: every business rule (uniqueness, existence of referenced
genre ids, genre replacement) lives in :mod:`src.movies.service`, so this
module imports nothing from it and no response model can carry a field the SPEC
does not list.

Input rules implemented here, all from SPEC-3 Section 2.4:

* every request model is ``extra="forbid"`` (FR-4);
* strings are stripped before their length is checked, so a value that is only
  whitespace fails the 1-character minimum;
* ``poster_url`` is an absolute ``http``/``https`` URL of at most
  :data:`POSTER_URL_MAX_LENGTH` characters;
* ``genre_ids`` holds distinct ids and at most :data:`MAX_GENRE_IDS` of them;
* ``MovieUpdate`` accepts every field as optional, but an empty body and an
  explicit ``null`` for a non-nullable field are both rejected. Only
  ``poster_url: null`` is meaningful -- it clears the poster.

No money value appears in this module, so there is nothing to type as a float
(SPEC-3 FR-15; ``principles.md``, "All money is integer cents, never float").
"""

from __future__ import annotations

from datetime import datetime
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# SPEC-3 Section 2.4 bounds, declared once and referenced by both the field
# validators below and any later caller that needs the same number.
GENRE_NAME_MAX_LENGTH = 50
MOVIE_TITLE_MAX_LENGTH = 200
MOVIE_DESCRIPTION_MAX_LENGTH = 5000
POSTER_URL_MAX_LENGTH = 2048
MAX_GENRE_IDS = 10

# Only these two schemes count as a usable poster reference; anything else
# (``ftp:``, a bare path, a relative URL) is rejected by ``_validate_poster_url``.
_POSTER_URL_SCHEMES = frozenset({"http", "https"})


def _strip_and_bound(
    value: str,
    *,
    field: str,
    max_length: int,
    allow_empty: bool,
) -> str:
    """Strip whitespace, then enforce a length range on the stripped value.

    The check runs on the stripped text, so ``"  a  "`` and ``"a"`` are the
    same value and a string of only whitespace counts as empty.

    Args:
        value: The submitted string.
        field: The field name, used verbatim in the error message so the client
            can tell which value was wrong.
        max_length: Maximum allowed number of characters after stripping.
        allow_empty: When ``True``, an empty stripped value is accepted (used
            for ``description``); when ``False`` it is an error.

    Returns:
        str: The stripped value.

    Raises:
        ValueError: If the stripped value is empty while ``allow_empty`` is
            ``False``, or longer than ``max_length``. Pydantic turns the
            ``ValueError`` into a validation error, which the API reports as 422
            ``VALIDATION_ERROR`` (SPEC-3 FR-4).
    """
    stripped = value.strip()
    if not allow_empty and not stripped:
        raise ValueError(f"{field} must not be empty")
    if len(stripped) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return stripped


def _validate_poster_url(value: str | None) -> str | None:
    """Validate an optional absolute ``http``/``https`` poster URL.

    A plain ``str`` is used rather than a URL-typed field because the SPEC only
    asks for the scheme and length rules, and no extra dependency is wanted.
    ``None`` passes through so ``poster_url: null`` can clear the poster
    (SPEC-3 Section 2.4, ``MovieUpdate``).

    Args:
        value: The submitted URL, ``None``, or an empty string.

    Returns:
        str | None: The stripped URL, or ``None`` when the input was ``None``.

    Raises:
        ValueError: If the value is longer than :data:`POSTER_URL_MAX_LENGTH`,
            or is not absolute or does not use ``http``/``https``. An empty or
            whitespace-only string has no scheme and is therefore rejected too.
    """
    if value is None:
        return None
    stripped = value.strip()
    if len(stripped) > POSTER_URL_MAX_LENGTH:
        raise ValueError(
            f"poster_url must be at most {POSTER_URL_MAX_LENGTH} characters"
        )
    parts = urlsplit(stripped)
    if parts.scheme not in _POSTER_URL_SCHEMES or not parts.netloc:
        raise ValueError("poster_url must be an absolute http or https URL")
    return stripped


def _validate_genre_ids(value: list[int]) -> list[int]:
    """Enforce the ``genre_ids`` count and distinctness rules (SPEC-3 FR-4).

    Args:
        value: The submitted list of genre ids, in any order.

    Returns:
        list[int]: The list unchanged. Order is preserved because the service
            sorts the associated genres by name when it builds the response, so
            the request order carries no meaning.

    Raises:
        ValueError: If the list has more than :data:`MAX_GENRE_IDS` items or
            contains a duplicate id.
    """
    if len(value) > MAX_GENRE_IDS:
        raise ValueError(f"genre_ids must contain at most {MAX_GENRE_IDS} items")
    if len(set(value)) != len(value):
        raise ValueError("genre_ids must not contain duplicate ids")
    return value


class GenreCreate(BaseModel):
    """The body of ``POST /admin/genres`` (SPEC-3 Sections 2.3, 2.4).

    Extra fields are forbidden (FR-4).

    Attributes:
        name: The genre name. Stripped, then 1 to
            :data:`GENRE_NAME_MAX_LENGTH` characters inclusive.
    """

    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        """Strip ``name`` and enforce its 1-50 character range.

        Args:
            value: The submitted name.

        Returns:
            str: The stripped name.

        Raises:
            ValueError: If the stripped name is empty or too long.
        """
        return _strip_and_bound(
            value,
            field="name",
            max_length=GENRE_NAME_MAX_LENGTH,
            allow_empty=False,
        )


class GenreOut(BaseModel):
    """A genre as the API returns it (SPEC-3 Section 2.3).

    Attributes:
        id: The genre's primary key.
        name: The stored name.
    """

    id: int
    name: str


class GenreRef(BaseModel):
    """A genre reference embedded in a :class:`MovieOut` (SPEC-3 Section 2.3).

    Identical in shape to :class:`GenreOut` but declared separately, as the SPEC
    names both: this one is the nested reference inside a movie, that one is the
    top-level response of the genre endpoints.

    Attributes:
        id: The genre's primary key.
        name: The stored name.
    """

    id: int
    name: str


class MovieCreate(BaseModel):
    """The body of ``POST /admin/movies`` (SPEC-3 Sections 2.3, 2.4).

    Extra fields are forbidden (FR-4). A movie with no genres is valid, so
    ``genre_ids`` defaults to an empty list.

    Attributes:
        title: The movie title. Stripped, then 1 to
            :data:`MOVIE_TITLE_MAX_LENGTH` characters inclusive.
        description: Free text, 0 to :data:`MOVIE_DESCRIPTION_MAX_LENGTH`
            characters; defaults to ``""``.
        poster_url: Optional absolute ``http``/``https`` URL, at most
            :data:`POSTER_URL_MAX_LENGTH` characters; ``None`` means no poster.
        genre_ids: Distinct genre ids to link, at most :data:`MAX_GENRE_IDS`.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    description: str = ""
    poster_url: str | None = None
    genre_ids: list[int] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str) -> str:
        """Strip ``title`` and enforce its 1-200 character range.

        Args:
            value: The submitted title.

        Returns:
            str: The stripped title.

        Raises:
            ValueError: If the stripped title is empty or too long.
        """
        return _strip_and_bound(
            value,
            field="title",
            max_length=MOVIE_TITLE_MAX_LENGTH,
            allow_empty=False,
        )

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str) -> str:
        """Strip ``description`` and enforce its 0-5000 character range.

        Args:
            value: The submitted description.

        Returns:
            str: The stripped description (possibly empty).

        Raises:
            ValueError: If the stripped description is too long.
        """
        return _strip_and_bound(
            value,
            field="description",
            max_length=MOVIE_DESCRIPTION_MAX_LENGTH,
            allow_empty=True,
        )

    @field_validator("poster_url")
    @classmethod
    def _clean_poster_url(cls, value: str | None) -> str | None:
        """Validate the optional poster URL.

        Args:
            value: The submitted URL or ``None``.

        Returns:
            str | None: The stripped URL, or ``None``.

        Raises:
            ValueError: If the URL is not an absolute ``http``/``https`` URL or
                is too long.
        """
        return _validate_poster_url(value)

    @field_validator("genre_ids")
    @classmethod
    def _clean_genre_ids(cls, value: list[int]) -> list[int]:
        """Enforce the ``genre_ids`` count and distinctness rules.

        Args:
            value: The submitted genre ids.

        Returns:
            list[int]: The list unchanged.

        Raises:
            ValueError: If there are too many ids or a duplicate.
        """
        return _validate_genre_ids(value)


class MovieUpdate(BaseModel):
    """The body of ``PATCH /admin/movies/{movie_id}`` (SPEC-3 Section 2.4).

    Every field is optional and only the fields actually present in the body are
    applied (FR-6); ``model_fields_set``, read by the service, records which ones
    those are. The body must not be empty, an explicit ``null`` is rejected for
    ``title``/``description``/``genre_ids`` because none of them is nullable in
    the database, and ``poster_url: null`` is the one meaningful ``null`` -- it
    clears the poster. ``genre_ids`` present means "replace the whole set";
    absent means "leave the links untouched".

    Attributes:
        title: New title, or ``None`` when it is not being changed.
        description: New description, or ``None`` when it is not being changed.
        poster_url: New URL, or ``None`` to clear the poster.
        genre_ids: New complete set of genre ids, or ``None`` when the links are
            not being changed.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    poster_url: str | None = None
    genre_ids: list[int] | None = None

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str | None) -> str | None:
        """Strip and bound ``title`` when it is supplied.

        Args:
            value: The submitted title. The validator only runs for a supplied
                field, so a ``None`` here means the client sent ``null``.

        Returns:
            str | None: The stripped title.

        Raises:
            ValueError: If ``value`` is ``null``, empty after stripping, or too
                long.
        """
        if value is None:
            raise ValueError("title must not be null")
        return _strip_and_bound(
            value,
            field="title",
            max_length=MOVIE_TITLE_MAX_LENGTH,
            allow_empty=False,
        )

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str | None) -> str | None:
        """Strip and bound ``description`` when it is supplied.

        Args:
            value: The submitted description. The validator only runs for a
                supplied field, so a ``None`` here means the client sent
                ``null``.

        Returns:
            str | None: The stripped description (possibly empty).

        Raises:
            ValueError: If ``value`` is ``null`` or too long.
        """
        if value is None:
            raise ValueError("description must not be null")
        return _strip_and_bound(
            value,
            field="description",
            max_length=MOVIE_DESCRIPTION_MAX_LENGTH,
            allow_empty=True,
        )

    @field_validator("poster_url")
    @classmethod
    def _clean_poster_url(cls, value: str | None) -> str | None:
        """Validate the optional poster URL, treating ``null`` as "clear it".

        Args:
            value: The submitted URL or ``None``.

        Returns:
            str | None: The stripped URL, or ``None`` to clear the poster.

        Raises:
            ValueError: If the URL is not an absolute ``http``/``https`` URL or
                is too long.
        """
        return _validate_poster_url(value)

    @field_validator("genre_ids")
    @classmethod
    def _clean_genre_ids(cls, value: list[int] | None) -> list[int] | None:
        """Enforce the ``genre_ids`` rules when the field is supplied.

        Args:
            value: The submitted genre ids. The validator only runs for a
                supplied field, so a ``None`` here means the client sent
                ``null``, which is not how an emptied set is expressed -- an
                empty list is.

        Returns:
            list[int] | None: The list unchanged.

        Raises:
            ValueError: If ``value`` is ``null``, has too many ids, or contains
                a duplicate.
        """
        if value is None:
            raise ValueError("genre_ids must not be null; send [] to clear them")
        return _validate_genre_ids(value)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> Self:
        """Reject a body that changes nothing (SPEC-3 FR-4).

        Returns:
            Self: The validated model.

        Raises:
            ValueError: If no field was supplied, i.e. the body was ``{}``.
        """
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class MovieOut(BaseModel):
    """A movie as the API returns it (SPEC-3 Section 2.3).

    Attributes:
        id: The movie's primary key.
        title: The stored title.
        description: The stored description (never ``None``).
        poster_url: The stored poster URL, or ``None`` when there is no poster.
        genres: The linked genres, sorted by name.
        created_at: Row creation time, in UTC.
        updated_at: Time of the last change, in UTC.
    """

    id: int
    title: str
    description: str
    poster_url: str | None
    genres: list[GenreRef]
    created_at: datetime
    updated_at: datetime
