# Desk ↔ iPad comms (issue #8)

Two independent pieces:

1. **Screens and messages** (built into TMTools): the desk operator pushes
   messages/screens to iPads from **Comms** in the admin UI.
2. **Voice**: a [Mumble](https://www.mumble.info/) server (`k8s/mumble.yaml`).
   TMTools does not proxy voice.

## Setting up iPads

1. In **Comms → Stations**, add one station per iPad with a role:
   - `emcee` – messages, next-match countdown (+ upcoming teams and stats), day and finals agenda
   - `floor_manager` / `camera` – messages, day and finals agenda
2. Copy the station link (`/station/<name>`) onto the iPad and *Add to Home
   Screen* (full-screen, no browser bars). The link is the only credential,
   like a room link — use a hard-to-guess name.
3. Each iPad can type back to the desk; replies show under *Messages from
   stations*, and staff with push notifications enabled get a push.

## Operating

- **Preset messages** and custom text can be sent to everyone, one role, or one
  station; tick **Flash** to make the iPad flash until someone taps *Got it*.
- **Show screen** switches what the iPads display. A new message always takes
  the screen. iPads can also switch tabs themselves (e.g. to read the agenda).
- Agendas are edited under *Comms → Day agenda / Finals agenda*; the current
  item (by the iPad's clock, using start/end times) is shown large and bold.
- The countdown shows the earliest unscored match from the TM schedule cache.

## Voice (Mumble)

Deploy `k8s/mumble.yaml` (set the server password secret first), expose
`64738/tcp+udp` to the iPads (it is *not* behind the HTTP ingress), then install
the Mumble client on each iPad (or a compatible app such as Mumla/Mumble for iOS)
and connect with the server password. Use channels such as *Desk*, *Emcees*, *Floor*.

> Not verified: the Mumble manifest has not been run in a real cluster, and push-to-talk/headset behaviour on iPad is untested.
