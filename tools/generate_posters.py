"""Generate the twelve original showcase posters (SPEC-7A D2, FR-7).

The Movie Reservation System ships twelve fictional films (SPEC-7A Section 2.2)
whose posters must be original, offline-capable and committed to the repository.
This tool draws one abstract SVG per film straight into the frontend's
``public/posters/`` directory, using nothing outside the Python standard
library, so no image editor and no dependency is involved.

Usage (from the repository root)::

    python tools/generate_posters.py

Every poster is 600 x 900. Its background is one flat colour from the dark
palette of ``docs/design/frontend-design-brief.md`` Section 3 (``foyer`` or
``paper``), its artwork is three to five large geometric shapes (circles, arcs,
rectangles, stripes) in the accent colours ``velvet`` and ``brass`` plus
``ink`` and ``aisle``, and its title is set in a generic sans-serif at the
bottom of the frame, wrapped onto at most three lines at 40 px or more.

The output is deterministic: every choice comes from ``random.Random(slug)``,
seeded with the film's slug, so regenerating rewrites byte-identical files. The
SVG documents carry no gradient, no ``<script>`` and no external reference.

The film list below is the single source of truth for the file names, and
``backend/src/db/seed_demo.py`` builds ``poster_url`` from the same titles with
the same :func:`slugify` rule. ``backend/tests/integration/test_seed_demo.py``
fails when the two ever drift apart.
"""

from __future__ import annotations

import html
import random
import sys
from pathlib import Path
from typing import Final

# --- Dark palette (docs/design/frontend-design-brief.md Section 3) ---------
_FOYER: Final[str] = "#17131C"
_PAPER: Final[str] = "#221C29"
_INK: Final[str] = "#ECE8F0"
_VELVET: Final[str] = "#D4506B"
_BRASS: Final[str] = "#D9A441"
_AISLE: Final[str] = "#4A4455"

# The title is always this light paper white, never brass (the brief keeps
# brass away from text on dark surfaces for contrast).
_TITLE_COLOUR: Final[str] = "#FBFCFD"

_WIDTH: Final[int] = 600
_HEIGHT: Final[int] = 900
_MARGIN: Final[int] = 48
_ART_TOP: Final[int] = 130
_ART_BOTTOM: Final[int] = 650
_TITLE_BASELINE: Final[int] = 838
_MAX_TITLE_CHARACTERS: Final[int] = 20
_MIN_TITLE_PIXELS: Final[int] = 40

# One poster per film, as (title, primary genre, runtime in minutes). The
# runtime is printed in the small label at the top; the seed does not store it
# because the movies table has no runtime column (SPEC-7A Section 2.2).
_POSTERS: Final[tuple[tuple[str, str, int], ...]] = (
    ("The Long Quiet", "Drama", 112),
    ("Harbour Lights", "Drama", 98),
    ("Paper Moons", "Comedy", 104),
    ("Static Bloom", "Sci-Fi", 121),
    ("The Salt Road", "Drama", 133),
    ("Night Shift at Nine", "Comedy", 95),
    ("Orbit of Small Things", "Sci-Fi", 108),
    ("Glass Orchard", "Thriller", 117),
    ("A Year in Tbilisi Rain", "Documentary", 87),
    ("Second Summer", "Comedy", 101),
    ("Lantern Hill", "Thriller", 126),
    ("The Cartographer's Daughter", "Drama", 139),
)


def slugify(title: str) -> str:
    """Turn a film title into its poster's file name.

    Args:
        title: The film title, in any case.

    Returns:
        str: The lowercased title with every run of non-alphanumeric characters
        collapsed into a single hyphen and no leading or trailing hyphen, so
        ``"A Year in Tbilisi Rain"`` becomes ``"a-year-in-tbilisi-rain"``.
    """
    slug = "".join(character if character.isalnum() else "-" for character in title)
    return "-".join(part for part in slug.lower().split("-") if part)


