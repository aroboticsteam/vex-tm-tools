# Plan: running TMTools on `qecomp-server`

Status: **compose changes implemented in `compose.stack.yml` (2026-11-11, untested on site); SSD, WAL archiving, DNS and the tunnel still to do** (2026-11-10, updated after the first round of decisions). Related: `source-of-truth.md` §19 (`compose.stack.yml`), §22.1 issue #46 (portable Docker deployment).

> **Update 2026-11-11:** the domain is `qerobotics.co.uk` with flat hosts (`tmtools.`, `auth.`, `s3.`, `ldap.`, `lighting.`, `comp.`, `mumble.`) instead of `tmtools.<domain>` sub-levels, so one wildcard `*.qerobotics.co.uk` covers all. `compose.stack.yml` now implements 5.2 (host routing, ACME DNS-01 via the Cloudflare token on the server), Authelia on LLDAP, the lighting tracker behind forward auth, Mumble and the tunnel as profiles. Still open: 5.1 (SSD), 5.4 (local DNS), 5.7 (WAL archiving), trusted Mumble cert.

## 1. Goal

Run the whole TMTools stack (`compose.stack.yml`) on one on-site server, with all event traffic staying on the local network:

- clients reach it by DNS names that resolve to the server's LAN address, under a **wildcard certificate** issued by a DNS-01 challenge (no inbound ports, works for a LAN-only host);
- **remote access is limited to the public competitors page** (`/site`), by a Cloudflare tunnel, with the homelab ingress as the backup path;
- as much of the stack as possible runs on the server: app, Postgres, Redis, Garage (S3), Authelia, Traefik, and Mumble voice;
- the Postgres data is backed up to the homelab cluster.

## 2. What is there today (checked 2026-11-10)

| Item | State |
|---|---|
| Server | `qecomp-server`, aarch64, 4 cores, 7.9 GiB RAM, SD card 29 GB (18 GB free). LAN address `172.16.3.36/21` from **DHCP** (the site network is behind a Smoothwall, which re-signs TLS) |
| Remote management | tunnel agent (`/opt/tunnel-agent`) gives it a tailnet address; SSH as `vivaan` with the servers key; `vmd1` CLI installed |
| Stack | already running from `/var/lib/tmtools/compose.yml` (a copy of `compose.stack.yml`), `PUBLIC_HOST=qecomp-server.local`, self-signed cert, published ports 80, 443, 3000, 3901 (Garage S3), 9091 (Authelia) |
| SSD | **1 TB SSD planned, not connected**: the server sees only the SD card (no `sd*`/NVMe device, `lsusb` shows only root hubs) |
| Existing test route | `r-tmtools.vmd1.dev` (cluster ingress to the server over a Tailscale operator egress Service). **Test only: delete at cutover** |
| Cluster | Traefik ingress, cert-manager with Cloudflare DNS-01 (`letsencrypt-cloudflare`), CloudNativePG cluster `pg-cluster` (namespace `cnpg`, 2 instances, PostgreSQL 17.2, daily Backblaze B2 backup, 7-day retention), a Tailscale subnet router |
| Compose file | `compose.stack.yml` is untracked in this repo on the branch `feat/team-recordings` |

## 3. Decisions already made

