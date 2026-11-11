# Server hardening (qecomp-server)

Audit of 2026-11-11 and what is applied in `stack/*.yml` versus what needs doing on the host (root).

## In the compose stack (applied by deploying `main`)
- App is not published on `:3000`; only Traefik (80/443), DNS on the LAN address, and Mumble listen.
- `no-new-privileges` on app, Traefik, Authelia, Redis; `cap_drop: ALL` on app, Authelia, Traefik (+`NET_BIND_SERVICE`).
- Traefik adds `X-Content-Type-Options`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy`, `Permissions-Policy` and removes `Server`; TLS 1.2 minimum.
- Postgres, Redis, Garage, LLDAP and Authelia are internal-only.

## On the host (run as root, in order; keep a second SSH session open)
```sh
# 1. SSH: keys only, no root, no X11  (confirm your key login works first!)
sudo tee /etc/ssh/sshd_config.d/10-hardening.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
X11Forwarding no
MaxAuthTries 3
CONF
sudo sshd -t && sudo systemctl reload ssh

# 2. Drop services nobody needs
sudo systemctl disable --now rpcbind.service rpcbind.socket avahi-daemon.service avahi-daemon.socket

# 3. Brute-force protection and automatic security patches
sudo apt-get update && sudo apt-get install -y fail2ban unattended-upgrades nftables
sudo systemctl enable --now fail2ban unattended-upgrades

# 4. Host firewall for the host's own services (SSH etc.). Docker-published ports (80/443/53) are
#    forwarded to containers and are not touched by this INPUT filter; they are LAN-only because the
#    server has no public address. Own table, so Docker's and Tailscale's rules are left alone.
sudo tee /etc/nftables.conf <<'CONF'
table inet host
delete table inet host
table inet host {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    iif lo accept
    ip protocol icmp accept
    iifname "tailscale0" accept
    ip saddr 172.16.0.0/16 tcp dport { 22, 80, 443, 64738 } accept
    ip saddr 172.16.0.0/16 udp dport { 53, 64738, 6454 } accept
    ip saddr 172.16.0.0/16 tcp dport 53 accept
  }
}
CONF
sudo nft -c -f /etc/nftables.conf && sudo systemctl enable --now nftables
```
Check SSH from the LAN and over Tailscale in a second session before closing the first.

## Known residual risks
- The wildcard certificate is self-signed (no HSTS possible); move to ACME when the domain is reachable.
- Traefik `web` entrypoint trusts forwarded headers (needed for the homelab ingress); keep `:80` LAN/tailnet only.
- Anyone on the same LAN/Wi-Fi as the server can reach the login pages; put team devices on a separate VLAN.
- Full-disk encryption is not enabled; physical access to the Pi exposes the database.
