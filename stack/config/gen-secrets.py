"""Generate every secret the stack needs into the shared /secrets volume.

Idempotent: values that already exist are never changed, so the data already
encrypted with ENCRYPTION_KEY (and the S3 key Garage knows) stay valid across
restarts and upgrades; newly introduced secrets (e.g. the LLDAP ones) are
added next to an existing /secrets/env. Delete the `secrets` volume only
together with the others.
"""
import os
import secrets
import shlex
from pathlib import Path

from cryptography.fernet import Fernet

d = Path("/secrets")
env = d / "env"
first_run = not env.exists()

if first_run:
    admin_pw = os.environ.get("ADMIN_LOCAL_PASSWORD") or secrets.token_urlsafe(18)
    values = {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ADMIN_LOCAL_PASSWORD": admin_pw,
        # Garage key ids must be "GK" + 24 hex chars; secrets are 64 hex chars.
        "S3_ACCESS_KEY": "GK" + secrets.token_hex(12),
        "S3_SECRET_KEY": secrets.token_hex(32),
        # OIDC client secret the app presents to Authelia (Authelia stores a digest).
        "OIDC_CLIENT_SECRET": secrets.token_urlsafe(32),
    }
    env.write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in values.items()))
else:
    admin_pw = ""
    for line in env.read_text().splitlines():
        if line.startswith("export ADMIN_LOCAL_PASSWORD="):
            admin_pw = shlex.split(line[len("export "):])[0].split("=", 1)[1]

def put(name, value):
    f = d / name
    if not f.exists():
        f.write_text(value)

put("garage_rpc_secret", secrets.token_hex(32))
put("garage_admin_token", secrets.token_urlsafe(32))
for name in ("authelia_jwt", "authelia_session", "authelia_storage", "authelia_oidc_hmac",
             "lldap_jwt", "lldap_key_seed"):
    put(name, secrets.token_urlsafe(48))
# The LLDAP/Authelia `admin` user starts with the same password as /admin_login.
put("lldap_admin_pass", admin_pw)
for p in d.iterdir():
    p.chmod(0o644)

if not first_run:
    print("secrets already generated (missing ones topped up)")
    raise SystemExit(0)

print("=" * 60)
print("TMTools first-run credentials (stored in the `secrets` volume)")
print("  login:    /admin_login   user: admin_local")
print(f"  password: {admin_pw}")
print("  users:    https://ldap.<BASE_DOMAIN> (LLDAP, user: admin, same password)")
print("  re-print: docker compose -f compose.stack.yml run --rm --no-deps"
      " --entrypoint cat secrets-init /secrets/env")
print("=" * 60)
