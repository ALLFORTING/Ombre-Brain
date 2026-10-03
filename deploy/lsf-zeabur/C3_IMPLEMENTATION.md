# C3 implementation and delivery

Implementation parent: 6be6e84d11f3942061aa6061ea9f431155b5d29b.
Branch: codex/lsf-c3-loopback. Local implementation only; no push/deployment.

C3 adds c3_run.py and c3_provider.py, leaving run.py, environment.py, launcher.py,
provider_stub.py, C2 identity code, c2-source-hashes.json, original markers and business
contracts unchanged. Docker CMD selects c3_run.py. Existing volume/token/variables stay
unchanged; no additional required environment variable.

Startup reuses configure, lock_volume, runtime_sources, verify_c2_sources, initialize,
initialize_c2 and launcher.build/start/stop. Only an existing complete C2 volume is accepted.
The original seed identity and complete C2 identity/21 files/history/letters/20 vectors are
validated under the original service lock before provider/control/public MCP sockets open.
C3 does not seed or heal missing/partial C2. C3 artifacts have a separate LF-normalized
manifest. Dependency pins and the original runtime contract checks remain unchanged.

C3 isolation overrides only the inherited temporary-directory selection to Linux /tmp,
without TMP/TEMP or basetemp. Command-level PYTHONPATH survives configure_c3 for children;
it is never persisted. Docker continues using its own verified installed official RM.

Provider=127.0.0.1:18995, control=127.0.0.1:18994; only MCP/health use the existing public
platform port. Control is disarmed on each startup. It rejects browser Origin requests and
projects only scenario/generation/status/counts/event metadata/result hashes. It never
projects request bodies, auth headers, query tokens, provider credentials, or arbitrary
operation receipts. The fixed C3 bodies/keys are synthetic and public.

Fault selection requires the exact UTF-8 content SHA256, /v1/chat/completions and digest
or analyze prompt type. Only one scenario is armed. Arm does not consume one request:
all SDK attempts retain the fault until explicit disarm. Other paths/content/prompts pass
to the unchanged provider stub. HTTP429 uses Retry-After:0. Invalid completion is HTTP200
with invalid JSON in message.content. Connection failure writes zero HTTP response bytes
and closes the socket, preserving genuine SDK connection_error classification.

Disconnect transport observation is bound to the exact MCP tools/call key/tool/content
and its arm generation; ordinary session closes and completed HTTP responses do not count.
One gate deadline starts at first waiting and lasts at most45 seconds across provider retries.
Release requires waiting plus http.disconnect in the same generation before expiry.
Deadline expiry finishes ordinary provider passthrough and marks gate_timeout_unaccepted;
it never creates a new waiting window and never counts as acceptance. No scenario reruns
are automatic. On shutdown public/control listeners close first; keyed runners and legacy
post effects drain while provider and service lock remain held. Timeout cancels and joins
runners, preserves incomplete receipts and reports shutdown failure. Provider closes and
service lock releases last.

## Copy/export commands for later review

These commands only export the local commit to a new clean review checkout; they do not
push or deploy. Replace C3_SHA with the SHA delivered in chat. Do not use a dirty checkout
as the copy destination and do not overwrite old run/environment/launcher/provider files.

```powershell
git -C D:\Codex\projects\Ombre-Brain-C3 worktree add --detach D:\Codex\projects\OB-LSF-C3-DELIVERY C3_SHA
```

The complete baseline plus C3 is available there. For exact synthetic tool arguments,
run the following inside that checkout or, after separately authorized deployment,
inside the test container (never a production container):

```sh
python -B /app/deploy/lsf-zeabur/c3_control.py arguments rate
python -B /app/deploy/lsf-zeabur/c3_control.py arguments parse
python -B /app/deploy/lsf-zeabur/c3_control.py arguments connection
python -B /app/deploy/lsf-zeabur/c3_control.py arguments fallback
python -B /app/deploy/lsf-zeabur/c3_control.py arguments disconnect
```

## Later deployment steps — not executed in this Phase

1. Review this local commit and C3_VALIDATION.md; obtain separate push/deployment authorization.
2. Update only the existing L-SF test service, using the existing Dockerfile location,
   /data/lsf volume, query token and opt-in variables. Build context must be this exact
   clean commit; c3_audit.py verifies the manifest and Docker whitelist first.
3. Stop the old test process so its service.lock is released before starting C3. Overlap
   fails closed; do not bypass the lock. A missing/partial/changed C2 volume stays blocked.
4. Check existing /health and container-local c3_control.py status. Confirm disarmed and
   C2 complete validation; reconnect only the original L-SF test connector.
5. Give all manual steps from C3_CLAUDE_INSTRUCTIONS.md before beginning a timed window.
   Run one batch at a time; preserve actual returned receipts and control projections.
6. Any missing waiting/http.disconnect/completed event is unaccepted. Preserve the volume
   and reports; do not rearm/retry to substitute evidence. No automatic rollback or cleanup.

Docker build, container UID/mount permissions, Zeabur update and real Claude connector
acceptance remain unexecuted. Formal OB/main/旧日雪 are outside this delivery.


## C3 observation correction at parent 499fb66 (local only)

Each matching MCP HTTP request receives a UUID, including same-key replay. The armed
first request owns the waiting gate. Concurrent matching retries poison acceptance rather
than contributing their disconnect to another request. Events retain each request identity.
The local mark-operation command requires the exact target request ID after waiting.
The only accepted order is waiting -> operation_marked -> target http.disconnect -> release.
Release rechecks the wait/deadline, durable registration and non-completed receipt. Rejected
or incomplete observations never cause CLI release or automatic rerun. Normal timeout
passthrough retains its existing business behavior and remains unaccepted.
completed/time_basis=observed_at means completion was observed in a durable receipt;
it does not claim the time of the actual commit. No business/C2 identity changes are needed.
