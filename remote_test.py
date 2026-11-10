"""Two people on one machine: a local remote, two Studios, two browser windows (dev spec §16).

Run it through ``remote_test.bat``. It starts a Voxarium Studio Remote and two Studios —
"alice" and "bob", each with its own managed project and its own settings directory —
connects both to the remote, and opens a browser window on each, side by side. Type in one
window and watch the other.

    remote_test.bat              start everything; Enter (or closing the window) stops it
    remote_test.bat --detach     start everything and return; stop it with `remote_test.bat stop`
    remote_test.bat stop         stop what a detached run left running
    remote_test.bat --fresh      throw the test projects and the test server's data away first
    remote_test.bat --no-browser start the servers without opening the windows

This is the one sanctioned exception to "one instance, started by run.ps1" (AGENTS.md): a
second person cannot be tested with one Studio. It keeps out of ``run.ps1``'s way entirely —
its own ports (8787, 8801, 8802; never 8765 or 5173), its own projects, and it only ever
stops the processes it recorded starting. Everything it writes is under
``.scratch/remote-test/``, which is gitignored: the two projects, the server's database, the
keys it issued for that server, logs, and the browser profiles of the two windows.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".scratch" / "remote-test"
PIDS = WORK / "pids.json"
KEYS = WORK / "keys.json"

REMOTE_PORT = 8787
STUDIO_PORTS = {"alice": 8801, "bob": 8802}
SPACE = "remote-test"
REMOTE_URL = f"http://127.0.0.1:{REMOTE_PORT}"
SAMPLE_DOC = "welcome.md"
SAMPLE_TEXT = """\
# Editing together

This document is open in two Studios at once. Type here and watch the other window.

