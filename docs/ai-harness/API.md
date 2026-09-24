# uc-hub HTTP API (v1)

Contract between the browser app (`web/`) and the gateway (`hub/`). All
paths are under `/api`. Bodies are JSON (UTF-8). Times are Unix seconds
(float). Point IDs are `<site>/<device>/<object>` (see
`hub/src/uc_hub/core/ids.py`).

The JSON shapes below match the `to_json()` methods in
`hub/src/uc_hub/core/types.py`. The TypeScript mirror is
`web/src/api/types.ts`.

## Authentication

- Every request carries `Authorization: Bearer <token>`. SSE requests cannot
  set headers from `EventSource`, so they may pass `?access_token=<token>`
  instead.
- Tokens and their users and roles are configured in `hub.yaml`
  (`auth.tokens`). When no tokens are configured, the hub runs in **dev
  mode**: every request is user `dev` with all roles. The hub then only
  listens on 127.0.0.1 unless `--insecure-listen` is given.
- Roles: `viewer`, `operator`, `commissioner`, `admin`.
- Errors: `{"error": {"code": str, "message": str, "details"?: any}}` with
  status 400 (`invalid`, `validation`), 401, 403 (`denied`), 404
  (`not_found`), 409 (`conflict`), 501 (`unsupported`), 502 (`device_error`),
  504 (`timeout`).

## Site and points

| Method and path | Response |
|---|---|
| `GET /api/health` | `{"ok": true, "version": "0.1.0", "site": "hq", "llm": {"provider": "zai", "model": "glm-5.3", "configured": bool}, "dev_mode": bool}` |
| `GET /api/me` | `{"user": "dev", "roles": ["admin", ...]}` |
| `GET /api/site` | `Site` |
| `GET /api/devices/{name}` | `DeviceDescription` |
| `GET /api/points?q=&device=&space=&tag=&limit=200&offset=0` | `{"total": int, "points": [Point & {"reading"?: Reading}]}` |
| `POST /api/points/read` `{"ids": [str]}` | `{"readings": [Reading]}` |
| `GET /api/live?ids=a,b` or `?device=r204-ctl` (SSE) | Events `reading` with a `Reading` payload. The first events are the current cached values. Keep-alive comments every 15 s. |
| `POST /api/discover` `{"protocol"?: "bacnet-uc"\|"bacnet-ip"\|"mqtt", "timeout_s"?: float}` | `{"devices": [DiscoveredDevice & {"known": bool}]}` |
| `POST /api/devices/{name}/identify` `{"seconds"?: int}` | `{}` (tier L, operator or above) |

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
`life-safety`.

## Manifest, plan, apply, tests

| Method and path | Response |
|---|---|
| `GET /api/manifest` | `{"live_revision": int, "draft_revision": int \| null, "yaml": str, "draft_yaml": str \| null}` |
| `GET /api/manifest/revisions` | `{"revisions": [{"revision", "created_at", "author", "message", "live": bool}]}` |
| `GET /api/plan` | `{"plan": Plan \| null}`, the plan of the current draft |
| `GET /api/tests` | `{"results": [TestResult], "updated_at": float \| null}` |

Applying a plan is only possible through an approval (see below). There is
no direct "apply" endpoint.

```jsonc
// Plan
{"id": "p17", "revision": 17, "base_revision": 16, "created_at": 1790290000.0,
 "targets": ["r204-ctl", "gateway"], "warnings": ["app thermostat requests permission io"],
 "changes": [Change]}
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
| `GET /api/runs?limit=20` | `{"runs": [RunSummary]}` (newest first) |
| `POST /api/runs` `{"message": str, "playbook"?: str}` | `RunSummary` (status 201). The run starts at once. |
| `GET /api/runs/{id}` | `RunSummary` |
| `POST /api/runs/{id}/messages` `{"message": str}` | `{}` (202). Rejected with 409 while the run is `running`. |
| `POST /api/runs/{id}/cancel` | `{}` |
| `POST /api/runs/{id}/answer` `{"question_id": str, "answer": str}` | `{}` |
| `GET /api/runs/{id}/events?after=<seq>` (SSE) | Replays all events with `seq > after` (or `> Last-Event-ID`), then streams live ones |

```jsonc
// RunSummary
{"id": "r_8f2c", "title": "Commission room 204", "state": "waiting_approval",
 "created_at": 1790290000.0, "updated_at": 1790290100.0, "created_by": "dev", "last_seq": 42,
 "model": "glm-5.3"}
```

`state` is one of `running`, `waiting_approval`, `waiting_answer`, `idle`
(finished its turn, can take a new message), `failed` or `cancelled`.

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
| `tool.call` | `call_id`, `tool`, `tier`, `args` (object; `{}` until complete) |
| `tool.args.delta` | `call_id`, `delta` (raw JSON fragment while the model streams arguments) |
| `tool.result` | `call_id`, `ok`, `summary`, `duration_ms`, `data?` (small results only), `handle?` |
| `approval.request` | `approval` (Approval) |
| `approval.decided` | `approval` (Approval) |
| `question` | `question_id`, `call_id`, `text`, `options` (list of str; free text allowed when empty) |
| `question.answered` | `question_id`, `answer`, `user` |
| `plan.updated` | `plan_id \| null`, `revision \| null`, `changes` (count) |
| `tests.updated` | `results` (list of TestResult) |
| `error` | `message`, `code?` |

Clients rebuild a run's view by folding events in `seq` order. The same
reducer handles the replay and the live stream.

## Approvals

| Method and path | Response |
|---|---|
| `GET /api/approvals?state=pending` | `{"approvals": [Approval]}` |
| `POST /api/approvals/{id}` `{"decision": "approve" \| "reject", "comment"?: str}` | `Approval` (409 if already decided or expired, 403 if the role may not approve this tier) |

```jsonc
// Approval
{"id": "a_12", "run_id": "r_8f2c", "call_id": "call_3", "tool": "apply", "tier": "C",
 "title": "Apply plan p17", "summary": ["r204-ctl: io.json +1 point, 2 apps", "gateway: 1 bridge"],
 "diff": "...", "rollback": "Apply revision 16", "plan_id": "p17",
 "state": "pending", "requested_at": 1790290100.0, "expires_at": 1790291900.0,
 "requested_by": "dev", "decided_by": null, "decided_at": null, "comment": null}
```

Approval rules (enforced by the hub):

- Tier `L` needs role `operator`, `commissioner` or `admin`. Tier `C` needs
  `commissioner` or `admin`.
- `state` is one of `pending`, `approved`, `rejected` or `expired`.
  Approvals expire after 30 minutes by default. The run then gets a tool
  result saying so.
- Clients must require a deliberate gesture for tier `C`: a hold on phones,
  or a button inside the expanded card on desktop.

## Audit

`GET /api/audit?limit=100` returns `{"entries": [{"ts", "user", "run_id",
"action", "tool", "tier", "args", "outcome", "detail"}]}`. It is available
to the `admin` and `commissioner` roles.
