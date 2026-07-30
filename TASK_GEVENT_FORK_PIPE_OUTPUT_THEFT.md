# Task: a pre-exec gevent.subprocess child can steal another subprocess's pipe output (suspected)

Status: **confirmed and fixed** (2026-07-29). The suspicion below was right in
every particular. Reproduced on demand against the deployed pin, and closed by
the same commit that closed the TLS poisoning, since both are the pre-exec
child's hub touching inherited fds.

## Confirmed, on CPython 3.14.6

`src/gevent/tests/test__subprocess_fork_child_pipe.py`: sixteen subprocesses
each write 1500 numbered 64-byte lines to a pipe that a greenlet is reading,
while another greenlet spawns 120 short-lived commands. Every line is numbered
and fixed-width, so a short read is arithmetic.

| build | result |
|---|---|
| deployed pin `056b4710` (the four-fix branch) | 6 of 6 runs stole bytes |
| `fix-fork-ssl-poisoning` (`8f7e16b5`) | 6 of 6 runs intact, and 12 of 12 of the standalone form |
| deployed pin, no yielding at-fork handler | 6 of 6 intact (control) |

A failure reads, for example, `[(1, 0, 192), (3, 0, 64), (4, 0, 64), ...]`:
pipe 1 short by 192 bytes (3 whole lines), pipe 3 by one line, and so on, with
every producer exiting **0**. That is the production signature from the task
below, where the app saw an ffmpeg run's combined output come back empty.

Answering the numbered questions:

1. **A runnable greenlet copy can be entered in the child** (confirmed). It does
   not need anything in gevent's own child branch to yield. The
   `os.register_at_fork(after_in_child=)` handlers run inside `fork()` before
   that branch begins, and one of those yielding is sufficient.
2. **The fix closes it.** It parks nothing and guards no individual operation;
   it gives the child a fresh hub, so every watcher the parent had, including
   the `io` watcher the pipe reader was parked on, is unreachable. That is why
   the TLS and pipe manifestations fall together.
3. **Reproducer: done**, as the regression test above, asserting
   bytes-received == bytes-written per pipe rather than eyeballing a total. One
   pipe finds the defect on roughly half of runs; sixteen find it on every run,
   because each fork window then has sixteen chances to land on a parked reader.
4. **The stdin-write case is covered by the same fix** and needs no separate
   guard: a copy cannot run in the child at all, so it can neither read a pipe
   nor write one. It is not separately tested.

A child of a plain `os.fork()` was still exposed when this was written, and is
covered as of `9be51589` on `all-fixes-not-upstream`, which cancels the parent's
pending waits in any forked child. See the sibling file.

---

Status when written: needs-verification, then a gevent-side fix. Sibling of
`TASK_GEVENT_FORK_SSL_POISONING.md`, same root defect, different victim: an
anonymous pipe instead of a TLS socket. Written for whoever is working on the
`ddorian/gevent` `all-fixes-not-upstream` branch; self-contained, but read the
sibling file for the full TLS evidence.

## The observation

Staging, 2026-07-28 07:05:07 UTC (Sentry HEAP-BACKEND-D9, app repo `stream`):
an ffmpeg run's captured stdout+stderr came back **completely empty**, so the
app raised `FFMpegError("Error while calling ffmpeg binary")` (the raise fires
on `total_output == ""`). The command was a normal chunk encode reading a
presigned S3 URL. The queue job (staging `queue_v1` id 298) retried and
succeeded 13 minutes later in the same process, so the input, the command
line, and the binary were all fine.

Environment: CPython 3.14.6, gevent 26.7.1.dev0 pinned to
`ddorian/gevent` branch `all-fixes-not-upstream` rev `05dd2f2c` (the four-fix
branch), monkey-patched uwsgi worker that forks ffmpeg constantly (a video
encode fleet).

## Ruled out

- **OOM kill**: the host's kernel journal has zero oom-kill events since
  2026-07-27, and none in the incident window.
