"""Exercise Windows ownership with real disposable processes, never a Studio server."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows desktop lifetime")

WORKER = r'''
import json, os, subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
from voxarium_studio import desktop
desktop.SHUTDOWN_GRACE_SECONDS = float(sys.argv[2])
server = SimpleNamespace(should_exit=False)
desktop.supervise(server)
orphan = subprocess.check_output([sys.executable, '-c', """
import subprocess, sys
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(p.pid, flush=True)
"""], text=True)
print(json.dumps({'orphan': int(orphan)}), flush=True)
while not server.should_exit:
    time.sleep(.02)
if sys.argv[1] == 'hung':
    time.sleep(120)
time.sleep(.2)
Path(os.environ['VOXARIUM_STUDIO_DESKTOP_STOP_FILE'] + '.flushed').touch()
'''


class ProcessHandle:
    def __init__(self, pid):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        self.kernel.OpenProcess.restype = ctypes.c_void_p
        self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.kernel.OpenProcess(0x00100001, False, pid)
        assert self.handle

    def exited(self):
        return self.kernel.WaitForSingleObject(self.handle, 0) == 0

    def close(self):
        if not self.exited():
            self.kernel.TerminateProcess(self.handle, 1)
        self.kernel.CloseHandle(self.handle)


@pytest.mark.parametrize("mode", ["graceful", "crash", "hung", "parent_exit"])
async def test_desktop_exit_cleans_orphaned_descendants(tmp_path, mode):
    stop = tmp_path / "instance.stop"
    parent = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(120)",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    worker = await asyncio.create_subprocess_exec(
        sys.executable, "-c", WORKER, "hung" if mode == "hung" else "normal", "1.5",
        env={**os.environ, "VOXARIUM_STUDIO_DESKTOP_PARENT": str(parent.pid),
             "VOXARIUM_STUDIO_DESKTOP_STOP_FILE": str(stop)},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    orphan = None
    try:
        line = await asyncio.wait_for(worker.stdout.readline(), 10)
        if not line:
            pytest.fail((await worker.stderr.read()).decode())
        orphan = ProcessHandle(json.loads(line)["orphan"])
        assert not orphan.exited()
        if mode == "crash":
            worker.kill()
        elif mode == "parent_exit":
            parent.kill()
            await parent.wait()
        else:
            stop.write_text("stop")
        await asyncio.wait_for(worker.wait(), 8)
        for _ in range(100):
            if orphan.exited():
                break
            await asyncio.sleep(.02)
        assert orphan.exited(), "A tool whose parent already exited must not survive the backend"
        assert Path(str(stop) + ".flushed").exists() == (mode in {"graceful", "parent_exit"})
    finally:
        if worker.returncode is None:
            worker.kill()
        await worker.wait()
        if parent.returncode is None:
            parent.kill()
        await parent.wait()
        if orphan:
            orphan.close()


@pytest.mark.parametrize("mode", ["normal", "hung", "recycled_pid"])
async def test_desktop_stop_helper_waits_for_the_owned_process(tmp_path, mode):
    stop = tmp_path / "instance.stop"
    worker = await asyncio.create_subprocess_exec(
        sys.executable, "-c", WORKER, "normal" if mode == "normal" else "hung", "60",
        env={**os.environ, "VOXARIUM_STUDIO_DESKTOP_PARENT": str(os.getpid()),
             "VOXARIUM_STUDIO_DESKTOP_STOP_FILE": str(stop)},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    orphan = None
    try:
        line = await asyncio.wait_for(worker.stdout.readline(), 10)
        if not line:
            pytest.fail((await worker.stderr.read()).decode())
        orphan = ProcessHandle(json.loads(line)["orphan"])
        if mode == "recycled_pid":
            owner = Path(str(stop) + ".owner.json")
            data = json.loads(owner.read_text())
            data["created"] = "0"
            owner.write_text(json.dumps(data))
        helper = Path(__file__).resolve().parents[2] / "desktop/scripts/stop-backend.ps1"
        command = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(helper), "-StopFile", str(stop), "-WaitMilliseconds", "700",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _, errors = await asyncio.wait_for(command.communicate(), 15)
        assert command.returncode == 0, errors.decode()
        if mode == "recycled_pid":
            # No valid ownership: helper must return without waiting for or killing it.
            assert not orphan.exited()
        else:
            await asyncio.wait_for(worker.wait(), 3)
            for _ in range(100):
                if orphan.exited():
                    break
                await asyncio.sleep(.02)
            assert orphan.exited()
            assert Path(str(stop) + ".flushed").exists() == (mode == "normal")
    finally:
        if worker.returncode is None:
            worker.kill()
        await worker.wait()
        if orphan:
            orphan.close()


async def test_update_handoff_survives_the_old_backend_job(tmp_path):
    source = r'''
import os, sys, time
from types import SimpleNamespace
from voxarium_studio.desktop import supervise
from voxarium_studio.updates.relauncher import _spawn
supervise(SimpleNamespace(should_exit=False))
handoff = _spawn([sys.executable, '-c', 'import time; time.sleep(120)'],
                 cwd=None, log=None, detach=True)
print(handoff.pid, flush=True)
time.sleep(120)
'''
    worker = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source,
        env={**os.environ, "VOXARIUM_STUDIO_DESKTOP_PARENT": str(os.getpid()),
             "VOXARIUM_STUDIO_DESKTOP_STOP_FILE": str(tmp_path / "restart.stop")},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    handoff = None
    try:
        line = await asyncio.wait_for(worker.stdout.readline(), 10)
        if not line:
            pytest.fail((await worker.stderr.read()).decode())
        handoff = ProcessHandle(int(line))
        worker.kill()
        await worker.wait()
        assert not handoff.exited(), "The update relauncher must survive the old backend"
    finally:
        if worker.returncode is None:
            worker.kill()
        await worker.wait()
        if handoff:
            handoff.close()
