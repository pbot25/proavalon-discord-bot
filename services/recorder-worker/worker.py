from __future__ import annotations

import asyncio
import os
import signal
import sys


async def run_worker() -> None:
    browser = await asyncio.create_subprocess_exec(
        sys.executable,
        "browser/automatic_browser_recorder.py",
        "--output-dir",
        os.getenv("VIDEO_OUTPUT_DIR", "/data/browser"),
    )
    controller = await asyncio.create_subprocess_exec(
        sys.executable,
        "automatic_audio_controller.py",
    )
    processes = (browser, controller)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            loop.add_signal_handler(getattr(signal, name), stopping.set)

    waits = [asyncio.create_task(process.wait()) for process in processes]
    stop_wait = asyncio.create_task(stopping.wait())
    done, _ = await asyncio.wait(
        [*waits, stop_wait], return_when=asyncio.FIRST_COMPLETED
    )
    unexpected = not stop_wait.done()
    for process in processes:
        if process.returncode is None:
            process.terminate()
    await asyncio.gather(*(process.wait() for process in processes))
    for task in waits:
        task.cancel()
    stop_wait.cancel()
    if unexpected:
        raise RuntimeError("A recorder-worker process stopped unexpectedly.")


if __name__ == "__main__":
    asyncio.run(run_worker())
