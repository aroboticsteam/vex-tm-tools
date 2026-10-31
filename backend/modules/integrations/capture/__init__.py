"""Capture agent control (issues #42, #38, #45).

Lets automations and button pages start/stop the match recording and mark
replay moments on the local ffmpeg capture agent. The agent itself connects
outward to `/ws/agent`; this integration reports DEGRADED while it is absent.
"""
