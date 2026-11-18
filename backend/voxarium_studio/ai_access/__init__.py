"""Opt-in AI access through Remote — AI Control Protocol 1 contracts and storage (§16.12).

Future protocol, NOT a shipped feature: nothing in this package is mounted, started or
imported by the application. ``protocol`` restates the wire contract, ``identity`` and
``grants`` keep the private per-user consent outside the managed project, ``ledger`` and
``projection`` hold the project-side command ledger and the allowlisted DTOs. Transport,
API routes and the executor admission path are later stages (design §11 S-A3/S-A4).

"ACP" in this package always means the Studio's *AI Control Protocol*, never the Agent
Client Protocol that the Antigravity backend speaks over stdio (§10.6).
"""
