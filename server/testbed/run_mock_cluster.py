from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local Mock KV260 cluster")
    parser.add_argument("--workers", type=int, default=3, choices=range(1, 21))
    parser.add_argument("--base-port", type=int, default=18081)
    args = parser.parse_args()
    processes: list[subprocess.Popen[bytes]] = []
    stopping = False

    def stop(_signum: int | None = None, _frame: object | None = None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for index in range(1, args.workers + 1):
            port = args.base_port + index - 1
            environment = os.environ.copy()
            environment["MOCK_BOARD"] = f"mock-kv260{index}"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "testbed.mock_worker:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                env=environment,
            )
            processes.append(process)
            print(f"mock-kv260{index} -> http://127.0.0.1:{port}", flush=True)
        while not stopping:
            failed = next((p for p in processes if p.poll() is not None), None)
            if failed is not None:
                return failed.returncode or 1
            time.sleep(0.2)
    finally:
        stop()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        print("Mock cluster stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
