# C3 bounded local validation — 2026-10-03

Local implementation parent: 6be6e84d11f3942061aa6061ea9f431155b5d29b.
Worktree D:/Codex/projects/Ombre-Brain-C3, branch codex/lsf-c3-loopback.
Scope is deploy/lsf-zeabur only. No push, deployment, production access, shared dependency
installation, persistent environment modification, or business/C2 contract modification.

## Environment

Read-only original runtime_sources() accepted Python3.12.14, MCP1.29.1, RM0.1.0 from
/mnt/d/Codex/projects/Ombre-Brain-S5-Remaining/.s5-rm010-official. Official release URL:
https://github.com/peanutsuee/Remember-Me/releases/download/v0.1.0/remember_me-0.1.0.tar.gz
SHA256 93d1514f940bde00a43b34b61681fe7f64da130313840f247869157d6e250485.
Original metadata/direct_url/contract checks are unchanged. Every worker independently
reports its RM module source and version; parent assertions require the official path.
Command-level PYTHONPATH is preserved for children after isolation, never persisted.
Formal evidence: evidence/c3-formal-rm-preflight.json.

Command in the C3 WSL checkout root:

```sh
PYTHONPATH=/mnt/d/Codex/projects/Ombre-Brain-S5-Remaining/.s5-rm010-official \
/home/ting/.venvs/ombre/bin/python -B -m pytest \
  deploy/lsf-zeabur/test_c3.py -q -x -p no:cacheprovider \
  --junitxml=deploy/lsf-zeabur/evidence/c3-junit-formal-rm.xml
```

Only this bounded module ran; no full suite. Linux default /tmp is asserted, with no
TMP/TEMP or basetemp. C3 preparation also resets original configure()'s TMPDIR/tempfile
selection; original environment.py itself is unchanged.

## Evidence and outcomes

Original default-venv attempt:1 failed at the untouched RM==0.1.0 identity gate; venv has
RM0.1.0.dev7. Original c3-bounded-output.txt and c3-junit.xml are retained byte-for-byte.
No service sockets opened in that attempt. No failed root was intentionally cleaned.

Formal attempt1:1 failed,33.63s. Actual C2 preparation, identity refusal, partial refusal,
held-lock refusal and C2 preservation assertions completed; exercise then failed because
C3 harness read result rather than result_text from the durable operation row.
Negative probes used independent copied synthetic roots, preserving the original marker
and each refusal site. Those completed checks were not rerun.

Formal attempt2:1 failed,16.68s. A C3 harness field replacement mistakenly affected the
MCP JSON-RPC result field; no five-scenario injection was reached in this attempt.
The normal hold durable key remained in its synthetic root and was subsequently replayed.

Formal attempt3:1 failed,12.79s. All five scenario business/runtime assertions completed,
including real socket disconnect, then worker JSON emission failed on a deque in the return
value. The failure is in the encoder after exercise() has returned and after its finally
has shut down listeners. The final C3-only correction converts that deque to a list.
No completed fault scenario was reinjected afterward.

Final bounded receipt recovery:1 passed,8.47s. Same runtime root, same five keys, real MCP
HTTP replay only, no fault arm or injection. Each original completed receipt matched, fault
provider counts={} and original stub counts={} throughout. Current evidence:
c3-junit-formal-rm.xml, c3-formal-rm-output.txt, c3-formal-safe-evidence.json.
Earlier formal outputs/JUnit remain in *attempt1*, *attempt2*, *attempt3* files.

The continuation ledger activates only while its original /tmp root exists; otherwise
the bounded module uses a fresh synthetic root and its full checks. It does not clean,
repair or mutate an existing failed root. Do not rerun this completed local batch.

## Five-scenario assertions completed in attempt3

| Scenario | Runtime assertion and durable recovery |
|---|---|
| grow429 | Real OpenAI SDK, loopback HTTP429 for all3 SDK attempts; completed reason=rate_limited; no new Markdown business bucket; identical-key replay with no provider increment |
| grow invalid completion | Real SDK HTTP200 invalid message.content;1 attempt, completed reason=parse_error; no business bucket; identical replay/count invariance |
| grow connection | Raw TCP closure sends no HTTP response bytes across3 SDK attempts; completed reason=connection_error; no business bucket; identical replay/count invariance |
| hold fallback | Real analyze parser fails; explicit default-metadata/parse_error receipt; exactly one bucket keeps full original body and domain=[未分类]; replay/count invariance |
| keyed hold disconnect | Actual asyncio TCP MCP client closes after waiting; exact keyed ASGI http.disconnect observed before gate release; original durable request completes; reconnect/replay returns its original receipt without provider increment |

All scenario calls used fixed content hash/path/prompt type and original opaque key.
Real SDK, loopback socket, stateful authenticated MCP and business SQLite/Markdown/vector
storage were used. No business parser/retrieval/store mock. Fallback/disconnect permit
synthetic buckets and normal related effects; C1 all-file zero-change is not asserted.

Important evidence limit: attempt3's in-memory provider/event rows were not archived
because of the encoder failure. Its final assertion requires waiting/http.disconnect/
released/completed and excludes gate_timeout_unaccepted, but timestamped original event
rows cannot be inspected in the artifact. The final recovery does not recreate those
rows or claim a second disconnect test. Treat the standalone per-event transport evidence
artifact as incomplete/unaccepted, even though the original local runtime assertions
finished. No missing event was replaced by500, stop-output or page refresh.

## Preservation and secrecy

