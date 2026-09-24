# uc-hub web app

The browser app for the uc-hub gateway: a three-pane desktop console and an
agent-first phone PWA (the design is in `../docs/ai-harness/DESIGN.md` section 14
and `../docs/ai-harness/ui-mockup.html`). It speaks only the HTTP API in
`../docs/ai-harness/API.md`; everything else runs on the gateway.

Stack: Vite, React 18, TypeScript (strict), plain CSS with the mockup's design
tokens (light and dark via `prefers-color-scheme`). No UI framework, no state
library.

## Run it

```sh
pnpm install
```

### Against a hub

Start the hub (it listens on `127.0.0.1:8080` by default), then:

```sh
pnpm dev                              # http://localhost:5173, /api is proxied to the hub
UC_HUB_URL=http://10.0.2.5:8080 pnpm dev   # a hub somewhere else
```

- **Dev mode hub** (no tokens in `hub.yaml`): nothing to do, every request is user `dev`.
- **Hub with tokens**: open the app once as `/?token=<token>`. The token is
  stored in `localStorage` and removed from the address bar; every request then
  sends `Authorization: Bearer <token>`, including the event streams. Open
  `/?token=` (empty) to forget it.

For production, `pnpm build` writes `dist/`; point the hub at it with
`web_dir: ../web/dist` in `hub.yaml` and it serves the app at `/`.

### Mock mode (no hub needed)

```sh
pnpm dev          # then open http://localhost:5173/?mock=1
VITE_MOCK=1 pnpm dev
```

`?mock=1` stays on for the browser tab (`?mock=0` turns it off) and a
**Mock data** chip shows in the header. `&mockSpeed=4` plays the agent runs
four times faster.

The mock (`src/api/mock/`) implements the whole API in the page, behind
`window.fetch`, so the real client, SSE parser, reducer and UI run unchanged.
It serves the example site **HQ**: Floor 2 with rooms r201 to r205 (BACnet-uc
room controllers, r203 offline, r205 too warm), AHU-1 (third-party BACnet/IP)
in the plant room, an MQTT CO2 sensor in room 204 and two unplaced devices.
Live values random-walk. Two runs are already in progress:

- **Commission room 204** waits for the tier C approval of plan p17. Approving
  it applies the plan (room 204 gets its new points, Changes empties) and runs
  the acceptance tests on the live site. Rejecting it keeps the draft.
- **IO checkout · r204-ctl** asks the technician a question per IO channel;
  answer them on the Agent tab.

New runs answer a few scripted requests: "Which rooms are above 24 °C?",
"Why is room 205 warmer than its setpoint?", onboarding the unplaced devices,
and the two scenarios above.

## Scripts

| Script | What it does |
|---|---|
| `pnpm dev` | Vite dev server with the `/api` proxy |
| `pnpm build` | `tsc -b` and a production build into `dist/` |
| `pnpm typecheck` | TypeScript only |
| `pnpm test` | Vitest unit and component tests (jsdom; the mock server tests run in Node) |
| `pnpm e2e` | Playwright against `vite preview` in mock mode: builds, starts the preview on a random port, runs desktop and phone (390 × 844) tests |

The e2e tests use Playwright 1.56.1 with the browsers already installed in
`PLAYWRIGHT_BROWSERS_PATH` (do not run `playwright install`), and need no
internet (Google Fonts requests are aborted, the fallback fonts are used).

## Layout

```
src/api/types.ts      JSON shapes and run events of API.md
src/api/client.ts     fetch wrapper, token handling, typed ApiError, stream helpers
src/api/sse.ts        SSE over fetch: parser, Last-Event-ID resume, backoff, idle watchdog
src/api/mock/         in-browser mock backend (site state, scripted runs, routes)
src/state/            hub data (polled resources), live and run streams, the run reducer, view state
src/agent/            agent stream: tool, approval and question cards, composer, run picker
src/panes/            desktop workspace: Points, Changes, Tests, Devices
src/desktop/          top bar, site tree, three-pane shell
src/phone/            phone shell, Site and Changes tabs, the approval sheet
src/field/            Field tab and the QR scanner
src/ui/               tabs, hold button, diff view, chips and icons
public/sw.js          service worker (production builds only)
```

## Behaviour worth knowing

- **Runs** are folded from their event stream by `state/runReducer.ts`. The
  replay after (re)connecting and the live stream use the same reducer, and
  events whose `seq` was already folded are ignored, so a reconnect that
  replays overlapping events is harmless. Reconnects resume with
  `Last-Event-ID` (and `?after=`), with exponential backoff and a 45 s idle
  watchdog against silently dead connections.
- **Approvals**: tier C needs a deliberate gesture. On the desktop, Approve is
  only inside the expanded card next to the diff; on phones the Changes tab
  asks for a 1.5 s hold (pointer, or Space/Enter held), and a short tap does
  nothing.
- **Layouts**: at 1024 px and wider the three panes; below that the phone
  layout (portrait tablets get it too, in a centred column).
- **PWA**: `public/sw.js` caches the app shell (network first for pages,
  cache first for hashed assets) and never touches `/api`, so live values,
  runs and approvals always come from the gateway.
- **Scanner**: the Field tab scans QR labels with `BarcodeDetector` where the
  browser has it (Chrome on Android, macOS and ChromeOS); elsewhere, or when
  the camera is refused, it asks for the device id. A label may hold the device name, its
  BACnet instance, its hardware id, `uc:<name>` or a URL with `?device=<name>`.
- **Playbooks** are sent as `POST /api/runs {"playbook": ...}` with the ids
  `onboard`, `io-checkout` and `troubleshoot`.
