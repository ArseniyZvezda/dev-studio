"""Which commit this build came from (§2.5).

A build is named by its tag when it has one and by its commit hash otherwise: those are
the two things that actually identify what is running, unlike the hand-maintained
``__version__``, which names a release line and moves once per release at best.

The Studio ships as a wheel, so at runtime there is usually no ``.git`` to ask — the
answer has to be written into the package while it is being built. Two sources, in
order:

1. ``_build_stamp.json`` next to this module, written by ``backend/hatch_build.py``
   into the wheel (never into the source tree, so a dev checkout cannot end up with a
   stale one shadowing git).
2. ``git`` in the Studio's own checkout, which is the case for a source tree or an
   editable install — there a stamp would be wrong the moment anyone commits.

Neither is the *managed* project's repository. That one has its own history and its own
``VcsBackend`` (§9.6); this module only ever asks about the application.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import __version__

log = logging.getLogger(__name__)

STAMP_NAME = "_build_stamp.json"

CHANGELOG_NAME = "CHANGELOG.md"
"""What each release changed, read by ``GET /api/changelog`` and shown in the rail (§12.1).

It travels with the build for the same reason the stamp does: the answer has to describe
what is *running*, and a wheel has no repository to look in.
"""

GIT_TIMEOUT = 10.0
"""Seconds. A hung `git` must not hold up startup — an unknown commit is survivable."""

_SEP = "\x1f"
"""Field separator for the one `git show` that fetches hash, short hash and date."""

_RELEASE_TAG = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?(-[0-9A-Za-z.-]+)?")


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """What the UI, `/healthz` and `voxarium_studio version` all say about this build."""

    version: str = __version__
    """The release tag or short commit shared by backend, SPA and desktop (§2.5, §2.7).
    The package version is only a fallback when no build identity is available."""
    commit: str | None = None
    short_commit: str | None = None
    tag: str | None = None
    """A tag pointing at this exact commit. A release is named by whoever tagged it, so
    when one exists it outranks the hash."""
    branch: str | None = None
    committed_at: str | None = None
    """ISO-8601 date of the *commit*, not of the build: the commit is the reproducible
    thing, and two builds of it are the same Studio."""
    dirty: bool = False
    """Built from a tree with uncommitted changes, so the hash alone does not describe
    what is running."""
    source: str = "unknown"
    """``stamp`` | ``git`` | ``unknown`` — where everything above the version came from."""

    @property
    def display(self) -> str:
        """What this build is called: its tag, or the commit it was made from.

        `v0.2.0` on a tagged commit, `c3fd2b1` on any other, and the package version only
        when there is no commit to name — an installed build that knows nothing else. One
        string, composed here, so the UI and the CLI cannot drift apart.
        """
        return self.tag or self.short_commit or self.version

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "display": self.display}


@lru_cache(maxsize=1)
def build_info() -> BuildInfo:
    """Resolved once per process.

    Caching is not just for the subprocesses it saves: the running code *is* the commit
    it was imported from, so re-reading git after someone commits would report a version
    this process is not.
    """
    return _resolve_build_info(__version__)


def read_build_info() -> BuildInfo:
    """Resolve installed files again without changing the running process's identity.

    Recovery (§2.5) must see a replaced wheel stamp or new checkout HEAD, including
    a changed package-version fallback, while ``build_info()`` stays cached.
    """
    tree = ast.parse((_package_dir() / "__init__.py").read_text(encoding="utf-8"))
    version = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets))
    if not isinstance(version, str) or not version:
        raise ValueError("Invalid installed Studio package version.")
    return _resolve_build_info(version)


def _resolve_build_info(package_version: str) -> BuildInfo:
    return (_from_stamp(package_version=package_version)
            or _from_git(studio_repo_root(), package_version=package_version)
            or BuildInfo(version=package_version))


def studio_repo_root() -> Path | None:
    """The Studio's own checkout, or ``None`` when this is an installed package.

    Deliberately strict about the layout (``<root>/backend/voxarium_studio/``): a wheel unpacked
    into a site-packages directory can easily *sit* inside some unrelated repository, and
    reporting that repository's HEAD as the Studio's version is worse than reporting no
    commit at all.
    """
    package = _package_dir()
    root = package.parent.parent
    if package == root / "backend" / "voxarium_studio" and (root / ".git").exists():
        return root
    return None


def changelog_path() -> Path | None:
    """This build's changelog, or ``None`` when it was built without one.

    The checkout's copy comes first, since a source tree's own file is the one being
    written; the wheel's copy — put beside this module by ``hatch_build.py`` — is what an
    installed Studio has instead.
    """
    root = studio_repo_root()
    candidates = [root / CHANGELOG_NAME] if root else []
    candidates.append(_package_dir() / CHANGELOG_NAME)
    return next((path for path in candidates if path.is_file()), None)


def stamp_payload() -> dict[str, Any]:
    """What ``hatch_build.py`` writes into the wheel, or ``{}`` when git cannot say.

    ``source`` is left out on purpose — whoever reads the file back knows it came from a
    stamp, and the field would otherwise claim `git` inside an installed package.
    """
    info = _from_git(studio_repo_root())
    if info is None or info.commit is None:
        return {}
    return {key: value for key, value in asdict(info).items() if key != "source"}


# ---- the two sources ---------------------------------------------------------------


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def _from_stamp(*, package_version: str = __version__) -> BuildInfo | None:
    stamp = _package_dir() / STAMP_NAME
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Ignoring an unreadable build stamp at %s: %s", stamp, exc)
        return None
    if not isinstance(data, dict):
        log.warning("Ignoring a build stamp at %s that is not an object.", stamp)
        return None
    return _from_fields(data, source="stamp", package_version=package_version)


def _from_git(root: Path | None, *, package_version: str = __version__) -> BuildInfo | None:
    if root is None:
        return None
    described = _git(root, "show", "--no-patch", f"--format=%H{_SEP}%h{_SEP}%cI", "HEAD")
    if not described:
        return None
    parts = described.split(_SEP)
    if len(parts) != 3 or not parts[0]:
        return None
    commit, short_commit, committed_at = parts

    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    # "HEAD" is what a detached checkout answers, and that is not a branch anyone can name.
    if not branch or branch == "HEAD":
        branch = None

    # `--exact-match` is the whole point: a tag three commits back names a release this
    # build is not. Failure here means "no tag on HEAD", which is the ordinary case.
    tag = _git(root, "describe", "--tags", "--exact-match", "HEAD")

    # `None` here means git refused to answer, which is not evidence of a clean tree.
    changes = _git(root, "status", "--porcelain")

    return _from_fields(
        {
            "commit": commit,
            "short_commit": short_commit,
            "tag": tag,
            "committed_at": committed_at,
            "branch": branch,
            "dirty": bool(changes),
        },
        source="git",
        package_version=package_version,
    )


def _from_fields(data: dict[str, Any], *, source: str, package_version: str = __version__) -> BuildInfo:
    commit = _text(data.get("commit"))
    short_commit = _text(data.get("short_commit")) or (commit[:7] if commit else None)
    tag = _text(data.get("tag"))
    # §2.5, §2.7: a release tag is the runtime version too, not only a display label.
    # Match the Windows/Electron SemVer normalization, retaining prerelease suffixes.
    release = _RELEASE_TAG.fullmatch(tag or "")
    version = (f"{release[1]}.{release[2]}.{release[3] or '0'}{release[4] or ''}"
               if release else tag or short_commit or _text(data.get("version")) or package_version)
    return BuildInfo(
        version=version,
        commit=commit,
        short_commit=short_commit,
        tag=tag,
        branch=_text(data.get("branch")),
        committed_at=_text(data.get("committed_at")),
        dirty=bool(data.get("dirty", False)),
        source=source,
    )


def _text(value: Any) -> str | None:
    """A stamp is a file on disk: treat anything that is not a non-empty string as absent."""
    return value.strip() or None if isinstance(value, str) else None


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("git %s in %s: %s", " ".join(args), root, exc)
        return None
    if result.returncode != 0:
        log.debug("git %s exited %s: %s", " ".join(args), result.returncode, result.stderr.strip())
        return None
    return result.stdout.strip()
