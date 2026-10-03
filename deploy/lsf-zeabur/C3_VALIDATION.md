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
