#!/bin/sh
# Render Authelia's config from the generated secrets (users live in LLDAP).
# Runs on every start (cheap, idempotent); the OIDC signing key is created once.
set -eu
. /secrets/env
base="${BASE_DOMAIN:-vex.localhost}"
cd /config
mkdir -p data
if [ ! -f oidc_key.pem ]; then
  mkdir -p /tmp/k
  authelia crypto pair rsa generate --bits 2048 --directory /tmp/k >/dev/null
  mv /tmp/k/private.pem oidc_key.pem
fi
client_hash=$(authelia crypto hash generate pbkdf2 --password "$OIDC_CLIENT_SECRET" | sed 's/^Digest: //')
rm -f users_database.yml

cat > configuration.yml <<YAML
theme: auto
server:
  address: "tcp://:9091"
log:
  level: info
# No SMTP on site: identity-verification codes (password reset/change) are
# written to this file. Users can also change their password in LLDAP.
notifier:
  filesystem:
    filename: /config/data/notification.txt
authentication_backend:
  password_change:
    disable: false
  password_reset:
    disable: false
  ldap:
    implementation: lldap
    address: ldap://lldap:3890
    base_dn: dc=tmtools,dc=local
    user: uid=admin,ou=people,dc=tmtools,dc=local
    # password: AUTHELIA_AUTHENTICATION_BACKEND_LDAP_PASSWORD_FILE
access_control:
  default_policy: one_factor
  rules:
    - domain: ["lighting.$base", "lights.$base"]
      policy: one_factor
      subject: ["group:qecomp-admin"]
    - domain: ["lighting.$base", "lights.$base"]
      policy: deny
session:
  cookies:
    - domain: $base
      authelia_url: "https://auth.$base"
      default_redirection_url: "https://tmtools.$base"
      name: authelia_session
storage:
  local:
    path: /config/data/db.sqlite3
identity_providers:
  oidc:
    jwks:
      - key: |
$(sed 's/^/          /' oidc_key.pem)
    claims_policies:
      tmtools:
        id_token: [email, email_verified, preferred_username, groups, name]
    clients:
      - client_id: qecomp
        client_name: TMTools
        claims_policy: tmtools
        client_secret: "$client_hash"
        public: false
        authorization_policy: one_factor
        redirect_uris:
          - "https://tmtools.$base/auth/callback"
          - "http://tmtools.$base/auth/callback"
          - "https://localhost/auth/callback"
        scopes: [openid, profile, email, groups]
        userinfo_signed_response_alg: none
        token_endpoint_auth_method: client_secret_post
YAML
echo "authelia config rendered for $base"
