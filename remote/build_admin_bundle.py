"""Stage an allowlisted Remote context and installed Windows OpenSSH, never a checkout.

Usage: python remote/build_admin_bundle.py --root REPO --output WORK/assets
The output must be absent or empty. PyInstaller supplies the build-only pefile module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

from voxarium_studio_remote.admin_bundle import REQUIRED_REMOTE_FILES


def _plain(path: Path, root: Path) -> None:
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError(f"Links are not allowed in bundle resources: {path}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Resource escapes its source directory: {path}")


def source_files(remote: Path) -> list[Path]:
    _plain(remote, remote.parent)
    package = remote / "voxarium_studio_remote"
    _plain(package, remote)
    files = [remote / name for name in ("Dockerfile", "pyproject.toml", "README.md")]
    for name in REQUIRED_REMOTE_FILES:
        if not (remote / name).is_file():
            raise ValueError(f"Required Remote source is missing: {name}")
    for directory, dirs, names in os.walk(package, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d.isidentifier() and d not in {"__pycache__", "tests"})
        for name in dirs:
            _plain(Path(directory) / name, remote)
        files.extend(Path(directory) / name for name in sorted(names)
                     if name.endswith(".py") and Path(name).stem.isidentifier())
    if package / "__init__.py" not in files:
        raise ValueError("Remote source is missing voxarium_studio_remote/__init__.py")
    for path in files:
        _plain(path, remote)
        if not path.is_file():
            raise ValueError(f"Required Remote source is missing: {path}")
    return files


def ssh_files(openssh: Path) -> list[Path]:
    """Resolve local DLL imports; OS/API-set DLLs stay supplied by Windows.

    Windows' inbox OpenSSH stores libcrypto.dll in System32, not OpenSSH.
    No binaries are downloaded and no arbitrary PATH directories are consulted.
    """
    _plain(openssh, openssh.parent)
    ssh = openssh / "ssh.exe"
    if not ssh.is_file():
        raise ValueError("Windows OpenSSH Client is missing. Install the Windows optional feature and rebuild.")
    import pefile
    files = [ssh]
    # These helper processes support hardware-backed keys and PKCS#11 providers.
    files.extend(openssh / name for name in ("ssh-sk-helper.exe", "ssh-pkcs11-helper.exe")
                 if (openssh / name).is_file())
    seen = {p.name.lower() for p in files}
    for path in files:
        _plain(path, openssh.parent)
        with pefile.PE(str(path)) as pe:
            imports = [entry.dll.decode("ascii") for attr in
                       ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT")
                       for entry in getattr(pe, attr, [])]
        for name in imports:
            if Path(name).name != name:
                raise ValueError(f"Invalid DLL import: {name}")
            key = name.lower()
            if key in seen:
                continue
            local = openssh / name
            if not local.is_file() and key.startswith(("libcrypto", "libssl")):
                local = openssh.parent / name
                if not local.is_file():
                    raise ValueError(f"OpenSSH dependency is missing: {name}. Repair Windows OpenSSH.")
            if local.is_file():
                seen.add(key)
                files.append(local)
    for name in ("LICENSE.txt", "NOTICE.txt"):
        notice = openssh / name
        if not notice.is_file():
            raise ValueError(f"OpenSSH redistribution notice is missing: {notice}. Repair Windows OpenSSH.")
        _plain(notice, openssh)
        files.append(notice)
    return files


def stage_assets(root: Path, output: Path, openssh: Path) -> None:
    remote = root / "remote"
    sources = source_files(remote)
    binaries = ssh_files(openssh)
    if output.is_symlink() or output.resolve().is_relative_to(remote.resolve()):
        raise ValueError("Bundle output must be a separate build directory, not Remote source.")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Bundle output must be empty; choose a fresh build assets directory.")
    for source in sources:
        target = output / "remote" / source.relative_to(remote)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (output / "openssh").mkdir()
    for source in binaries:
        shutil.copyfile(source, output / "openssh" / source.name)
    # Provenance for the staged candidate (§16.9); no profiles/data are inputs.
    files = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(output.rglob("*")) if path.is_file()}
    (output / "manifest.json").write_text(json.dumps({
        "format": 1, "wire_protocol": 2, "schema_version": 2,
        "admin_rpc_version": 1, "files": files,
    }, sort_keys=True, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    openssh = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "OpenSSH"
    try:
        stage_assets(args.root, args.output, openssh)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Bundle staging failed: {exc}\n")


if __name__ == "__main__":
    main()
