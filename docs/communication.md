# How the components communicate

## Inference: applications ↔ GPUqueue ↔ Ollama

1. An integrated application sends a supported inference POST to loopback port
   11436. OpenAI-style clients use `/v1`; Ollama clients use `/api`.
2. The broker validates the request and stores a job plus its encrypted body in
   SQLite. A supplied idempotency key identifies the logical request.
3. The scheduler orders queued jobs by aged priority, then insertion order. It
   checks lane capacity, model ownership, declared budgets and fresh GPU telemetry.
4. When admitted, the broker forwards the request to Ollama on port 11434. This is
   the backend hop; apps should not send their inference around the queue.
5. Ollama returns response chunks. The broker encrypts and stores them, and the
   result handler streams saved chunks back to the waiting application. The CPU
   OpenAI embedding adapter collects and converts its response before storing it.
6. The broker releases **that request's model** and verifies that it is absent
   from Ollama's loaded-model list. It never globally unloads unrelated models.
7. Confirmed cleanup permits a terminal state. An exception whose cleanup is
   uncertain leaves `recovery_blocked`, retaining the lane reservation. The
   client may already have received an HTTP 200 response at this point.

The default HTTP request stays connected while waiting. `Prefer: respond-async`
instead returns HTTP 202 and a durable job ID immediately after acceptance.
Authenticated `GET /jobs/ID` and `GET /jobs/ID/result` retrieve status and retained
response bytes. Reading a saved result does not rerun inference. Client retry
coalescing is not a universal exactly-once guarantee across arbitrary retries.

Metadata GETs `/api/ps`, `/api/tags`, `/api/version` and `/v1/models` are forwarded
without reserving a GPU inference slot. Supported inference routes are listed in
`coordinator/server.py:ROUTES`; unlisted API operations are not transparent proxies.
The legacy `/v1/systemone` route is simply forwarded for compatible custom
backends; stock Ollama support is not implied.

## Managed commands: local client ↔ coordinator; local client → child

The command path does not pass model tensors or CUDA calls through HTTP.

1. `client.py run` registers its PID, birth time, command specification, owner,
   model label, priority and memory requirements with `POST /jobs/command`.
2. The client waits and sends heartbeats. The coordinator grants admission.
3. The client creates a suspended child, puts it in its own Windows Job Object,
   registers the child PID with the coordinator, and resumes it.
4. The child imports its model and uses CUDA directly under the granted lease.
   An optional entry-point guard verifies its exact registered identity.
5. Cancellation terminates only the client's owned process tree. Before release,
   the client confirms the tree is empty and records the exit status.
6. A lost heartbeat alone never releases a still-live process. Uncertain tree
   cleanup blocks recovery instead of granting conflicting work.

The HTTP server does not run arbitrary submitted command lines. The process-owning
local client does. Only explicitly repeatable command jobs with a declared OOM
exit code can use the bounded, one-time exclusive retry path.

## Desktop, browser and supervision

The tray reads `/health`, `/status`, `/api/version`, and `watchdog.json`; CPU usage
comes from Windows system-time deltas. GPU metrics come from the coordinator's
`nvidia-smi` probe. They are **whole-device readings**, not per-job measurements.
The tray's traffic light and primary counters describe GPU jobs; CPU jobs have
their own tab. Service failures and global pause remain visible.

The web dashboard polls status/history and exposes priority/cancel controls only
in a scoped operator session obtained through `client.py open --control`. The tray
opens the ordinary read-only web URL. Its only configuration write is toggling
the existing verified tray autostart task.

The watchdog checks the coordinator's HTTP identity and process entry point. It
restarts a missing broker with bounded backoff when the port is free. It does not
kill live unhealthy services, take over unknown listeners, restart Ollama, clear
blocked jobs or replay inference. Task Scheduler starts the watchdog after login
and can restart the watchdog itself. Closing the tray window hides it; exiting
the tray does not stop the coordinator.

## Source map

| Concern | Source |
|---|---|
| HTTP routes, backend forwarding, cleanup | `coordinator/server.py` |
| Admission policy and priority aging | `coordinator/scheduling.py` |
| SQLite, encryption, lifecycle and recovery | `coordinator/store.py` |
| Process ownership and local command execution | `coordinator/client.py` |
| Entry-point lease validation | `coordinator/guard.py` |
| Watchdog identity checks and restart decisions | `supervisor.py` |
| Desktop polling, tabs and traffic light | `desktop/GPUqueue.cs` |
| CPU sampling and tray autostart setting | `desktop/DesktopServices.cs` |

The Archify diagram is a component map. Its arrows summarize relationships, not
every HTTP response direction; this walkthrough defines the request/return order.
