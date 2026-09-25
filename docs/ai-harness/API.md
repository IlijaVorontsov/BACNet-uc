# uc-hub HTTP API (v1)

Contract between the browser app (`web/`) and the gateway (`hub/`). All
paths are under `/api`. Bodies are JSON (UTF-8). Times are Unix seconds
(float). Point IDs are `<site>/<device>/<object>` (see
`hub/src/uc_hub/core/ids.py`).

The JSON shapes below match the `to_json()` methods in
`hub/src/uc_hub/core/types.py` and the store's row helpers
(`hub/src/uc_hub/store/db.py`); the endpoints are in `hub/src/uc_hub/api/`.
The TypeScript mirror is `web/src/api/types.ts`. API responses carry
`Cache-Control: no-store`. JSON is strict: NaN and infinities (a faulty
sensor) are sent as `null`.

## Authentication

- Every request carries `Authorization: Bearer <token>`. SSE requests cannot
  set headers from `EventSource`, so the two event streams (`/api/live` and
  `/api/runs/{id}/events`) may pass `?access_token=<token>` instead; other
  endpoints do not accept it. `GET /api/health` needs no token.
- Tokens and their users and roles are configured in `hub.yaml`
  (`auth.tokens`). When no tokens are configured, the hub runs in **dev
  mode**: every request is user `dev` with all roles. The hub then refuses
  to start on a `listen.host` that is not a loopback address unless
  `--insecure-listen` is given.
- Roles: `viewer`, `operator`, `commissioner`, `admin`; each includes the
  ones before it.
- Errors: `{"error": {"code": str, "message": str, "details"?: any}}`:

  | Status | `code` | When |
  |---|---|---|
  | 400 | `invalid`, `validation` | a malformed request: bad JSON (also NaN), not an object, unknown or missing fields, a string that is not valid Unicode (a lone surrogate), a bad query parameter; `validation` for a manifest that does not validate (`details` lists the errors) |
  | 401 | `unauthorized` | no or an unknown token (`WWW-Authenticate: Bearer`) |
  | 403 | `denied` | the role may not do this, or the policy refuses it |
  | 404 | `not_found` | unknown device, run, question, approval or route |
  | 409 | `conflict` | the object is not in a state that allows it: an approval already decided or expired, a question already answered or expired, a message to a running run, a stale plan |
  | 501 | `unsupported` | the device or driver cannot do it |
  | 502 | `device_error` | the device answered with an error or did not answer a sweep |
  | 504 | `timeout` | the device did not answer in time |
  | 500 | `error` | a bug; the hub log has the details |

## Site and points

| Method and path | Response |
|---|---|
| `GET /api/health` | `{"ok": true, "version": "0.1.0", "site": "hq", "llm": {"provider": "zai", "model": "glm-5.3", "configured": bool}, "dev_mode": bool}` (no token needed) |
| `GET /api/me` | `{"user": "dev", "roles": ["viewer", ..., "admin"]}` (in increasing order of authority) |
| `GET /api/site` | `Site` |
| `GET /api/devices/{name}` | `DeviceDescription` (the last description; a device never described is asked now, and when it does not answer `points` is empty and `extra.error` says why) |
| `GET /api/points?q=&device=&space=&tag=&limit=200&offset=0` | `{"total": int, "points": [Point & {"reading"?: Reading}]}`: every word of `q` in the id, name, description, tags, source, device or space; `space` includes its child spaces; `limit` 1..1000; `reading` is the latest cached value |
| `POST /api/points/read` `{"ids": [str]}` | `{"readings": [Reading]}` (1..500 full or device-relative ids; an unknown point reads `fault`) |
| `GET /api/live?ids=a,b` or `?device=r204-ctl` (SSE) | Events `reading` with a `Reading` payload. The first events are the current cached values. Keep-alive comments every 15 s. While the stream is open its points are watched (polled or subscribed); the watch is released when the client goes away. |
| `POST /api/discover` `{"protocol"?: "bacnet-uc"\|"bacnet-ip"\|"mqtt", "timeout_s"?: float}` | `{"devices": [DiscoveredDevice & {"known": bool, "device": str \| null}], "errors": {protocol: str}}` (`device` is the manifest name of a known device; `errors` lists the sweeps that failed) |
| `POST /api/devices/{name}/identify` `{"seconds"?: int}` | `{}`: operator or above; runs as a tier L `device_identify` call that the caller approves themselves (policy checked, audited); `seconds` 1..3600, default 30 |

