"""Stdlib-only stdio forwarder for the run-scoped Studio MCP bridge (§10.6).

Executed by absolute path using the Studio's Python, independent of the managed cwd.
No handlers or database access live in this subprocess.
"""

from __future__ import annotations

import os
import socket
import sys
import threading


def main() -> None:
    token = os.environ.pop("VOXARIUM_STUDIO_MCP_BRIDGE_TOKEN")
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=10) as connection:
        connection.settimeout(None)
        connection.sendall((token + "\n").encode("ascii"))

        def forward_input() -> None:
            try:
                while chunk := sys.stdin.buffer.read1(64 * 1024):
                    connection.sendall(chunk)
            except OSError:
                pass
            finally:
                try:
                    connection.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        # A dead Studio must not leave a proxy blocked forever on the CLI's stdin.
        threading.Thread(target=forward_input, daemon=True).start()
        while chunk := connection.recv(64 * 1024):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
