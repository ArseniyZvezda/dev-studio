"""Online sync with a Voxarium Studio Remote (§16).

The local Studio stays where it is — beside the managed project, serving one person's
browser — and this package is its other end: one connection to a shared server, through
which the project's Documents reach everybody else's Studio.
"""

from .service import RemoteSync

__all__ = ["RemoteSync"]
