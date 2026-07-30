# Task: absorb the at-fork handler guard into gevent itself

Status: app-side guard SHIPPED and verified (this repo, commit `50b97ba27`,
staging 2026-07-30: the stitch that failed 13 consecutive times passed first
try). This file is the gevent-side half: move the same guard into
`gevent/subprocess.py` so every gevent application gets it, plus the API that
lets the app-side copy be deleted. Written for the `ddorian/gevent`
`all-fixes-not-upstream` branch; self-contained.

## gevent-side status, 2026-07-30

Items 1, 2 and 4 are done, on `all-fixes-not-upstream`: `09d08119` (the guard,
`gevent.config.atfork_guard`/`GEVENT_ATFORK_GUARD`, public
`gevent.subprocess.in_pre_exec_child()` with a Windows counterpart, changenote,
end-to-end pipe-marker test) and `046906f5` (the held-lock reproducer and the
concurrent-pair stress). The app-side `gevent_patch.py` can drop its pin on
`_forking_for_exec_in` and use `in_pre_exec_child()`, or be deleted outright
once running a gevent with `09d08119`.

**Item 3 (retry the fork once) was considered and skipped**, deliberately. The
retry has to go inside `_execute_child`, where two things make it invasive: the
parent closes the child-end fds before it reads the errpipe, so a second fork
needs those closes deferred past the retry decision; and the dead first child
has already been through `fork_and_watch`, so `_on_child` has set `returncode`
and completed `result` by then, and a retry that does not reset both leaves
`poll()` reporting 127 for a process that is running. That is ~30 lines of
surgery in the spawn path every POSIX gevent user runs, to cover a case this
file already describes as one that should never fire once handlers are guarded.
Revisit only if a handler registered *before* `gevent.subprocess` was imported
(a `coverage` started from a site hook is the realistic one) is ever seen
wedging a child in practice; until then the 1s deadline fails such a spawn
cleanly rather than hanging, which is the behaviour that matters.

Two corrections to this file's own text, both measured on `d1b08752`:

- The deterministic reproducer under "Deterministic reproducer" does **not**
  fail on this rev. `subprocess.run` succeeds in ~0.01s. The handler's wait on
  the contended lock reaches a hub whose loop has nothing to do, so it ends in
  a `LoopExit` that CPython prints as "Exception ignored in:" and then ignores,
  and the child goes on to `exec`. Reaching `_ForkedChildHubEntry` needs the
  child's loop to have referenced work, which the application had and a reduced
  case does not. The regression test that *is* deterministic is the pipe
  marker: a handler either writes its byte or it does not.
- Consequently the "Validation bar" as stated cannot be met by that reproducer
  on this rev, and was not used. What was verified instead: the pipe-marker
  contract both ways, and the full 3.14 suite run with the guard on and off,
  same failing set, the only difference being the guard's own test, which must
  fail when the guard is off.

## The defect, one paragraph

`os.register_at_fork(after_in_child=)` handlers run inside `fork()`, in the
child, before `gevent.subprocess._execute_child` can get to `exec()`. Those
handlers are written for a child that *continues* — OpenTelemetry's
providers reinitialise locks and rebuild a `ThreadPoolExecutor`, sentry-sdk
resets its worker-thread state, filelock reinitialises locks — and under
monkey-patching one of them can block forever on a lock whose holder is an
unrunnable greenlet copy. Rev `527efc0e` already bounds the damage (the
`_PreExecChildHub` deadline kills the child at 1.0s and the parent's spawn
fails with `_ForkedChildHubEntry`), but the spawn is still lost. On a code
path that forks concurrently the collision is near-certain, not rare: our
2-rung stitch runs two `ffmpeg -f concat` spawns at once, each fork landing
while the other spawn's instrumentation is mid-critical-section. Measured on
staging: 9 of 9 stitches lost both concats (Sentry HEAP-BACKEND-DQ,
7641283058, child `returncode=127`), while 46 of 46 *serial* chunk forks
were clean. CPython's own subprocess never sees any of this because its C
`fork_exec` does not run Python at-fork hooks; gevent's Python-level fork
does.

## Registrant census (what actually runs in the child, this app)

- `opentelemetry/sdk/trace/__init__.py` `ConcurrentMultiSpanProcessor`:
  rebuilds a `concurrent.futures.ThreadPoolExecutor`.
- `opentelemetry/sdk/_logs/_internal/__init__.py` `LoggerProvider` and
  `opentelemetry/sdk/metrics/_internal/__init__.py` `MeterProvider`: fresh
  locks + `_update_resource(_get_process_dependent_resource())`.
- `sentry_sdk/monitor.py`, `sentry_sdk/_batcher.py`,
  `sentry_sdk/_span_batcher.py`: thread-state resets.
- `filelock/_api.py`, `filelock/_read_write.py`: lock reinit.
- `coverage/patch.py` under subprocess coverage (test runs).

Individually each looks cheap; the killer is any of them touching a
monkey-patched lock (`logging` included) that another greenlet holds at the
fork instant. Which one blocked on staging was never pinned down — the guard
makes the question moot by skipping them all.

