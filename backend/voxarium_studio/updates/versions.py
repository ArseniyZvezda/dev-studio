"""Release tags, ordered (§12.1).

A build is named by the tag on its commit (:mod:`voxarium_studio.build_info`), so "is there a
newer version" is a question about **tags**, never about commits: a tagged commit is one
somebody decided to ship, and the update button offers exactly those.

Only finished releases parse — ``v1.0``, ``2.3.1``, ``v0.10``. Anything else (``v1.1-rc1``,
``nightly``, ``release/2026-05``) is deliberately unparseable rather than ordered last:
this module's answer becomes a button that restarts the Studio, and a tag nobody can place
in an order is not a release to offer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

_RELEASE_TAG = re.compile(r"^v?(\d+(?:\.\d+)*)$")

_WIDTH = 4
"""How many components every parsed tag is padded to.

``v1`` and ``v1.0`` are the same release written two ways, and a comparison of the raw
tuples would call the shorter one older. Padding both to the same width is what makes them
equal — and what keeps ``v1.0`` below ``v1.0.1``.
"""


@dataclass(frozen=True, slots=True, order=True)
class Release:
    """One release tag, comparable to another.

    ``numbers`` comes first and is the only compared field: two tags that mean the same
    release (``v1`` and ``v1.0``) must compare equal even though their text differs, and
    the text is only ever used to hand back to git and to show on screen.
    """

    numbers: tuple[int, ...]
    tag: str = field(compare=False)

    def __str__(self) -> str:
        return self.tag


def parse_tag(tag: str) -> Release | None:
    """``v1.0`` → a comparable release; anything else → ``None``."""
    match = _RELEASE_TAG.match(tag.strip())
    if match is None:
        return None
    numbers = tuple(int(part) for part in match.group(1).split("."))
    padded = numbers + (0,) * max(0, _WIDTH - len(numbers))
    return Release(numbers=padded, tag=tag.strip())


def parse_tags(tags: Iterable[str]) -> list[Release]:
    """Every tag this module understands, oldest first. Unparseable ones are dropped."""
    return sorted(release for tag in tags if (release := parse_tag(tag)) is not None)


def latest_release(tags: Iterable[str]) -> Release | None:
    """The newest release among ``tags``, or ``None`` when none of them is one."""
    releases = parse_tags(tags)
    return releases[-1] if releases else None


def is_newer(candidate: Release, current: Release | None) -> bool:
    """Whether ``candidate`` is worth offering over ``current``.

    ``None`` means the running build sits on no release at all — a checkout ahead of, or
    beside, every tag. Anything tagged is then newer than nothing, which is the honest
    answer: the update moves it onto a release.
    """
    return current is None or candidate > current
