#!/usr/bin/env python3
"""TMTools capture agent (issue #45).

Runs on the AV laptop next to the ATEM feed. It talks only to the TMTools
server: it receives `start_recording` / `stop_recording` / `mark` over
`/ws/agent`, records the feed with ffmpeg to local disk first, cuts instant-
replay clips on request, and uploads everything to the object store with
presigned PUT URLs obtained from the server.

Priorities: the live recording is never interrupted; clip cut + upload runs on
its own worker so it never waits behind the (large) match file upload.

    python agent.py --server https://tm.example.com --key qec_... \
        --input "-f avfoundation -framerate 30 -i 0:none"
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shlex
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

VERSION = "1.0"
log = logging.getLogger("capture_agent")

# How long to wait after a mark before cutting, so the muxer has flushed the
# tail of the live file to disk.
FLUSH_WAIT_S = 1.5
HEARTBEAT_S = 15
RETRY_DELAYS = (2, 5, 10, 30, 60)


@dataclass
class Settings:
    server: str
    key: str
    input_args: list[str]
    work_dir: Path
    ffmpeg: str = "ffmpeg"
    reencode: bool = True
    video_bitrate: str = "15M"
    agent_id: str = field(default_factory=lambda: f"agent-{uuid.uuid4().hex[:6]}")

    def as_report(self) -> dict[str, Any]:
        return {"reencode": self.reencode, "video_bitrate": self.video_bitrate}


def _encode_args(settings: Settings) -> list[str]:
    """Match recording: H.264 with 1 s keyframes (so clips cut cleanly), or stream copy."""
    if not settings.reencode:
        return ["-c", "copy"]
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", settings.video_bitrate,
        "-maxrate", settings.video_bitrate, "-bufsize", "2M", "-g", "30", "-keyint_min", "30",
        "-sc_threshold", "0", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
    ]


class Recorder:
    """One ffmpeg process writing a Matroska master (crash-safe while growing)."""

    def __init__(self, settings: Settings, recording_id: str, match_type: str, match_number: str) -> None:
        self.settings = settings
        self.recording_id = recording_id
        self.match_type = match_type
        self.match_number = match_number
        self.dir = settings.work_dir / recording_id
        self.master = self.dir / "master.mkv"
        self.proc: asyncio.subprocess.Process | None = None
        self.started_at = 0.0

    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        cmd = [self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *self.settings.input_args,
               *_encode_args(self.settings), "-f", "matroska", str(self.master)]
        log.info("recording %s %s: %s", self.match_type, self.match_number, " ".join(shlex.quote(c) for c in cmd))
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        self.started_at = time.monotonic()
        await asyncio.sleep(0.3)
        if self.proc.returncode is not None:
            err = (await self.proc.stderr.read()).decode(errors="replace")[-400:] if self.proc.stderr else ""
            raise RuntimeError(f"ffmpeg exited immediately: {err.strip()}")

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    async def stop(self) -> Path:
        """Finish the master and remux to a faststart MP4. Returns the MP4 path."""
        proc = self.proc
        if proc and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.stdin.write(b"q")
                await proc.stdin.drain()
            try:
                await asyncio.wait_for(proc.wait(), timeout=20)
            except asyncio.TimeoutError:
                proc.terminate()
                await proc.wait()
        out = self.dir / "match.mp4"
        await _run([self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(self.master),
                    "-c", "copy", "-movflags", "+faststart", str(out)])
        return out

    async def cut(self, moment_id: str, length_s: float, end_ms: int) -> Path:
        """Re-encode the last `length_s` seconds before `end_ms` into a standalone MP4."""
        await asyncio.sleep(FLUSH_WAIT_S)
        start_s = max(0.0, end_ms / 1000 - length_s)
        out = self.dir / f"moment-{moment_id}.mp4"
        await _run([self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start_s:.3f}",
                    "-i", str(self.master), "-t", f"{length_s:.3f}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "20", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(out)])
        return out


async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {err.decode(errors='replace')[-400:].strip()}")


async def probe_duration(ffprobe: str, path: Path) -> float | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return float(out.decode().strip())
    except Exception:
        return None


# ── upload queue ────────────────────────────────────────────────────────


@dataclass
class UploadJob:
    kind: str  # "match" | "moment"
    recording_id: str
    path: str
    moment_id: str | None = None
    duration_s: float | None = None
    ready_from: float | None = None  # wall-clock time of the mark, for ready_ms
    attempts: int = 0

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.moment_id or self.recording_id}"


class UploadQueue:
    """Persisted upload queue with a dedicated worker per lane, so clips never
    wait behind a match file. Failed jobs retry with backoff and survive restarts."""

    def __init__(self, work_dir: Path, client: httpx.AsyncClient, retry_delays: tuple[float, ...] = RETRY_DELAYS) -> None:
        self.file = work_dir / "queue.json"
        self.client = client
        self.retry_delays = retry_delays
        self.jobs: dict[str, UploadJob] = {}
        self.lanes: dict[str, asyncio.Queue[UploadJob]] = {"moment": asyncio.Queue(), "match": asyncio.Queue()}
        self.done: list[str] = []
        self.on_event: Callable[[str, UploadJob], None] = lambda *_: None
        self._workers: list[asyncio.Task[None]] = []

    def load(self) -> None:
        if not self.file.exists():
            return
        try:
            for raw in json.loads(self.file.read_text()):
                self.add(UploadJob(**raw), persist=False)
        except (ValueError, TypeError):
            log.warning("ignoring unreadable queue file %s", self.file)

    def _persist(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps([j.__dict__ for j in self.jobs.values()]))
        os.replace(tmp, self.file)

    def add(self, job: UploadJob, persist: bool = True) -> None:
        if job.key in self.jobs:
            return
        self.jobs[job.key] = job
        self.lanes[job.kind].put_nowait(job)
        if persist:
            self._persist()

    def start(self) -> None:
        self._workers = [asyncio.create_task(self._worker(lane)) for lane in self.lanes]

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        for w in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await w

    async def _worker(self, lane: str) -> None:
        q = self.lanes[lane]
        while True:
            job = await q.get()
            try:
                await self._upload(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                job.attempts += 1
                delay = self.retry_delays[min(job.attempts - 1, len(self.retry_delays) - 1)]
                log.warning("upload %s failed (%s); retry in %ss", job.key, exc, delay)
                await asyncio.sleep(delay)
                q.put_nowait(job)
            else:
                self.jobs.pop(job.key, None)
                self.done.append(job.key)
                self._persist()
                self.on_event("uploaded", job)

    async def _upload(self, job: UploadJob) -> None:
        path = Path(job.path)
        if not path.exists():
            raise FileNotFoundError(path)
        body: dict[str, Any] = {"kind": job.kind, "recording_id": job.recording_id}
        if job.moment_id:
            body["moment_id"] = job.moment_id
        grant = (await self.client.post("/api/v1/capture/uploads", json=body)).raise_for_status().json()

        async def chunks():
            with path.open("rb") as fh:
                while block := fh.read(1024 * 1024):
                    yield block

        headers = dict(grant.get("headers") or {})
        headers["Content-Length"] = str(path.stat().st_size)
        # The presigned URL points at the object store, not the TMTools server.
        async with httpx.AsyncClient(timeout=None) as store:
            (await store.put(grant["url"], content=chunks(), headers=headers)).raise_for_status()
        complete: dict[str, Any] = {"kind": job.kind}
        if job.duration_s is not None:
            complete["duration_s"] = job.duration_s
        if job.ready_from is not None:
            complete["ready_ms"] = max(0, int((time.time() - job.ready_from) * 1000))
        (await self.client.post(f"/api/v1/capture/uploads/{grant['upload_id']}/complete", json=complete)).raise_for_status()
        log.info("uploaded %s (%s)", job.key, grant.get("object_key"))


# ── the agent ───────────────────────────────────────────────────────────


class CaptureAgent:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            base_url=settings.server.rstrip("/"), headers={"Authorization": f"Bearer {settings.key}"}, timeout=60
        )
        self.queue = UploadQueue(settings.work_dir, self.client)
        self.recorder: Recorder | None = None
        self.send: Callable[[dict[str, Any]], Any] = self._drop
        self._tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    async def _drop(_: dict[str, Any]) -> None:
        return None

    @property
    def state(self) -> str:
        return "recording" if self.recorder else "idle"

    async def report(self, msg: dict[str, Any]) -> None:
        """Best effort: if the link is down the server will reconcile on reconnect."""
        with contextlib.suppress(Exception):
            await self.send(msg)

    async def handle_command(self, cmd: dict[str, Any]) -> None:
        kind = cmd.get("type")
        try:
            if kind == "start_recording":
                await self._start(cmd)
            elif kind == "stop_recording":
                await self._stop(cmd)
            elif kind == "mark":
                self._spawn(self._mark(cmd))
        except Exception as exc:  # noqa: BLE001
            log.exception("command %s failed", kind)
            await self.report({"type": "error", "detail": str(exc), "recording_id": cmd.get("recording_id"), "moment_id": cmd.get("moment_id")})

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _start(self, cmd: dict[str, Any]) -> None:
        if self.recorder:  # a new match supersedes the running one
            await self._stop({"recording_id": self.recorder.recording_id})
        recorder = Recorder(self.settings, cmd["recording_id"], str(cmd.get("match_type")), str(cmd.get("match_number")))
        await recorder.start()
        self.recorder = recorder
        await self.report({"type": "status", "state": "recording"})

    async def _stop(self, cmd: dict[str, Any]) -> None:
        recorder = self.recorder
        if recorder is None or (cmd.get("recording_id") and cmd["recording_id"] != recorder.recording_id):
            return
        self.recorder = None
        out = await recorder.stop()
        duration = await probe_duration(self.settings.ffmpeg.replace("ffmpeg", "ffprobe"), out)
        await self.report({"type": "recording_stopped", "recording_id": recorder.recording_id, "duration_s": duration})
        await self.report({"type": "status", "state": "idle"})
        self.queue.add(UploadJob("match", recorder.recording_id, str(out), duration_s=duration))

    async def _mark(self, cmd: dict[str, Any]) -> None:
        recorder = self.recorder
        if recorder is None or recorder.recording_id != cmd.get("recording_id"):
            await self.report({"type": "error", "detail": "Not recording that match", "moment_id": cmd.get("moment_id")})
            return
        marked_at = time.time()
        end_ms = recorder.elapsed_ms
        try:
            clip = await recorder.cut(cmd["moment_id"], float(cmd["length_s"]), end_ms)
        except Exception as exc:  # noqa: BLE001
            await self.report({"type": "error", "detail": f"clip cut failed: {exc}", "moment_id": cmd["moment_id"]})
            return
        await self.report({"type": "moment_cut", "moment_id": cmd["moment_id"], "end_offset_ms": end_ms})
        self.queue.add(UploadJob("moment", recorder.recording_id, str(clip), moment_id=cmd["moment_id"], ready_from=marked_at))

    # ── link to the server ────────────────────────────────────────────

    async def run(self) -> None:
        import websockets  # imported late so unit tests need not have it

        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        self.queue.load()
        self.queue.start()
        url = self.settings.server.rstrip("/").replace("http", "ws", 1) + "/ws/agent"
        attempt = 0
        while True:
            try:
                async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {self.settings.key}"}) as ws:
                    attempt = 0

                    async def send(msg: dict[str, Any]) -> None:
                        await ws.send(json.dumps(msg))

                    self.send = send
                    await send({"type": "hello", "agent_id": self.settings.agent_id, "version": VERSION,
                                "state": self.state, "settings": self.settings.as_report()})
                    heartbeat = asyncio.create_task(self._heartbeat())
                    try:
                        async for raw in ws:
                            try:
                                cmd = json.loads(raw)
                            except ValueError:
                                continue
                            if isinstance(cmd, dict):
                                await self.handle_command(cmd)
                    finally:
                        heartbeat.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("server link down (%s)", exc)
            self.send = self._drop
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await self.report({"type": "status", "state": self.state, "pending_uploads": len(self.queue.jobs)})


def parse_args(argv: list[str] | None = None) -> Settings:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default=os.environ.get("TMTOOLS_SERVER"), help="TMTools base URL, e.g. https://tm.example.com")
    p.add_argument("--key", default=os.environ.get("TMTOOLS_AGENT_KEY"), help="API key with the capture:upload permission")
    p.add_argument("--input", default=os.environ.get("CAPTURE_INPUT"), help='ffmpeg input args, e.g. "-f avfoundation -i 0:none"')
    p.add_argument("--work-dir", default=os.environ.get("CAPTURE_WORK_DIR", "./capture-data"))
    p.add_argument("--ffmpeg", default=os.environ.get("FFMPEG", "ffmpeg"))
    p.add_argument("--reencode", action=argparse.BooleanOptionalAction, default=True, help="re-encode match video to H.264 (default) or stream-copy")
    p.add_argument("--video-bitrate", default="15M")
    p.add_argument("--agent-id", default=os.environ.get("CAPTURE_AGENT_ID"))
    a = p.parse_args(argv)
    missing = [n for n in ("server", "key", "input") if not getattr(a, n)]
    if missing:
        p.error("missing: " + ", ".join("--" + m for m in missing))
    kwargs: dict[str, Any] = {}
    if a.agent_id:
        kwargs["agent_id"] = a.agent_id
    return Settings(a.server, a.key, shlex.split(a.input), Path(a.work_dir), a.ffmpeg, a.reencode, a.video_bitrate, **kwargs)


async def _main(settings: Settings) -> None:
    agent = CaptureAgent(settings)
    task = asyncio.create_task(agent.run())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    if agent.recorder:  # leave a playable file behind on Ctrl-C
        await agent._stop({"recording_id": agent.recorder.recording_id})
    await agent.queue.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_main(parse_args()))