```jsonc
// Site
{
  "name": "hq", "description": "HQ building",
  "spaces": [{"id": "f2", "name": "Floor 2", "parent": null}, {"id": "r204", "name": "Room 204", "parent": "f2"}],
  "devices": [Device],
  "summary": {"devices": 7, "online": 6, "points": 42, "unassigned": 2, "pending_changes": 3}
}
// Device
{"name": "r204-ctl", "protocol": "bacnet-uc", "address": "10.0.2.51:1337", "online": true,
 "managed": true, "model": "nucleo_f767zi", "firmware": "0.1.0", "hwid": "", "instance": 2041,
 "space": "r204", "last_seen": 1790290000.1, "points": 6}
// DeviceDescription
{"device": Device, "points": [Point], "apps": [AppStatus], "extra": {}}
// Point
{"id": "hq/r204-ctl/analog-input:1", "device": "r204-ctl", "obj": "analog-input:1",
 "name": "R204 Temp", "kind": "input", "datatype": "real", "units": "degrees-celsius",
 "writable": false, "commandable": false, "tags": ["Zone_Air_Temperature_Sensor"],
 "safety": "normal", "description": "", "source": "io:ai0", "space": "r204"}
// Reading
{"id": "hq/r204-ctl/analog-input:1", "value": 22.4, "ts": 1790290000.5, "quality": "good",
 "priority"?: 12, "error"?: "..."}
```

`quality` is one of `good`, `stale`, `fault` or `offline`. `kind` is one of
`input`, `output` or `value`. `safety` is one of `normal`, `critical` or
`life-safety`. `datatype` has one meaning for every protocol: `real` (a
float), `int` (a whole number: multi-state state numbers 1..n, counters),
`enum` (a two-state value, 0 = inactive/off, 1 = active/on: BACnet binary
objects), `bool` or `string`.

## Manifest, plan, apply, tests

| Method and path | Response |
|---|---|
| `GET /api/manifest` | `{"live_revision": int, "draft_revision": int \| null, "yaml": str, "draft_yaml": str \| null}` |
| `GET /api/manifest/revisions` | `{"revisions": [{"revision", "created_at", "author", "message", "live": bool}]}` |
| `GET /api/plan` | `{"plan": Plan \| null}`, the latest plan of the current draft (null before planning, after an edit and after a successful apply) |
| `GET /api/tests` | `{"results": [TestResult], "updated_at": float \| null}` |

Applying a plan is only possible through an approval (see below). There is
no direct "apply" endpoint. `blocked` maps the targets that could not be
planned (a node that did not answer) to the reason; such a plan is
incomplete and is never applied, even when it is loaded again from the
database. A plan also cannot be applied once another revision went live or
the draft changed after it was computed (409 `conflict`: plan again).

`uc-hub plan -c hub.yaml` prints the same plan on the command line (for a
stopped hub).

```jsonc
// Plan (ids are p<revision>, then p<revision>-2, ... when a revision is planned again)
{"id": "p17", "revision": 17, "base_revision": 16, "created_at": 1790290000.0,
 "targets": ["r204-ctl", "gateway"], "warnings": ["app thermostat requests permission io"],
 "changes": [Change], "blocked": {}}
// Change
{"id": "c1", "target": "r204-ctl", "kind": "upload-doc", "summary": "io.json: +1 point",
 "diff": "--- a/io.json\n+++ b/io.json\n@@ ...", "tier": "C"}
// TestResult
{"name": "valve opens when cold", "status": "pass", "duration_ms": 4210,
 "failed_step": null, "detail": "", "target": "live"}
```

## Agent runs

A run is one conversation with the agent. It lives on the gateway and
continues when the browser disconnects.

