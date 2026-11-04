"""Run an arbitrary Python snippet inside the Unreal editor.

The import node's own mechanism, exposed directly: a project that needs some other editor
call should not need a new plugin to make it.
"""

from __future__ import annotations

from pathlib import Path

import _voxarium_studio as td
import _ue

PREAMBLE = '''\
# Provided by Voxarium Studio. Print MARKER + a JSON object to return structured data.
MARKER = __VOXARIUM_STUDIO_MARKER__
'''


def build_editor_script(user_script: str) -> str:
    return PREAMBLE.replace("__VOXARIUM_STUDIO_MARKER__", repr(_ue.RESULT_MARKER)) + "\n" + user_script


def main() -> None:
    payload = td.read_payload()
    values = td.resolved(payload)
    settings = _ue.UnrealSettings.from_payload(td.settings(payload), values)

    user_script = str(values.get("script") or "").strip()
    if not user_script:
        td.fail("No script provided")

    td.log(f"transport: {settings.transport}")

    work_dir = Path((payload.get("context") or {}).get("work_dir") or ".")
    try:
        result, lines = _ue.run_script(
            settings,
            build_editor_script(user_script),
            work_dir=work_dir,
            timeout=1500,
            log=td.log,
        )
    except _ue.UnrealError as exc:
        td.fail(str(exc))
        return

    if result is not None and result.get("error"):
        td.fail(f"The script failed in the editor: {result['error']}")
        return

    td.emit({"result": result or {}, "output": "\n".join(lines)})


if __name__ == "__main__":
    main()
