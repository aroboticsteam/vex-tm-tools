# Authentik Setup (plan Appendix A.15)

Authentik is configured **after** the app is deployed (plan Appendix A.1).
This document lists what to create in Authentik and which env vars to set
once you have.

## 1. Create the OAuth2/OIDC application

- Application type: **OAuth2/OIDC Provider**.
- Client type: **Confidential**.
- Redirect URIs: `<app_url>/auth/callback` (e.g.
  `https://qecomp.example.org/auth/callback`). Exactly one redirect URI is
  needed — this app only ever redirects back to its own `/auth/callback`.
- Scopes: `openid`, `profile`, `email`, plus a custom scope/mapping that
  exposes the user's **group memberships** as a claim on the ID token (see
  §2). Authentik's default "openid"/"email"/"profile" scopes do not include
  groups by default — add a custom Scope Mapping for this.

## 2. Groups claim

Add a custom **Scope Mapping** (Authentik: Customization → Property
Mappings → Scope Mapping) that emits the authenticated user's group names
as a claim, e.g.:

```python
return {"groups": [group.name for group in request.user.ak_groups.all()]}
```

Attach that scope mapping to the application's provider. Whatever claim
name you give it (`groups` is the conventional default) must match the
`OIDC_GROUPS_CLAIM` env var below.

## 3. Create the default groups

Create Authentik groups matching the default role names seeded into
`role_permissions` (plan §13 — editable afterwards from the app's Users &
Roles page, `/api/v1/users/roles`):

- `qecomp-admin`
- `qecomp-operator`
- `qecomp-lighting`
- `qecomp-video`
- `qecomp-emcee`
- `qecomp-viewer`
- `qecomp-staff` (event staff who only need push alerts and room info)

Assign each event-day operator to the appropriate group(s) before the
event. Group membership is only re-evaluated at the next login (plan
Appendix B.9) — moving a user between groups mid-event requires them to
log out and back in (or an admin force-logout from the Users page).

## 4. App environment variables

Set on the QEComp deployment once the Authentik application exists:

| Env var | Value |
|---|---|
| `OIDC_ISSUER_URL` | The Authentik provider's issuer URL, e.g. `https://authentik.example.org/application/o/qecomp/` |
| `OIDC_CLIENT_ID` | The application's client ID |
| `OIDC_CLIENT_SECRET` | The application's client secret |
| `OIDC_GROUPS_CLAIM` | The claim name your Scope Mapping emits groups under (default: `groups`) |

## 5. Local emergency admin (independent of Authentik)

`ADMIN_LOCAL_PASSWORD` (env var) sets the password for the
non-Authentik `admin_local` account (`/admin_login`, plan §3.9). This
account always has every permission and is not configurable from the UI —
it exists specifically so operators can regain access if Authentik itself
is unreachable during an event. Keep this password long, random, and
different from any Authentik account's password; it is not subject to
group/RBAC changes.
