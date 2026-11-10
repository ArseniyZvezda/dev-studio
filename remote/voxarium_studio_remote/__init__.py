"""Voxarium Studio Remote — the server local Studios synchronise through (dev spec §16).

Deliberately a package of its own that imports nothing from ``voxarium_studio``: it is
deployed alone, in a container, and what it shares with the Studio is a wire protocol
(:mod:`voxarium_studio_remote.protocol`), not code.
"""

__version__ = "0.1.0"
