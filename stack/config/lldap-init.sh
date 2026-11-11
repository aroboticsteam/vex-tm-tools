#!/bin/sh
# Create the `qecomp-admin` group in LLDAP and add `admin` to it. Safe to re-run.
# Gives up quietly if `admin` can no longer log in with the generated
# password (it was changed in the UI), the group then already exists.
set -u
apk add --no-cache curl jq >/dev/null
pass=$(cat /secrets/lldap_admin_pass)
body=$(jq -n --arg p "$pass" '{username:"admin",password:$p}')
token=""
for i in $(seq 1 30); do
  token=$(curl -sf -X POST http://lldap:17170/auth/simple/login -H 'Content-Type: application/json' -d "$body" | jq -r '.token // empty') || true
  [ -n "$token" ] && break
  sleep 2
done
if [ -z "$token" ]; then echo "could not log in to LLDAP as admin; skipping group setup"; exit 0; fi
gql() { curl -sf -X POST http://lldap:17170/api/graphql -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d "$1"; }
gid=$(gql '{"query":"{groups{id displayName}}"}' | jq -r '.data.groups[] | select(.displayName=="qecomp-admin") | .id')
if [ -z "$gid" ]; then
  gid=$(gql '{"query":"mutation{createGroup(name:\"qecomp-admin\"){id}}"}' | jq -r '.data.createGroup.id')
  echo "created group qecomp-admin ($gid)"
fi
gql "{\"query\":\"mutation{addUserToGroup(userId:\\\"admin\\\",groupId:$gid){ok}}\"}" >/dev/null || true
echo "lldap ready: admin is in qecomp-admin"
