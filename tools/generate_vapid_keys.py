#!/usr/bin/env python3
"""Generate a VAPID keypair for Web Push (team/staff notifications).

Prints `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` lines ready to drop into the
environment (or the k8s secret). Generate once per deployment and keep the
private key secret; changing it invalidates every existing subscription.

    python tools/generate_vapid_keys.py
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> None:
    vapid = Vapid01()
    vapid.generate_keys()
    private_raw = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    print(f"VAPID_PRIVATE_KEY={_b64url(private_raw)}")
    print(f"VAPID_PUBLIC_KEY={_b64url(public_raw)}")


if __name__ == "__main__":
    main()