| Method and path | Response |
|---|---|
| `GET /api/runs?limit=20` | `{"runs": [RunSummary]}` (newest first, `limit` 1..200) |
| `POST /api/runs` `{"message": str, "playbook"?: str}` | `RunSummary` (status 201). The run starts at once. `playbook` is one of `commission`, `onboard`, `io-checkout`, `troubleshoot`, `handover` (400 `invalid` otherwise); its instructions join the run's system prompt. The title is the playbook's name, else the first sentence of the message. Any role may start a run; a viewer's run only gets the read tools. |
| `GET /api/runs/{id}` | `RunSummary` |
| `POST /api/runs/{id}/messages` `{"message": str}` | `{}` (202): a new turn, run as the sender (their roles decide what the tools may do). 409 while the run is `running`, `waiting_approval` or `waiting_answer`, and for MCP sessions. |
| `POST /api/runs/{id}/cancel` | `{}`: stops the turn; the run's pending approvals and open questions expire, calls that did not run get a failed result, and the agent's leases the run holds are released. The run's creator or an operator (or above). Cancelling a run between turns does nothing; a cancelled MCP session takes no more calls. |
| `POST /api/runs/{id}/answer` `{"question_id": str, "answer": str}` | `{}`. The run's creator or an operator (or above). With `options` the answer must be one of them (400 `invalid`); a question is answered once, and not after it expired (409); a question of another run is not found (404). |
| `GET /api/runs/{id}/events?after=<seq>` (SSE) | Replays all events with `seq > after`, then streams live ones. A `Last-Event-ID` header (a reconnecting `EventSource`) wins over `after`; a resume point larger than the run's `last_seq` is taken as `last_seq`, so only new events follow. 400 for a resume point that is not a whole number ≥ 0. Keep-alive comments every 15 s. |

```jsonc
// RunSummary
{"id": "r_8f2c", "title": "Commission room 204", "state": "waiting_approval",
 "created_at": 1790290000.0, "updated_at": 1790290100.0, "created_by": "dev", "last_seq": 42,
 "model": "glm-5.3"}
```

`state` is one of `running`, `waiting_approval`, `waiting_answer`, `idle`
(finished its turn, can take a new message), `failed` (an internal error;
it can take a new message too) or `cancelled`. `last_seq` is the `seq` of
the run's last event.

A turn ends with `idle` and an `error` event when the model provider fails
(`llm`: send a message to retry), stops its answer (`sensitive`, `length`),
the turn's budget is used up (`budget`: `agent.max_tool_calls` tool calls,
or `agent.max_wall_s` of work, not counting the time spent waiting for
people), or the same tool call fails twice in a row with the same arguments
(`repeated_failure`). Tool calls the model asked for but that did not run
get a failed `tool.result`. At a hub restart, runs that wait for an
approval or an answer keep waiting; runs that were `running` become `idle`
with an `error` event `interrupted by a hub restart` (`restart`).

Clients of `uc-hub mcp` get a run of their own ("MCP session", `model`
`mcp`), where their calls appear as `tool.call`, `tool.result` and
approval events.

### Run events

SSE framing: `id: <seq>`, `event: <type>`, and `data: <json>`. Each `data`
object has `{"seq": int, "run_id": str, "ts": float, "type": str}` plus the
fields listed here:

| type | fields |
|---|---|
| `run.state` | `state`, `reason?` |
| `message.user` | `text`, `user` |
| `thinking.delta` | `text` |
| `message.delta` | `text` |
| `message.done` | `text` (the complete assistant message) |
| `tool.call` | `call_id`, `tool`, `tier`, `args`: sent with `args` `{}` as soon as the model starts the call, and again with the complete arguments once they are known (`{}` when they are not a JSON object) |
| `tool.args.delta` | `call_id`, `delta` (raw JSON fragment while the model streams arguments) |
| `tool.result` | `call_id`, `ok`, `summary`, `duration_ms`, `data?` (only results up to `agent.result_inline_limit` bytes), `handle?` (`result://rN` of a larger result, which the model pages with `result_get`) |
| `approval.request` | `approval` (Approval) |
| `approval.decided` | `approval` (Approval) |
| `question` | `question_id`, `call_id`, `text`, `options` (list of str; free text allowed when empty) |
| `question.answered` | `question_id`, `answer`, `user` |
| `plan.updated` | `plan_id \| null`, `revision \| null`, `changes` (count); null after an edit made the plan stale and after a successful apply |
| `tests.updated` | `results` (list of TestResult: every stored live result, after a test run) |
| `error` | `message`, `code?` (`llm`, `sensitive`, `length`, `budget`, `repeated_failure`, `restart`, `internal`, ...) |