def _wrap(title: str, max_characters: int) -> list[str]:
    """Wrap a title onto lines of at most ``max_characters`` characters.

    Args:
        title: The film title.
        max_characters: The widest line, in characters.

    Returns:
        list[str]: The title's words, distributed over its lines. A single word
        longer than ``max_characters`` still gets a line of its own rather than
        being split mid-word.
    """
    lines: list[str] = []
    current = ""
    for word in title.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_characters:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _circle(rng: random.Random, colour: str) -> str:
    """Draw one large filled disc.

    Args:
        rng: The poster's deterministic generator.
        colour: The fill colour.

    Returns:
        str: The ``<circle>`` element.
    """
    radius = rng.randint(90, 170)
    cx = rng.randint(_MARGIN + radius // 2, _WIDTH - _MARGIN - radius // 2)
    cy = rng.randint(220 + radius // 2, 640 - radius // 2)
    return f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="{colour}" opacity="0.92" />'


def _rectangle(rng: random.Random, colour: str) -> str:
    """Draw one large rectangle, rotated a few degrees around its centre.

    Args:
        rng: The poster's deterministic generator.
        colour: The fill colour.

    Returns:
        str: The ``<rect>`` element.
    """
    width = rng.randint(200, 360)
    height = rng.randint(160, 300)
    x = rng.randint(60, _WIDTH - 60 - width)
    y = rng.randint(_ART_TOP, _ART_BOTTOM - height)
    angle = rng.randint(-12, 12)
    centre_x = x + width / 2
    centre_y = y + height / 2
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
        f'fill="{colour}" opacity="0.9" '
        f'transform="rotate({angle} {centre_x:.1f} {centre_y:.1f})" />'
    )


def _arc(rng: random.Random, colour: str) -> str:
    """Draw one large semicircular arc as a thick stroke.

    Args:
        rng: The poster's deterministic generator.
        colour: The stroke colour.

    Returns:
        str: The ``<path>`` element, opening downwards.
    """
    radius = rng.randint(140, 240)
    cx = rng.randint(radius + 60, _WIDTH - radius - 60)
    cy = rng.randint(430, 640)
    stroke = rng.randint(22, 40)
    left = cx - radius
    right = cx + radius
    return (
        f'<path d="M {left} {cy} A {radius} {radius} 0 0 1 {right} {cy}" '
        f'fill="none" stroke="{colour}" stroke-width="{stroke}" '
        f'stroke-linecap="butt" opacity="0.9" />'
    )


def _stripes(rng: random.Random, colour: str) -> str:
    """Draw one group of four to six wide vertical stripes.

    The whole group is one shape: a single ``<g>`` of equal bars.

    Args:
        rng: The poster's deterministic generator.
        colour: The fill colour.

    Returns:
        str: The ``<g>`` element holding the bars.
    """
    count = rng.randint(4, 6)
    width = rng.randint(26, 44)
    gap = rng.randint(16, 28)
    height = rng.randint(220, 340)
    total = count * width + (count - 1) * gap
    start_x = rng.randint(_MARGIN, _WIDTH - _MARGIN - total)
    y = rng.randint(_ART_TOP, _ART_BOTTOM - height)
    bars = "".join(
        f'<rect x="{start_x + step * (width + gap)}" y="{y}" '
        f'width="{width}" height="{height}" fill="{colour}" />'
        for step in range(count)
    )
    return f'<g opacity="0.9">{bars}</g>'


# The four shape kinds, indexed by a generator draw in :func:`_artwork`.
_SHAPES = (_circle, _arc, _rectangle, _stripes)


def _shape_colours(rng: random.Random, count: int) -> list[str]:
    """Pick one colour per shape, guaranteeing both accent colours appear.

    Args:
        rng: The poster's deterministic generator.
        count: How many shapes the poster has, three to five.

    Returns:
        list[str]: ``count`` colours, starting with ``velvet`` and ``brass`` and
        filled up from the dark palette's other tones, in random order.
    """
    colours = [_VELVET, _BRASS]
    palette = (_VELVET, _BRASS, _INK, _AISLE)
    while len(colours) < count:
        colours.append(rng.choice(palette))
    rng.shuffle(colours)
    return colours


def _artwork(rng: random.Random) -> str:
    """Compose the poster's abstract artwork.

    Args:
        rng: The poster's deterministic generator, consumed for the number of
            shapes, then their kinds, colours and geometry.

    Returns:
        str: The markup of three to five large shapes, each on its own line.
    """
    count = rng.randint(3, 5)
    colours = _shape_colours(rng, count)
    elements = [_SHAPES[rng.randrange(len(_SHAPES))](rng, colour) for colour in colours]
    return "\n    ".join(elements)


def _title_markup(title: str) -> str:
    """Set the film title at the bottom of the poster.

    The title is wrapped onto at most three lines; the type size is 56, 48 or
    42 px depending on the line count and is reduced by two pixels at a time
    (never below :data:`_MIN_TITLE_PIXELS`) when a line would be wider than the
    printable area.

    Args:
        title: The film title.

    Returns:
        str: The ``<text>`` elements and the brass rule above them.
    """
    lines = _wrap(title, _MAX_TITLE_CHARACTERS)
    assert len(lines) <= 3, title
    font_size = {1: 56, 2: 48}.get(len(lines), 42)
    longest = max(len(line) for line in lines)
    while (
        longest * 0.58 * font_size > _WIDTH - 2 * _MARGIN
        and font_size > _MIN_TITLE_PIXELS
    ):
        font_size -= 2
    line_height = round(font_size * 1.18)
    first_baseline = _TITLE_BASELINE - (len(lines) - 1) * line_height
    rule_y = first_baseline - font_size - 26
    rule = f'<rect x="{_MARGIN}" y="{rule_y}" width="64" height="4" fill="{_BRASS}" />'
    texts = "".join(
        f'<text x="{_MARGIN}" y="{first_baseline + index * line_height}" '
        f'font-family="sans-serif" font-size="{font_size}" '
        f'fill="{_TITLE_COLOUR}">{html.escape(line)}</text>'
        for index, line in enumerate(lines)
    )
    return f"{rule}{texts}"


def _poster_svg(title: str, genre: str, runtime: int) -> str:
    """Build one poster's complete SVG document.

    Args:
        title: The film title.
        genre: The film's primary genre, printed in the small top label.
        runtime: The film's runtime in minutes, printed in the same label.

    Returns:
        str: A complete, self-contained SVG document with a flat background,
        three to five shapes and the title.
    """
    slug = slugify(title)
    rng = random.Random(slug)
    background = _PAPER if rng.random() < 0.4 else _FOYER
    label = f"{genre.upper()} \u00b7 {runtime} MIN"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_WIDTH} '
        f'{_HEIGHT}" width="{_WIDTH}" height="{_HEIGHT}" role="img" '
        f'aria-labelledby="title-{slug} description-{slug}">\n'
        f'  <title id="title-{slug}">{html.escape(title)}</title>\n'
        f'  <desc id="description-{slug}">Abstract poster for the fictional '
        f"film {html.escape(title)}.</desc>\n"
        f'  <rect width="{_WIDTH}" height="{_HEIGHT}" fill="{background}" />\n'
        f'  <text x="{_MARGIN}" y="76" font-family="sans-serif" '
        f'font-size="18" fill="{_INK}" letter-spacing="5" '
        f'opacity="0.75">{html.escape(label)}</text>\n'
        f"  <g>\n    {_artwork(rng)}\n  </g>\n"
        f"  <g>\n    {_title_markup(title)}\n  </g>\n"
        "</svg>\n"
    )


def poster_documents() -> dict[str, str]:
    """Build every poster in memory.

    Returns:
        dict[str, str]: The SVG document of each poster, keyed by file name.
    """
    return {
        f"{slugify(title)}.svg": _poster_svg(title, genre, runtime)
        for title, genre, runtime in _POSTERS
    }


def _default_output_dir() -> Path:
    """Return the committed poster directory.

    Returns:
        Path: ``frontend/public/posters`` in this tool's repository.
    """
    repository_root = Path(__file__).resolve().parents[1]
    return repository_root / "frontend" / "public" / "posters"


def main() -> int:
    """Write the twelve posters and report every file's size.

    Returns:
        int: Always ``0``; a failed write raises rather than returning a code.
    """
    output_dir = _default_output_dir()
    documents = poster_documents()
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        path = output_dir / name
        path.write_text(document, encoding="utf-8", newline="\n")
        print(f"{name:<34} {path.stat().st_size:>6} bytes")
    print(f"wrote {len(documents)} posters to {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
