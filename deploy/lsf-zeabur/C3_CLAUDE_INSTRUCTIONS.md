# C3 Claude batch instructions

Only the existing L-SF synthetic test connector. No other OB/旧日雪 connector, no C2 fixture
mutation, no marker edit, no delete/repair/cleanup. All instructions below must be provided
before starting a timed window. Do not wait for chat relays during the45-second window.
Fixed exact arguments and keys are in c3-fixed-inputs.json; do not shorten, translate,
normalize punctuation, regenerate keys or append extra content.

Every batch: administrator captures status before/after; Claude calls the indicated tool
once, records actual parameters/result, then replays exactly the same key/content once.
The durable receipt must match and the provider counts must not increase on replay.
After completed, administrator disarms and stops that batch for review. Do not arm a second
scenario concurrently. Existing completed keys cannot be used to prove a fresh injection.

Commands below run in the L-SF test container terminal, never in a production terminal.
No control URL is public, no query token is passed to these commands.

## Batches1–4

For each scenario name below, replace NAME in these commands, run arguments and give
its complete output to Claude before arm. Then arm immediately before the tool call.

```sh
python -B /app/deploy/lsf-zeabur/c3_control.py arguments NAME
python -B /app/deploy/lsf-zeabur/c3_control.py status
python -B /app/deploy/lsf-zeabur/c3_control.py arm NAME
```

Claude instruction: Use the displayed exact tool and arguments on the L-SF connector.
Do not change operation_id. Record the original result, repeat the identical call once,
compare the two receipts, then stop. Do not add a repair, delete or alternative call.

1. NAME=rate: grow digest, expected completed failure receipt reason=rate_limited.
2. NAME=parse: grow digest, expected completed failure receipt reason=parse_error.
3. NAME=connection: grow digest, expected completed failure reason=connection_error.
   Administrator must also observe tcp_closed_without_http for each SDK attempt.
   Ordinary HTTP500, stopped generation or page refresh cannot substitute this evidence.
4. NAME=fallback: hold analyze parsing fails, receipt explicitly says parse_error and
   default metadata. Original full body must remain in exactly one synthetic bucket.
   Normal automatic related effects are allowed; old C1 all-file zero-change is not required.

For1–3 no business bucket may be created. SDK429/connection retry attempts must all be
injected while the same scenario stays armed (local SDK observed3 attempts each).
Parsing failure uses one completion. Save status after first call before replay, then:

```sh
python -B /app/deploy/lsf-zeabur/c3_control.py status
python -B /app/deploy/lsf-zeabur/c3_control.py disarm
```

## Batch5: real HTTP client disconnect and same-key recovery

Prepare the exact disconnect arguments in advance. Identify a real connector-client
transport close action before arming. It must actually close the active MCP HTTP request;
stop-model-output and refresh alone are not acceptance. If the client offers no verifiable
transport close action, mark this connector case unaccepted and stop, without inventing one.

Give Claude this complete instruction beforehand: Call hold using exactly the disconnect
arguments. During the observable waiting gate the active HTTP client will be disconnected.
After administrator evidence shows completed, reconnect the same test connector and call
hold with exactly the original key/content once. Save the recovered original receipt.
Do not regenerate the key and do not send a substitute content.

Administrator commands, in order:

```sh
python -B /app/deploy/lsf-zeabur/c3_control.py arguments disconnect
python -B /app/deploy/lsf-zeabur/c3_control.py status
python -B /app/deploy/lsf-zeabur/c3_control.py arm disconnect
python -B /app/deploy/lsf-zeabur/c3_control.py watch-disconnect
```

Start the watcher immediately before Claude's already-prepared call. A second local
terminal may use status to observe waiting. Upon waiting, perform the preidentified real
client transport close action. The watcher checks waiting+http.disconnect for that exact
request/generation, releases the provider gate itself and waits for completed, all within
its45-second window. No chat relay is needed. A timeout or missing event is unaccepted;
the gate may finish normal passthrough at its own deadline, which does not convert a failed
observation into success. Do not automatically rearm or replay an unobserved experiment.

After a successful watcher result, reconnect and replay the original hold once. Compare
actual receipt, one bucket with the original body, and unchanged provider counts; normal
related effects are permitted. Save status, disarm and stop.

## Acceptance record

Keep actual tool name/arguments/key, first/replay receipts, provider count deltas, generation
and waiting/http.disconnect/released/completed metadata. Check C2 marker bytes/21 files/
SQL rows/vectors unchanged using the original complete manifest validator. Never print
or archive query tokens, cookies or provider auth headers. A local WSL client test does
not count as Claude connector or Zeabur acceptance.