Clients rebuild a run's view by folding events in `seq` order. The same
reducer handles the replay and the live stream. Streamed text arrives in
pieces of about 0.1 s. Tool call ids are unique within a run, not across
runs. The `data` of a tool result may contain text that devices reported
(under `device_data`); show it as text.

## Approvals

| Method and path | Response |
|---|---|
| `GET /api/approvals?state=pending` | `{"approvals": [Approval]}` (newest first; `state` optional) |
| `POST /api/approvals/{id}` `{"decision": "approve" \| "reject", "comment"?: str, "scope"?: "call" \| "run"}` | `Approval` (409 if already decided or expired, 403 if the role may not approve this call) |

```jsonc
// Approval
{"id": "a_12", "run_id": "r_8f2c", "call_id": "call_3", "tool": "apply", "tier": "C",
 "title": "Apply plan p17", "summary": ["r204-ctl: io.json +1 point, 2 apps", "gateway: 1 bridge"],
 "diff": "...", "rollback": "Apply revision 16", "plan_id": "p17",
 "state": "pending", "scope": "call", "requested_at": 1790290100.0, "expires_at": 1790291900.0,
 "requested_by": "dev", "decided_by": null, "decided_at": null, "comment": null}
```

Each `summary` line has the form `"<target>: <detail>"` (a device name,
`gateway` or `site.yaml`), followed by the plan's warnings. `decided_by`,
`decided_at` and `comment` are null until the approval is decided or
expires (an expiry sets `decided_at` and says why in `comment`).

Approval rules (enforced by the hub):

- Tier `L` needs role `operator`, `commissioner` or `admin`. Tier `C` needs
  `commissioner` or `admin`, and only an `admin` may approve a plan with
  more targets than the site policy's `max_devices_per_stage`, or a plan
  that adds or removes a life-safety mark.
- The hub checks the policy before it asks: a call the policy refuses
  anyway (a life-safety point, a deny pattern, a stale plan) fails at once
  and never becomes an approval. An approve checks the call again with the
  approver's roles (the plan may have gone stale, the policy may have
  changed): when the hub would refuse it now, the request fails with that
  error (e.g. 409 `conflict` for a stale plan) and the approval stays
  pending, to be rejected. A reject needs the requester or someone who may
  approve the tier.
- `scope: "run"` (tier L only; 400 `invalid` for tier C) also approves the
  run's later tier L calls: they run under the approver's roles without a
  new approval, and each is audited as covered by this approval. Tier C
  calls always need their own approval. The approval shows `scope: "run"`.
- `state` is one of `pending`, `approved`, `rejected` or `expired`.
  Approvals expire after `policy.approval_ttl_s` (30 minutes by default);
  the hub checks every few seconds. The run then gets `approval.decided`
  and a tool result saying so. Approvals of a cancelled run expire at once.
  An MCP client waits at most `mcp.approval_wait_s` for a decision; the
  approval expires when it stops waiting.
- Clients must require a deliberate gesture for tier `C`: a hold on phones,
  or a button inside the expanded card on desktop.

## Audit

`GET /api/audit?limit=100` returns `{"entries": [AuditEntry]}` (newest
first, `limit` 1..1000). It is available to the `commissioner` and `admin`
roles.

```jsonc
// AuditEntry
{"ts": 1790290100.0, "user": "dev", "run_id": "r_8f2c",   // run_id null outside a run
 "action": "tool",                                       // tool, approval, run.cancel, lease.*, ...
 "tool": "point_write", "tier": "L",                     // null when the action has no tool
 "args": {"point": "hq/r204-ctl/analog-value:1", "value": 23},   // any JSON, null when there are none
 "outcome": "ok", "detail": "wrote 23 ... (approved by ilija)"}
```

## The web app

When `web_dir` is set, the hub serves the built app at `/`: a path that
names a file below `web_dir` serves it (`/assets/` with a long cache
lifetime, everything else revalidated), any other path without a file
extension serves `index.html`, and everything else is 404. Nothing outside
`web_dir` is ever served.
