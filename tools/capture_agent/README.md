# Capture agent

Runs on the AV laptop that receives the ATEM program/camera feed (issue #45). It
records matches with ffmpeg, cuts instant-replay clips on request and uploads
everything to the object store. It talks **only to the TMTools server**: commands
arrive over `/ws/agent`, uploads go through presigned PUT URLs the server issues.
It is not involved in playback (OBS plays clips from a browser source), and it may
run on a different machine from OBS.

## Setup

1. Install [ffmpeg](https://ffmpeg.org/) (with `ffprobe`) and Python 3.10+.
2. `pip install -r requirements.txt`
3. In TMTools **Settings → API keys**, create a key with only the **`capture:upload`**
   permission and copy it.
4. Find the ffmpeg input for the feed, e.g.
   - macOS: `ffmpeg -f avfoundation -list_devices true -i ""` → `-f avfoundation -framerate 30 -i "0:none"`
   - Windows: `ffmpeg -list_devices true -f dshow -i dummy` → `-f dshow -i video="Blackmagic WDM Capture"`
   - Linux: `-f v4l2 -i /dev/video0`
5. Run it:

```bash
python agent.py --server https://tm.example.com --key qec_... --input "-f avfoundation -framerate 30 -i 0:none"
```

`TMTOOLS_SERVER`, `TMTOOLS_AGENT_KEY`, `CAPTURE_INPUT` and `CAPTURE_WORK_DIR` can be used instead of flags.

## Settings

| Flag | Default | Meaning |
|---|---|---|
| `--reencode` / `--no-reencode` | on | On: H.264 at `--video-bitrate` with 1 s keyframes (~41 GB for 245 IQ matches at 15 Mbps). Off: stream copy of the feed as-is (can be ~190 GB at ~70 Mbps). Clips are always re-encoded. |
| `--video-bitrate` | `15M` | Target bitrate when re-encoding |
| `--work-dir` | `./capture-data` | Local recordings and the upload queue (`queue.json`) |
| `--agent-id` | random | Shown in the deck |

## How it behaves

- **Local first.** Each match is written to `work-dir/<recording id>/master.mkv` (Matroska, safe to read while growing), then remuxed to a `+faststart` `match.mp4` when the match ends.
- **Clips.** `mark` cuts the last *N* seconds into a standalone MP4 about 1.5 s after the mark (so the tail is flushed) and uploads it immediately; the target is a playable clip within ~10 s.
- **Priorities.** Clip upload and match-file upload run on separate workers, so a clip never waits behind a multi-GB match file, and uploads never touch the running recording.
- **Resilience.** The upload queue is persisted and retried with backoff, so a dropped venue connection or a restart loses nothing. The WebSocket reconnects automatically; a recording in progress continues while it is down.
- **Layout in the bucket.** `{matchType}/{matchNumber}/match.mp4` and `{matchType}/{matchNumber}/segments/moment-N.mp4`. Clear between events from the server (`DELETE /api/v1/capture/recordings`).
- Ctrl-C finishes and queues the current recording before exiting.

## Tests

`backend/tests/test_capture/test_agent_tool.py` records a synthetic `lavfi` source and uploads it to the real object store through the real API.