The original complete C2 validator and before/after snapshots compare marker SHA256
(equivalent bytes), all21 file hashes, fixed history/letter SQL rows and20 vector rows.
No C2 marker/fixture/source/contract is edited. Hashes of protected run/environment/
launcher/provider_stub/C2 files match the exact Git baseline after LF normalization.
No full C1 snapshot invariant is promised. Failed synthetic roots are retained in /tmp;
WSL temporary-storage lifetime is external to this checkout.

Safe logging checks the query token and synthetic API credential sentinel without keeping
raw logs. Control projections exclude content/credentials and expose only known-key receipt
status/result hashes. Origin requests are rejected. Final artifacts are scanned for the
query/API sentinel and credential-header patterns; no raw credential is archived.

## Remaining unaccepted checks

- Original timestamped disconnect/provider-attempt event rows: incomplete artifact as above.
- Real Claude connector transport close/recovery and all five remote acceptance batches.
- Docker image build/runtime UID10001, mounted-volume permissions and Zeabur update/lock timing.
- Manual control watch-disconnect CLI: syntax/static review only; runtime deadline and ASGI
  gate paths are exercised by the local module, but the CLI itself was not a timed live batch.
- SIGKILL/process crash matrix and real third-party provider quality/cost/timeouts.

No automatic experiment rerun, failure-site cleanup, identity bypass or lock bypass.
Deployment/copy steps: C3_IMPLEMENTATION.md. All timed manual steps: C3_CLAUDE_INSTRUCTIONS.md.
After static range/hash/credential checks and listener closure, make one local commit and stop.

Listener closure: 18993/18994/18995 all returned ECONNREFUSED (111); see c3-listener-closure.json.
The original first pytest command created ignored .pytest_cache in this independent worktree; it is retained, not committed. Formal commands disable cacheprovider.
WSL static audit uses command-only GIT_DIR/GIT_WORK_TREE to read the Windows-created worktree; no Git pointer or config is rewritten.

Source/doc diff whitespace checks pass; failure-output/JUnit whitespace is intentionally preserved rather than rewritten.

## New local disconnect evidence addendum — 2026-10-03

This is a new experiment authorized after parent commit
508f01fc7f996c9e5477f141ae26eaab53fba1de. It does not rebuild, replace or claim to recover
attempt3's missing original event rows. Every old output/JUnit/evidence file and the original
commit remain unchanged. The historical artifact gap above still applies to that old run;
this addendum supplies independently observed evidence for a new local disconnect case.

Only test_c3_disconnect_evidence.py ran: **1 passed in17.45s**, once. No other fault scenario
or old batch ran. New C2 synthetic preparation reused the original initializer/provider;
its distinct root is /tmp/c3-disconnect-evidence-w26gxqxi/fresh-c3-disconnect. The test uses
Linux mkdtemp directly rather than pytest numbered temporary directories, avoiding rotation
or cleanup of earlier failed roots. No root was removed or repaired. Command-level official
RM PYTHONPATH and the original runtime identity/contract checks passed again.

```sh
PYTHONPATH=/mnt/d/Codex/projects/Ombre-Brain-S5-Remaining/.s5-rm010-official \
/home/ting/.venvs/ombre/bin/python -B -m pytest \
  deploy/lsf-zeabur/test_c3_disconnect_evidence.py -q -x -p no:cacheprovider \
  --junitxml=deploy/lsf-zeabur/evidence/c3-disconnect-new-junit.xml
```

The existing fixed disconnect key obweb-ls-c3-v1-disconnect and its exact content hash were
confirmed absent from this fresh root before arm. The existing shipped watch-disconnect CLI
was launched as a real child process with the official command-level RM path inherited.
The CLI, not the test controller, released the gate after observing the exact request's
http.disconnect. The MCP client was an actual loopback TCP connection: it sent an authenticated
stateful tools/call request, observed waiting via status, closed its writer and socket, and
then let the CLI observe/release/wait for completion. No500, model stop or page refresh.

New safe JSON: evidence/c3-disconnect-new-evidence.json. It is parsed back after every write.
It contains actual live monotonic timestamps, observer sequence, generation, actual CLI
stdout/exit code, count snapshots, receipt/body/single-effect checks, C2 snapshots and listener
closure. It also keeps UTC collection/close/start/end timestamps. Event monotonic times
retain the existing observer's rounding; sequence records actual append order even when
rounded timestamps coincide. No old event is reconstructed. Runtime event storage is deque;
list conversion and JSON roundtrip are explicitly asserted, and the entire artifact parses.

Generation1 observed exactly one of each required event in this order:
waiting -> http.disconnect -> released -> completed. The CLI returned accepted=true with
exit code0, before the45-second limit; no gate timeout event occurred. The JSON includes
armed/provider_attempt as preceding observed events. Fault provider count is disconnect=1.
Original stub counts after completion are embedding=4 and analyze=1; these are ordinary
business provider calls, not repeated disconnect injections.

Readback preserves the complete original body in exactly one new bucket and one frozen
plan item, with a stored vector. A fresh real HTTP MCP client precisely replays the same
key/content. Its receipt equals the original durable result; bucket IDs/effects are unchanged.
Fault provider and stub counts after replay equal their before-replay values.

The original full C2 complete validator passed before listeners opened. Before/after actual
marker bytes, all21 fixture hashes,20 fixed vector rows and manifest-designated history/
letter SQL rows are identical. Credential sentinel checks pass, no raw auth/session values
are archived. 18993/18994/18995 all return ECONNREFUSED(111) after service/CLI shutdown.

The new test refuses execution if this evidence artifact already exists; do not rerun it.
The new evidence is independent of the old missing rows. The local CLI/disconnect and
serialization evidence gaps are now covered by this new case. Real Claude connector,
Docker/container/volume and Zeabur acceptance remain unexecuted. No push/deployment or
formal OB/main/origin/旧日雪 change occurred.