## The app-side guard to absorb (working, tested)

`gevent_patch.py` in this repo, installed immediately after
`monkey.patch_all()`:

- Wraps `os.register_at_fork`. Registrations made *after* the wrap get their
  `after_in_child` handler replaced by a closure that first checks
  `gevent.subprocess._forking_for_exec_in`: set and different from
  `os.getpid()` means "I am a pre-exec child" — return without calling the
  handler. `before` and `after_in_parent` pass through untouched.
- gevent's own `_detach_inherited_hub_in_pre_exec_child` is registered
  earlier (at `gevent.subprocess` import, inside `patch_all`) and therefore
  stays unwrapped — it MUST keep running in the pre-exec child.
- Skipping is safe precisely because this child execs: nothing a handler
  would reinitialise survives. A real fork's child (marker unset) still runs
  every handler.

Regression tests to port: `tests/test_gevent_patch.py`. The end-to-end one
registers a handler that writes one byte to an inherited pipe, then runs a
gevent subprocess (must contribute nothing) and a plain `os.fork()` child
(must contribute exactly one byte). Verified red with the guard disabled.

## Deterministic reproducer (no app needed)

The staging failure, distilled — the handler blocks on a lock a greenlet
holds across the spawn, and on the current rev the spawn dies at the 1s
deadline:

```python
import gevent.monkey; gevent.monkey.patch_all()
import os, subprocess, threading
import gevent

lock = threading.Lock()

def handler():
    with lock:      # held by `holder` at fork time; owner unrunnable in the child
        pass

os.register_at_fork(after_in_child=handler)

def holder():
    with lock:
        gevent.sleep(5)

gevent.spawn(holder)
gevent.sleep(0.1)
subprocess.run(["true"])   # raises _ForkedChildHubEntry after ~1s; with the guard: succeeds
```

## The work, in gevent source, priority order

1. **Install the registration wrapper in `gevent/subprocess.py`** at module
   import, right where `_detach_inherited_hub_in_pre_exec_child` is
   registered today (that ordering guarantee — gevent registers before the
   wrap, applications after — is the whole trick, and gevent's import
   position makes it airtight there in a way an app-side install can only
   approximate). Suggested shape: keep a module-level reference to the raw
   `os.register_at_fork`, register gevent's own handler through it, then
   rebind `os.register_at_fork` to the wrapping version. Consider a
   `GEVENT_ATFORK_GUARD=0`-style escape hatch, since this changes observable
   behavior for any application that (wrongly) relies on `after_in_child`
   running in exec-bound children.
2. **Expose a public predicate**, e.g. `gevent.subprocess.in_pre_exec_child()`
   (trivial: the existing `_in_pre_exec_child`). Two consumers immediately:
   this repo's guard drops its pin on the private `_forking_for_exec_in`
   name, and instrumentation libraries (OTel/sentry upstream) could guard
   their own handlers — the cleanest long-term home for the check.
3. **Retry the fork once on `_ForkedChildHubEntry`** in `_execute_child`. A
   child killed before `exec()` has no side effects, so a retry is safe by
   construction. With handlers guarded this should never fire; it covers the
   residual case the wrapper cannot: handlers registered before
   `gevent.subprocess` was imported (e.g. coverage started by site hooks),
   and locks entered by non-handler means.
4. **Port the tests**: the pipe-marker end-to-end test, the reproducer above
   as a deadlock-regression test, and a concurrent-pair stress (two spawns
   racing while a greenlet cycles a contended lock) — the staging evidence
   says concurrency is what turns "possible" into "always". The existing
   `test__subprocess_fork_child_hub.py` family is the natural neighbor.

## Validation bar

The reproducer above must complete `subprocess.run` successfully with the
guard, and fail with `_ForkedChildHubEntry` without it. On the app: the
2-rung chunked stitch (two concurrent `-f concat` spawns) over a staged
46-chunk video — the exact workload that scored 0 for 13 across three pins —
passes; first verified green 2026-07-30 06:25-06:45 UTC on image
`ead9f1be3` (guard) over rev `527efc0e`.

## References

- App commit `50b97ba27` (`gevent_patch.py` + `tests/test_gevent_patch.py`);
  deployed image `ead9f1be3`.
- Sentry: HEAP-BACKEND-DQ (7641283058) `_ForkedChildHubEntry`;
  HEAP-BACKEND-DG (7640863001), the same failure's bogus-SIGSEGV costume on
  the six-fix pin.
- Siblings: `TASK_GEVENT_FORK_CHILD_HUB_WEDGE.md` (the autopsy + the
  2026-07-30 verdict this file extracts its asks from),
  `TASK_GEVENT_FORK_SSL_POISONING.md`, `TASK_GEVENT_FORK_PIPE_OUTPUT_THEFT.md`.
- Upstream context: https://github.com/gevent/gevent/issues/1865,
  https://github.com/gevent/gevent/issues/2194.
