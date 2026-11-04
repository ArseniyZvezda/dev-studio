"""Stamps the wheel with the commit it was built from.

An installed Studio has no ``.git`` to ask, so the commit has to travel inside the
package; :mod:`voxarium_studio.build_info` reads it back and only falls back to git in a source
tree. The stamp is written to a temporary directory and force-included, never into
``voxarium_studio/`` itself — a build that dies halfway would otherwise leave a file behind that
outranks git for the rest of that checkout's life.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class BuildStampHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.root not in sys.path:
            sys.path.insert(0, self.root)
        from voxarium_studio.build_info import STAMP_NAME, stamp_payload

        payload = stamp_payload()
        if not payload:
            self.app.display_warning(
                "No commit available: this build will report only its version number."
            )
            return

        self._staging = tempfile.mkdtemp(prefix="voxarium_studio-stamp-")
        stamp = Path(self._staging) / STAMP_NAME
        stamp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        build_data.setdefault("force_include", {})[str(stamp)] = f"voxarium_studio/{STAMP_NAME}"
        self.app.display_info(f"Stamping the build as {payload['commit'][:7]}.")

    def finalize(self, version: str, build_data: dict[str, Any], artifact: str) -> None:
        staging = getattr(self, "_staging", None)
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
            self._staging = None
