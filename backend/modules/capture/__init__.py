"""Match recording and instant-replay capture (issues #42, #38, #45).

The capture agent (`tools/capture_agent/`) runs on the AV laptop and connects
outward to `/ws/agent`. The server tells it what to record and clip; it
uploads the files to the object store with presigned PUT URLs.
"""