- [ ] Type a sentence as Alice
- [ ] Type one as Bob, at the same time
- [ ] Stop the remote (`remote_test.bat stop` kills everything) and see both carry on alone
"""

DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


class Failed(Exception):
    """Something the person running this has to fix; the message says what."""


# ------------------------------------------------------------------ processes


def read_pids() -> dict[str, int]:
    try:
        return {str(name): int(pid) for name, pid in json.loads(PIDS.read_text()).items()}
    except (OSError, ValueError):
        return {}


def write_pids(pids: dict[str, int]) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    PIDS.write_text(json.dumps(pids))


def image_of(pid: int) -> str:
    """The executable a PID belongs to right now, lower-cased — empty if there is none."""
    done = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True
    )
    first = done.stdout.strip().splitlines()[0] if done.stdout.strip() else ""
    return first.split(",")[0].strip('"').lower() if first.startswith('"') else ""


def stop(quiet: bool = False) -> None:
    """Stop what was recorded as started here — and only if the PID is still that program.

    A PID is recycled by Windows once its process is gone, so a number written down an hour
    ago may be somebody else's by now. Each is checked against the kind of program it was
    before it is touched.
    """
    expected = {"remote": "python", "alice": "python", "bob": "python"}
    for name, pid in read_pids().items():
        image = image_of(pid)
        wanted = expected.get(name, "")  # the windows are whatever browser was found
        if not image or (wanted and wanted not in image):
            continue
        # /T: a Studio's server is a child of the process recorded here.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        if not quiet:
            print(f"  stopped {name} (pid {pid})")
    write_pids({})


def port_busy(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def spawn(name: str, command: list[str], *, detach: bool, env: dict[str, str] | None = None) -> int:
    environment = {**os.environ, **(env or {})}
    # A Studio started from inside the desktop app's shell must not take itself for the
    # desktop-owned instance.
    environment.pop("VOXARIUM_STUDIO_DESKTOP_INSTANCE", None)
    log = open(WORK / f"{name}.log", "ab")
    process = subprocess.Popen(
        command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env=environment, cwd=str(ROOT),
        # Attached, the servers share this console and go when it is closed — which is the
        # whole lifecycle of an interactive run. Detached, they outlive it by design.
        creationflags=DETACHED if detach else 0,
    )
    return process.pid


# ------------------------------------------------------------------ HTTP


def call(method: str, url: str, body: dict | None = None, timeout: float = 5.0) -> dict | list:
    request = urllib.request.Request(
        url, method=method,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise Failed(f"{method} {url} answered {error.code}: {detail}") from error


def wait_for(what: str, check, *, seconds: float, log: str | None = None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (OSError, Failed, ValueError):
            pass
        time.sleep(0.25)
    hint = f" See {WORK / log}." if log else ""
    raise Failed(f"{what} did not happen within {seconds:.0f}s.{hint}")


def healthy(port: int, service: str) -> bool:
    answer = call("GET", f"http://127.0.0.1:{port}/healthz", timeout=1)
    return isinstance(answer, dict) and answer.get("service") == service


# ------------------------------------------------------------------ setup


def check_environment() -> None:
    if os.name != "nt":
        raise Failed("remote_test is a Windows launcher; on other systems start the three by hand.")
    missing = [
        package for package in ("voxarium_studio", "voxarium_studio_remote")
        if importlib.util.find_spec(package) is None
    ]
    if missing:
        raise Failed(
            f"Not installed in {sys.executable}: {', '.join(missing)}.\n"
            '  cd backend\n  uv pip install -e ".[teams,dev]" -e ../remote'
        )
    from voxarium_studio.main import static_dir

    if static_dir() is None:
        raise Failed("The SPA is not built. Run:  run.cmd -Build   (or `npm run build` in frontend/)")


def issue_keys() -> dict[str, str]:
    """One key per person for the test server — issued once and kept beside its database."""
    data = WORK / "remote-data"
    if KEYS.exists() and (data / "remote.db").exists():
        try:
            keys = json.loads(KEYS.read_text())
            if all(isinstance(keys.get(name), str) for name in STUDIO_PORTS):
                return keys
        except ValueError:
            pass
    keys: dict[str, str] = {}
    cli = [sys.executable, "-m", "voxarium_studio_remote.cli", "user"]
    for name in STUDIO_PORTS:
        done = subprocess.run([*cli, "add", name, "--data", str(data)], capture_output=True, text=True)
        if done.returncode != 0:
            # The user is there from an earlier run whose keys file is gone: issue a new key.
            done = subprocess.run([*cli, "rotate", name, "--data", str(data)], capture_output=True, text=True)
        if done.returncode != 0:
            raise Failed(f"Could not issue a key for {name}: {done.stdout}{done.stderr}")
        keys[name] = done.stdout.strip().splitlines()[-1]
    KEYS.write_text(json.dumps(keys))
    return keys


def connect(name: str, token: str) -> None:
    """Point one Studio at the test remote, the way its settings form would."""
    base = f"http://127.0.0.1:{STUDIO_PORTS[name]}"
    call("PUT", f"{base}/api/sync", {"enabled": True, "url": REMOTE_URL, "space": SPACE, "token": token})

    def connected() -> bool:
        status = call("GET", f"{base}/api/sync")
        assert isinstance(status, dict)
        if status["state"] in ("unauthorised", "error"):
            raise SystemExit(f"{name} cannot sync: {status.get('message')}")
        return status["state"] == "connected"

    wait_for(f"{name} connecting to the remote", connected, seconds=30, log=f"{name}.log")


# ------------------------------------------------------------------ windows


def find_browser() -> str | None:
    """A Chromium browser to open the two windows in: Edge is on every Windows, Chrome is common."""
    import winreg

    for executable in ("msedge.exe", "chrome.exe"):
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
                path, _kind = winreg.QueryValueEx(winreg.OpenKey(hive, key), None)
            except OSError:
                continue
            if path and Path(path).is_file():
                return path
    return None


def open_windows(detach: bool) -> dict[str, int]:
    """One window per person, side by side — each in a profile of its own.

    A profile of its own is what makes them two *windows*: handed to a browser that is
    already running, a URL becomes one more tab in whatever window was used last, and the
    size and position asked for are ignored. It also keeps the test out of the person's
    real browser profile.
    """
    urls = {name: f"http://127.0.0.1:{port}/documents?path={SAMPLE_DOC}" for name, port in STUDIO_PORTS.items()}
    browser = find_browser()
    if browser is None:
        import webbrowser

        for url in urls.values():
            webbrowser.open_new(url)
        return {}

    user32 = ctypes.windll.user32
    width, height = user32.GetSystemMetrics(0) // 2, user32.GetSystemMetrics(1) - 60
    pids: dict[str, int] = {}
    for index, (name, url) in enumerate(urls.items()):
        process = subprocess.Popen(
            [
                browser,
                f"--user-data-dir={WORK / f'browser-{name}'}",
                "--no-first-run", "--no-default-browser-check",
                f"--window-position={index * width},0", f"--window-size={width},{height}",
                "--new-window", url,
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=DETACHED if detach else 0,
        )
        pids[f"window-{name}"] = process.pid
    return pids


# ------------------------------------------------------------------ commands


def start(args: argparse.Namespace) -> None:
    check_environment()
    stop(quiet=True)
    if args.fresh and WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True, exist_ok=True)
    for name, port in {"remote": REMOTE_PORT, **STUDIO_PORTS}.items():
        if port_busy(port):
            raise Failed(f"Port {port} ({name}) is in use by something this script did not start.")

    pids: dict[str, int] = {}
    try:
        print("Starting the remote...")
        keys = issue_keys()
        pids["remote"] = spawn(
            "remote",
            [sys.executable, "-m", "voxarium_studio_remote.cli", "serve",
             "--port", str(REMOTE_PORT), "--data", str(WORK / "remote-data")],
            detach=args.detach,
        )
        write_pids(pids)
        wait_for("The remote starting", lambda: healthy(REMOTE_PORT, "voxarium_studio_remote"),
                 seconds=30, log="remote.log")

        for name, port in STUDIO_PORTS.items():
            print(f"Starting {name}'s Studio...")
            project = WORK / f"{name}-project"
            project.mkdir(exist_ok=True)
            pids[name] = spawn(
                name,
                [sys.executable, "-m", "voxarium_studio.cli", "run", str(project),
                 "--port", str(port), "--no-open"],
                detach=args.detach,
                # Its own settings directory: its own key, its own sync state, and nothing
                # of this in the real recent-projects list.
                env={"VOXARIUM_STUDIO_HOME": str(WORK / f"home-{name}")},
            )
            write_pids(pids)
        for name, port in STUDIO_PORTS.items():
            wait_for(f"{name}'s Studio starting", lambda port=port: healthy(port, "voxarium_studio"),
                     seconds=90, log=f"{name}.log")

        print("Connecting both to the remote...")
        for name in STUDIO_PORTS:
            connect(name, keys[name])

        first = f"http://127.0.0.1:{STUDIO_PORTS['alice']}"
        if not call("GET", f"{first}/api/docs"):
            call("PUT", f"{first}/api/docs/{SAMPLE_DOC}", {"content": SAMPLE_TEXT, "exclusive": True})
            second = f"http://127.0.0.1:{STUDIO_PORTS['bob']}"
            wait_for("The sample document reaching bob", lambda: bool(call("GET", f"{second}/api/docs")),
                     seconds=15, log="bob.log")

        if not args.no_browser:
            pids.update(open_windows(args.detach))
            write_pids(pids)
    except BaseException:
        stop(quiet=True)
        raise

    print()
    print(f"  remote   {REMOTE_URL}   space '{SPACE}'")
    for name, port in STUDIO_PORTS.items():
        print(f"  {name:<8} http://127.0.0.1:{port}/documents?path={SAMPLE_DOC}")
    print(f"  files    {WORK}")
    print()
    if args.detach:
        print("Left running. Stop with:  remote_test.bat stop")
        return
    try:
        input("Press Enter to stop everything... ")
    except (EOFError, KeyboardInterrupt):
        print()
    stop()


def main() -> int:
    parser = argparse.ArgumentParser(prog="remote_test.bat", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("command", nargs="?", choices=["start", "stop"], default="start")
    parser.add_argument("--detach", action="store_true", help="leave everything running and return")
    parser.add_argument("--fresh", action="store_true", help="delete the test projects and server data first")
    parser.add_argument("--no-browser", action="store_true", help="do not open the two windows")
    args = parser.parse_args()
    try:
        if args.command == "stop":
            stop()
        else:
            start(args)
    except Failed as failure:
        print(f"\nremote_test: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