1. Plain Docker stack on the server (`compose.stack.yml`), self-contained.
2. Wildcard cert via **DNS-01**, e.g. `*.tmtools.<domain>`; **trusted wildcard for Mumble** too.
3. DNS records for the local names point at the server's **local IP**, which will be **static**.
4. Remote access: **only** `competitors.<domain>`, via a **Cloudflare tunnel**; the homelab **ingress** is the backup, and switching to it is a **manual DNS flip**.
5. The 1 TB SSD is the data disk, used through **filesystem mounts** (bind mounts into the containers, not Docker named volumes). It will be connected soon.
6. **Mumble** runs on the server too. The iPads are on the same segment as the server.
7. Postgres backup: **WAL archiving**, sent **over the tailnet** to the cluster. A **second CNPG cluster** in the `qecomp` namespace is acceptable where needed. The `secrets` backup also lives in the `qecomp` namespace.
8. It is a **one-day event**: short retention, bounded recording volume, and the focus is surviving that day, not long-term archives.
9. **Remote playback of match recordings: yes** (design still to be worked out, see 5.5).
10. **No data migration**: the server starts from a fresh stack (nothing to carry over from the cluster's `qecomp`).
11. The compose changes go in as a PR to `aroboticsteam/vex-tm-tools`.

Still to be decided: the domain (#1 in the open questions) and where the Cloudflare token lives (#2).

## 4. Target layout

Base name (placeholder): `tmtools.<domain>`. The wildcard does not cover its own apex, so the certificate carries both SANs.

| Name | Where it resolves | Served by |
|---|---|---|
| `tmtools.<domain>` | LAN only (server IP) | app via Traefik (`:443`) |
| `auth.tmtools.<domain>` | LAN only | Authelia (OIDC issuer; no `:9091`) |
| `s3.tmtools.<domain>` | LAN only | Garage S3 (presigned URLs; no `:3901`) |
| `mumble.tmtools.<domain>` | LAN only | Mumble (voice, not HTTP) |
| `competitors.<domain>` | public (Cloudflare tunnel, backup: ingress) | `/site` and the public API/static paths only |

Ports on the server after the change: `80`/`443` (Traefik), and `64738` tcp+udp (Mumble). The app's direct `3000` port was removed (hardening). `9091` and `3901` go away.

## 5. Workstreams

### 5.1 Storage (needs the SSD)

- Connect the SSD (use a powered enclosure or a supply that can feed it: a Pi's USB ports may not power a 1 TB bus-powered drive); confirm `lsblk` shows it.
- ext4, mounted by UUID in `/etc/fstab` with `nofail` (a missing disk must not block boot), at `/srv/tmtools`. Containers must not start if the mount is missing (otherwise they write to the SD card): use a systemd `RequiresMountsFor=` drop-in for the stack, or a start-up check.
- **Filesystem mounts instead of named volumes.** The compose file bind-mounts directories on the SSD: `/srv/tmtools/postgres`, `/srv/tmtools/garage/{meta,data}`, `/srv/tmtools/redis`, `/srv/tmtools/secrets`, `/srv/tmtools/authelia`, `/srv/tmtools/certs` (ACME state), `/srv/tmtools/mumble`, `/srv/tmtools/backup-spool`. Directory ownership has to match each image's user (the Alpine Postgres image uses uid 70, Garage and Authelia have their own); the setup script creates them with the right owner and mode `0700` for `postgres`, `secrets` and `certs`.
- Docker's own data root (images, about 1.8 GB now) can stay on the SD card; only the data directories need the SSD.
- Sizing for a **one-day event**: recordings are the big item (source of truth #45: about 41 GB at 15 Mbps up to ~190 GB as-is for 245 matches). One day is a fraction of that; confirm the match count and bitrate (open question) and check that 1 TB is ample.
- After the swap: check free space and SMART health, reboot, confirm the stack comes back with the disk mounted.

### 5.2 Compose changes (in this repo, as a PR, not edited on the box)

- Host-based routing on `:443` in Traefik; remove the `:3901` and `:9091` entrypoints.
- `OIDC_ISSUER_URL=https://auth.<base>`, `S3_PUBLIC_ENDPOINT_URL=https://s3.<base>`, `PUBLIC_BASE_URL=https://<base>`; Authelia cookie domain = `<base>`, session `authelia_url=https://auth.<base>`, OIDC redirect URIs for the new host.
- Network aliases on Traefik for `auth.<base>` and `s3.<base>`, so containers reach them at the same URL browsers use.
- `PUBLIC_HOST` is currently a single hostname feeding every URL; split it into a base name.
- Replace `cert-init` (self-signed) with the ACME flow in 5.3; the app's `SSL_CERT_FILE` bundle becomes system roots + the Smoothwall CA (5.9).
- Existing secrets, Postgres, Garage and Authelia volumes carry over: no reinstall, no data loss. Authelia's cookie and issuer change, so everyone logs in again once.
- Replace every named volume with a bind mount under `/srv/tmtools` (5.1).
- Replace `postgres:16-alpine` with a small image that adds `barman-cli-cloud`, and set the archiving options (5.7); add the `traefik-certs-dumper` sidecar and the Mumble service (5.6).
- Add the `cloudflared` service for the tunnel (5.5), behind a compose profile so the stack still starts without a token.
- Add a backup service (cron-style) for the base backups and the `secrets` copy.
- Set the static IP on the host, outside the compose file (5.4).
- Update `source-of-truth.md` §19/§22.1 in the same change, as `CLAUDE.md` requires.

### 5.3 Wildcard certificate (DNS-01)

- Traefik's built-in ACME with the Cloudflare DNS-01 provider, certificate for `tmtools.<domain>` + `*.tmtools.<domain>`, stored on a volume, renewed automatically.
- Cloudflare API token scoped to **DNS edit on the one zone**, held in a secret on the server only.
- The site network re-signs TLS, so Traefik must trust the Smoothwall CA to reach Let's Encrypt and the Cloudflare API (lego honours `LEGO_CA_CERTIFICATES`).
- A real certificate also fixes iOS web push, and the capture laptop uploading to Garage, which fail on a self-signed one.
- Certificate Transparency will list the wildcard but not the individual names.
- Alternative if the token should not live on the server: issue the wildcard in the cluster (cert-manager) and copy it to the server. More moving parts; see open questions.

### 5.4 Local DNS (names to the server's LAN IP)

The server gets a **static IP** on the site LAN (`172.16.0.0/21`, gateway `172.16.1.10`; it currently has `172.16.3.36` from DHCP). Pick an address outside the DHCP range and tell me which; it is set on the server (NetworkManager) and the Smoothwall's DHCP must not hand it out.

- **A. Public Cloudflare records** `*.tmtools` and the apex → the LAN IP. Simplest. Risks: some resolvers drop private answers (DNS-rebind protection), and local names stop resolving if the WAN or Cloudflare is down.
- **B. Smoothwall local DNS override.**
- **C. A small DNS server on the box** that answers the base name, with clients pointed at it.
- Recommendation: A plus B or C, so everything works with the internet down. Test from a client on the Smoothwall network.

### 5.5 Remote access: `competitors.<domain>` only

- **Primary: Cloudflare tunnel** (`cloudflared` as a compose service). The tunnel ingress for `competitors.<domain>` points at `http://traefik:80` with that Host header; Cloudflare terminates TLS, so no origin certificate is needed.
- Traefik on `:80` allows only the competitors host, and only these paths: `/site`, `/site/leaderboard`, the public API and static assets the page loads (the exact allowlist is derived from the routers at implementation time). Admin hosts are never served to the tunnel.
- **Not exposed remotely:** `/room`, `/notify`, `/station`, `/prompter`, `/overlay`, the API, `/ws/*` (except what the site itself needs).
- **Match recordings (remote playback wanted).** The site hands out presigned URLs on `s3.<base>` (a single `S3_PUBLIC_ENDPOINT_URL`), which is LAN-only today. For remote viewers the URL has to work from both sides, so the likely design is:
  - expose `s3.<base>` through the tunnel too, **GET/HEAD only** and only the recordings bucket paths, with the same name resolving to the LAN IP locally (this needs DNS option B or C, not A, because one name cannot have both the public tunnel record and a public record pointing at the LAN);
  - or give remote viewers a separate public name for downloads and have the site sign with it for them (needs an app change).
  - Either way uploads (the capture laptop) stay LAN-only, and bandwidth out of the venue limits playback. To be worked out next.
- **Backup path: homelab ingress.** The ingress terminates TLS for `competitors.<domain>` and proxies over the tailnet to the server's port 80 with the same Host header; the path allowlist stays on the server's Traefik, so the ingress cannot widen it. Failing over is a DNS record change at first; automation can follow.
- **Risk:** `cloudflared` needs outbound **7844** (TCP/UDP), not 443, and it validates Cloudflare's edge certificate, which TLS re-signing breaks. It may not connect at all from the Smoothwall network. Test this first; if it fails, the ingress path becomes the primary.

### 5.6 Mumble (desk and iPad voice)

- Add a Mumble server to the compose stack (`mumblevoip/mumble-server`, arm64 images exist). `k8s/mumble.yaml` shows what is expected but has **never been tested in a cluster**.
- Ports `64738` tcp+udp on the LAN only: not behind Traefik, not in the tunnel.
- Server password generated in `secrets-init` (not the placeholder in the manifest); data on the SSD.
- Certificate: the **trusted wildcard** for `mumble.<base>`. Traefik keeps its certificates in `acme.json`, which Mumble cannot read, so a `traefik-certs-dumper` sidecar writes `mumble.<base>`'s cert and key as files into a directory that is bind-mounted read-only into Mumble. Mumble reads the files at start, so restart it after a renewal (a small hook on the dumper, or a nightly restart; the cert renews about every 60 days, so a one-day event only needs the cert valid on the day).
- DNS: `mumble.<base>` on the same local DNS as above.
- The server and the iPads are on the same segment (confirmed).
- Push-to-talk on iPad is **unverified** (source of truth §22): test with at least two iPads before the event.

### 5.7 Backups to the cluster: WAL archiving over the tailnet

Goal: during the one-day event, the cluster always holds a copy of the database that is at most a few seconds behind, plus a base backup from before the event, so the data can be restored to any moment of that day.

- **Archiving method.** CloudNativePG restores from archives in the **barman-cloud** format, and it can recover a new cluster from archives written by a non-CNPG Postgres. So the server's Postgres archives with the barman-cloud tools (`barman-cloud-wal-archive` as `archive_command`, `barman-cloud-backup` for base backups), not pgBackRest or wal-g, whose layouts CNPG cannot read.
  - The stack uses a small custom Postgres 16 image (the official one plus `barman-cli-cloud`) and the config `archive_mode=on`, `archive_timeout=60s` (WAL is shipped at least every minute even when quiet). The archive command must not fail the database if the cluster is unreachable: it retries, and Postgres keeps the WAL until it succeeds (watch disk use on the SSD: `/srv/tmtools/postgres` must have headroom for a long outage).
  - A base backup runs just before the event starts, once more at the end, and every few hours in between (the database is small; this is cheap).
- **Where the archive goes.** An S3 endpoint in the cluster's `qecomp` namespace (a small MinIO or Garage with a `qecomp-wal` bucket), exposed **to the tailnet only** (a Tailscale operator Service annotation), so the server pushes to it as a tailnet address; nothing is published to the internet. Your ACL needs a rule letting the agent's tag reach that service; that file stays yours (`tailscale/policy.hujson`).
- **The second CNPG cluster** (`qecomp-pg`, namespace `qecomp`, **PostgreSQL 16** image to match the server) is for restores:
  - used **on demand** to prove a restore works, and after the event to restore the day's data into the cluster (`bootstrap.recovery` from the archive, with `recoveryTarget.targetTime` for point-in-time);
  - optionally kept running as a warm copy during the event if you want to fail the app over to the cluster; not needed otherwise.
  - Its own B2 backups are optional for a one-day copy; skip them unless you want the restored data kept.
- **Not used:** physical streaming replication to CNPG (works only for the same major version and keeps a connection open over the tailnet) and logical replication (breaks on every Alembic migration). Streaming remains an option if you later want a hot standby.
- **Secrets backup.** The `secrets` directory (it holds `ENCRYPTION_KEY`; losing it makes stored integration credentials unreadable) and the Authelia config are copied to the `qecomp` namespace as an encrypted Kubernetes Secret (created by hand with `kubectl`, like the existing `qecomp-secrets`; or a sops file if you prefer it in git). Do this once after first start, and again after any change.
- **Garage recordings** are not part of the database archive. Recommended: **no multi-replica Garage** across the server and the cluster, but a **one-way `rclone` copy** of the recordings bucket to an S3 bucket in the cluster over the tailnet:
  - Garage replication writes through the WAN link: with `replication_factor=2` and the default consistency, a write needs both nodes, so an uploaded match would fail whenever the tailnet or the venue's internet is down (and the agent may be on the slow `tailscale-wss` relay). Relaxing the consistency (`consistency_mode = "dangerous"`) avoids that but makes the second copy best-effort. The RPC port (3902) would also have to cross the tailnet, with the same Garage version on both sides.
  - Replication copies deletes and corruption too, and recordings are cleared between events, so it is not a backup.
  - A one-way copy is decoupled from the live system, can be rate-limited (`--bwlimit`) so it never starves the tunnel, and can run after each match block or only after the event.
  - If a hot copy of recordings in the cluster is wanted for a failover app, the same one-way copy serves it.
- **Retention:** a few days of WAL and base backups is plenty; prune after the restore is confirmed.
- **Restore drill before the event:** create `qecomp-pg-restore` from the archive in the `qecomp` namespace, check the data, delete it. Also practice bringing a fresh stack up from the archive plus the `secrets` copy.

### 5.8 Image supply and updates

- The server's network blocks ghcr.io; `ghcr.io/aroboticsteam/vex-tm-tools:backend` is not in `registry.kmnet.uk`. Add it to the registry mirror job (or pull through the Smoothwall CA) so updates work on site.
- Other images (Traefik, Postgres, Redis, Garage, Authelia, Mumble, cloudflared) come from Docker Hub/other registries: check they are reachable or mirror them too.

### 5.9 Smoothwall CA

- Install the Smoothwall root CA on the host (`/usr/local/share/ca-certificates/…crt`, `update-ca-certificates`, restart Docker) so `apt` and `docker pull` work.
- Add it to the containers that make outbound HTTPS calls: Traefik/lego (ACME), the app (VEX Events API, Spotify, web push), `cloudflared` (if it can work at all).
- The tunnel agent itself works without it (`VERIFY_TLS=false`, DoH unverified).

## 6. Verification checklist

- Storage: SSD mounted by UUID and surviving a reboot; containers refuse to start without the mount; all data directories on the SSD, none on the SD card.
- Stack: every service healthy after `docker compose up -d`; login works at `auth.<base>`; an upload and a presigned playback work at `s3.<base>`; OBS overlays, prompter and station pages work over the new names.
- Certificate: issued, renewal dry-run works; a phone on the LAN shows it as trusted; Mumble presents the trusted cert.
- DNS: the static IP is set; from a client on the Smoothwall network all names resolve to it; repeat with the WAN down.
- Remote: `competitors.<domain>` reachable from outside via the tunnel; every other path and host returns nothing; repeat via the ingress backup (manual DNS flip); remote playback of a recording works.
- Mumble: two iPads and a desk client connect, with and without internet.
- Backups: WAL reaches the cluster within about a minute of a write; a base backup completes; a restore into `qecomp-pg-restore` at a chosen time works; the `secrets` copy is retrievable; the archive command survives the tailnet being down for a while (WAL queues, nothing is lost).
- Behind the Squid MITM test rig, and with outbound access blocked, everything above that should work offline does.

## 7. Order of work

1. Connect the SSD; format, mount by UUID, create the data directories with the right owners.
2. Set the static IP; Smoothwall CA on the host and in the containers.
3. Decide the remaining open questions below (domain, token location, recordings design).
4. Compose PR to `aroboticsteam/vex-tm-tools`: bind mounts, host-based routing, new URLs, ACME, Mumble, backup image, `cloudflared`; update `source-of-truth.md` (§19, §22.1 #46).
5. Cluster side: the S3 endpoint exposed to the tailnet (and the ACL rule, yours), the `qecomp-pg` restore cluster definition.
6. Issue the wildcard cert; set up local DNS.
7. Cloudflare tunnel; test port 7844 first. Ingress backup path.
8. Backups running; practice a restore.
9. Run the verification checklist (rig and on site).
10. Cut over; delete `r-tmtools.vmd1.dev`, the old self-signed cert and the old `PUBLIC_HOST`.

## 8. Risks

- The tunnel may not work through the Smoothwall (port 7844, TLS re-signing).
- Local names that depend on public DNS stop working with the WAN down unless there is a local resolver; exposing `s3.<base>` through the tunnel for remote playback also rules out DNS option A.
- The SSD is a single disk with no redundancy: WAL archiving to the cluster is the protection, so test the restore.
- If the cluster or the tailnet is unreachable for long, WAL piles up on the SSD; size and monitor it.
- Mumble and push-to-talk on iPad are untested.
- Image updates depend on the mirror or the Smoothwall CA.
- First login after cutover is a new login for everyone (Authelia issuer and cookie change).
- The remote-playback design (5.5) may need a small app change.

## 9. Open questions

1. **Domain:** which domain for `tmtools.<domain>` and `competitors.<domain>`? (To be decided.) The tunnel hostname must be on Cloudflare.
2. **Cloudflare token:** (to be decided) keep a zone-scoped DNS-edit token on the server, or have the cluster issue the wildcard and copy it over?
3. **Static IP:** which address on `172.16.0.0/21`, and is it outside the Smoothwall's DHCP range?
4. **DNS option:** A, B or C (5.4)? Remote playback of recordings needs B or C. Can you set a DNS override on the Smoothwall, or should the box run a resolver?
5. **Remote recordings:** which design (5.5): `s3.<base>` through the tunnel with GET/HEAD only, or a separate public download name (needs an app change)? How much bandwidth does the venue have for outbound playback?
6. **Match volume:** how many matches and what capture bitrate, so the SSD and the WAL-spool headroom can be checked?
7. **Archive endpoint:** a new MinIO/Garage in the `qecomp` namespace exposed to the tailnet, or reuse an existing bucket? Which tag should the server's agent use in the ACL rule (yours to add)?
8. **Recordings copy:** should the day's Garage recordings be copied to the cluster after the event?
9. **Secrets backup form:** an encrypted Kubernetes Secret in `qecomp`, or a sops file in git?
10. **Hot standby:** do you want `qecomp-pg` kept running during the event as a warm copy, or only started for restore drills and after the event?
11. **SSD:** please connect it and tell me, so I can check the device and the powered-enclosure question.
12. **Failover:** a manual DNS flip to the ingress backup is agreed; who does it during the event, and is a short runbook enough?