- **Container stop / deploy kill**: the container booted 2026-07-27 19:34 and
  served until 2026-07-29; the retry succeeded inside it minutes later.
- **A normal ffmpeg failure**: any ordinary error prints to stderr, which
  would have been captured. Empty means killed-before-first-byte or the bytes
  went somewhere else. (The app currently discards `returncode` on this path,
  which would have disambiguated; an app-side change is planned so the next
  occurrence self-identifies. `returncode == 0` with empty capture would be
  near-proof of theft.)

## Suspected mechanism

`gevent.subprocess` forks and execs in Python, so between `fork()` and
`exec()` the child is briefly a whole interpreter holding copies of every
greenlet and every fd, including the **read end of the pipes the parent uses
to capture a different subprocess's stdout/stderr**.

The four-fix branch makes a pre-exec child refuse to *fork*, which closed the
2026-07-27 manifestation (one `subprocess.run` returning 2, 4 or 8
concatenated copies of its child's output). It does not stop the child's hub
from *running* inherited greenlet copies. A scheduled copy of the parent's
pipe-reader greenlet, entered in the child during that window, consumes bytes
from the shared pipe; those bytes never reach the parent. A read needs no
fork, no exec, and no write.

This is the pipe costume of the mechanism already established for TLS
sockets (see the sibling file: five sightings across 2026-07-27..29, both
read-side and write-side, Postgres and S3). On a TLS socket the theft is loud,
because the record layer's sequence numbers desync and the server sends
`bad_record_mac` or kin. On an anonymous pipe there is no MAC, so the theft is
silent: the parent just reads less than was written, in the extreme case
nothing at all.

Output duplication (2026-07-27, fixed) and output theft (this file) are the
two directions of one defect: the pre-exec child's hub touching inherited
pipe fds.

## The task, on the gevent side

1. In the pinned rev's `gevent/subprocess.py` child path, enumerate every
   operation between `fork()` and `exec()` that can enter the hub (the
   nonblocking-fd dance, closing fds, error paths, anything that yields).
   Confirm or refute that a runnable greenlet copy can be entered in the
   child.
2. Check whether the in-flight six-fix branch (its fixes 2 to 4 are described
   as one gevent.subprocess fork/exec family) already closes this window. If
   the fix parks or neuters the inherited hub in the child immediately after
   `fork()`, it should kill this manifestation and the TLS one together. If it
   only guards specific operations, pipe reads may still leak through.
3. Reproducer: one gevent process that (a) runs a subprocess producing large,
   steady stdout consumed by a greenlet that counts bytes, while (b) a tight
   loop fork+execs short-lived commands at high rate. Success bar: with the
   current four-fix pin, occasional short or empty reads on the consumer over
   hours; with the fix, byte counts match exactly, always. Assert
   bytes-received == bytes-the-child-wrote rather than eyeballing.
4. Same class, not yet observed, worth covering in the same fix and test: a
   child's greenlet copy **writing** into a subprocess's stdin pipe would
   corrupt the input stream of an unrelated subprocess.

## App-side confirmation signal (separate task, app repo)

The empty-output raise will start including `returncode`, pid, and whether
the output file exists. Sentry signature to watch: `FFMpegError: Error while
calling ffmpeg binary`. A recurrence with `returncode == 0` confirms theft; a
negative returncode redirects the investigation to whatever sent the signal.

## References

- Sibling task file: `TASK_GEVENT_FORK_SSL_POISONING.md` (mechanism, sighting
  tally, upstream fix directions, why the fix belongs in gevent and nowhere
  else).
- Pin: https://github.com/ddorian/gevent/tree/all-fixes-not-upstream at
  `05dd2f2cb2193defd16d4a563a951cabc9f403e3`.
- Upstream context: https://github.com/gevent/gevent/issues/1865,
  https://github.com/gevent/gevent/issues/2194.
- App evidence: Sentry issue HEAP-BACKEND-D9 (7637462256); staging `queue_v1`
  id 298; the 2026-07-27 duplicated-output hunt in
  `docs/design/self-hosting-migration.md` section 12.7.
