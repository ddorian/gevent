# Task: fork-poisoned Postgres connections ("SSL error: sslv3 alert bad record mac")

Status: **reproduced and fixed** in the gevent checkout (2026-07-29). Not yet
deployed, not yet upstreamed. See "Reproduction" and "The fix" below; the
sections after them are the investigation as it stood before, kept because the
elimination work still stands.

## Reproduction (../gevent/gevent_fork_ssl_poisoning_repro.py)

The mechanism below is no longer a hypothesis. A standalone script reproduces
it against a real TLS server in ~2 seconds, with no database, no S3 and no
ffmpeg: worker greenlets talk to a TLS echo server, one
`os.register_at_fork(after_in_child=)` handler yields (standing in for
staging's OpenTelemetry/Sentry handlers, per FFPROBE_PIPE_SPLICE_HUNT §0.2),
and the process forks.

    unfixed gevent, MODE=fork        4 of 4 connections dead, every run
    unfixed gevent, MODE=subprocess  4 of 4 connections dead, every run
    control (YIELD=0, no handler)    clean

Both alert costumes appear, and which one you get is a timing detail of the
same desynchronisation:

- `PARK=runnable` -> `[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC]` (the parent
  fails to decrypt a record whose sequence the child consumed)
- `PARK=timer` -> `[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record
  mac`, byte-for-byte the staging signature, from both psycopg and botocore

`MODE=subprocess` is the shape this application actually has (a
`gevent.subprocess` spawn, i.e. ffmpeg); `MODE=fork` is a plain `os.fork()`
whose child does nothing but `os._exit(0)`, which shows the damage happens
*inside* `fork()`, before it returns to anyone.

## The fix (branch `fix-fork-ssl-poisoning`, off the deployed `056b4710`)

Two commits, because the defect has two halves.

1. `237110a2` **gevent/threading.py: cancel the parent's queued greenlet
   switches in a forked child.** Everything on the loop's callback queue is a
   pending switch into a greenlet, and in the child every one is a copy.
   `_ForkHooks._stop_running_greenlets_in_child` already said in its own
   comment that making them *look* stopped was not enough; this cancels them.

   This is the fix that was tried before and reverted for hanging
   `test__threading_2.ThreadJoinOnShutdown.test_3_join_in_forked_from_thread`
   (FFPROBE_PIPE_SPLICE_HUNT §0.4, attempt 2). **The diagnosis in that note was
   wrong.** The child did not need a greenlet it had lost. Each callback is the
   loop's record of a reference it took in `run_callback` and hands back in
   `_run_callbacks`, so replacing the queue leaked one `ev_ref` per entry, and
   the child's loop could then never become unreferenced. It hung. Confirmed by
   dropping the queue *and* handing back the matching unrefs by hand, which
   passes. Stopping each callback in place instead of discarding the queue is
   the whole correction, and the test passes in 0.1s.

2. `8f7e16b5` **gevent/subprocess.py: give the pre-exec child a hub of its
   own.** Layer 1 alone does *not* fix this bug (measured: still 4 of 4
   poisoned), because the victims are usually parked on an `io` watcher or a
   timer, and the loop resumes those itself rather than via the callback queue.
   So the child gets a fresh hub instead, which puts the parent's watchers, its
   queued callbacks and its hub greenlet out of reach. (That greenlet is
   suspended inside the parent's `loop.run()`, and would carry on polling if
   anything switched to it.) `default=False` matters: libev's default loop is a
   process-wide singleton, so asking for it again hands back the parent's loop,
   watchers and all.

   This is direction 1 from "Upstream fix directions" below, and it reuses the
   `_forking_for_exec_in` marker the deployed `05dd2f2c` fix already
   maintains.

Measured with both: 20 consecutive runs of the repro in each victim shape,
0 poisoned. Regression tests `test__fork_child_greenlets`,
`test__subprocess_fork_child_hub` and `test__subprocess_fork_child_pipe` fail on
unfixed gevent and pass on fixed.

Full suites, each diffed against a baseline run of the same bed:

| bed | baseline | with the fix | failing set |
|---|---|---|---|
| CPython 3.10.12, vs master | 9/72 | 9/74 | identical |
| **CPython 3.14.6** (staging's), vs the deployed `056b4710` | 4/85 | 4/87 | identical |

The 3.14 baseline's four are `monkey_test test_weakref.py`,
`monkey_test test_subprocess.py`, the `test__socket_dns` job and the
`test__subprocess` job (the known flaky `test_run_with_shell_timeout_and_capture_output`);
all four fail identically without the fix.

**Confirmed on the deployed pin, on staging's exact Python.** Running the repro
against `056b4710` under CPython 3.14.6 poisons 4 of 4 connections with the
verbatim staging alert, so the four fixes already deployed do *not* cover this.

### Upstream: gevent#2023 is the same family

There is no existing gevent issue about TLS/fork poisoning (searched
2026-07-29). The nearest is **#2023**, open since 2024-02-20, "gevent gets
stuck when gevent.threadpool is used inside a fork hook". A fork handler that
touches the hub is the same trigger seen from the other end, deadlock rather
than corruption. Measured: its reproducer hangs on `056b4710` and
completes with this fix. The raw-`os.fork` variant of it still hangs, for the
reason in the next section. #1865 and #2055 are already known here; #1992
(`BAD_LENGTH` under Locust) is TLS corruption but involves no fork.

### The raw `os.fork()` hole, closed later the same day

The two commits above leave a plain `os.fork()` still able to poison an
inherited TLS session, because that child keeps the hub it inherited. Measured
with `gevent_raw_fork_poisoning_repro.py`, which is the subprocess reproducer
with the spawn replaced by a fork whose child only calls `os._exit(0)`: 5 of 5
runs poisoned.

Giving that child a fresh hub is not available. `gevent.socket.socket.__init__`
does `self.hub = get_hub()` and binds its io watchers to that hub's loop, so a
listening socket created before the fork would be left talking to a loop nobody
runs, and gevent's own bind/fork/accept server pattern depends on it working.

3. `9be51589` **gevent/threading.py: cancel the parent's pending waits in a
   forked child.** The callback queue holds only the greenlets that were
   *runnable*; a blocked one is parked on a watcher and the loop resumes it
   from there. A watcher active at fork time is active precisely because
   something is parked on it, and gevent's watchers are created once per object
   and started and stopped around each wait, so stopping one cancels the resume
   without damaging the object: the child's next wait starts it again. Keyed on
   the callback being a `Waiter.switch`, so signal handlers and `Timeout` are
   left alone.

   Checking `active` alone finds nothing. A watcher that has already fired is
   inactive but still *pending*, which is the ordinary state for a greenlet in a
   short `sleep`; the first version stopped 0 of the 7 watchers it walked.

   Raw-fork reproducer 5 of 5 poisoned to 5 of 5 clean;
   `test__fork_child_waits` fails 6 of 6 without it. Costs one
   `gc.get_objects()` walk per fork in the child, about 2ms against a 690,000
   object heap, against 70ms for the spawn it accompanies.

This one is on `all-fixes-not-upstream` but deliberately **not** on
`fix-fork-ssl-poisoning`: it changes what every forked child does, which is the
semantics `gevent/os.py` defers to a major release, so it wants its own
discussion upstream.

Not exercised: a real bind/fork/accept server accepting on an inherited listener
under load. `test__core_fork`, `test__os` and `test__threading_2` pass, which is
not the same thing.

### Next

- Push `fix-fork-ssl-poisoning`, advance the pin from `05dd2f2c`, redeploy
  staging, and watch for `bad record mac` in Sentry.
- Report upstream with `gevent_fork_ssl_poisoning_repro.py`, which runs
  standalone on pinned released versions:
  `uv run --script gevent_fork_ssl_poisoning_repro.py`. Layer 1 is
  uncontroversial; layer 2 is the one that needs discussion, since
  `gevent/os.py` already documents this behaviour class as changing only in a
  major release. Worth attaching to #2023 as well.

---

Status before this session: needs-work (upstream gevent; app-side blast radius
already contained)

## Summary

Under normal encode load, a pooled Postgres connection can be corrupted at the
TLS layer by an unrelated subprocess spawn in the same process. The victim sees
`psycopg.OperationalError: consuming input failed: SSL error: sslv3 alert bad
record mac` on its next statement, the session then cascades into
`PendingRollbackError`, and the queue job fails and retries. The retry
succeeds; since commits `40ca8de95` (failed batches drain their spawned work)
and `9a7136f31` (outputs publish by rename, never in place), the failure is
transient and cannot corrupt encode artifacts anymore. The trigger itself is
not fixed and belongs upstream, in gevent.

## The incident (staging, 2026-07-29, job 491)

- 13:44:16 `VideoEncodeJob` attempt 1 claims video `7488226943596934144`.
- 13:44:32 the inline audio batch encode starts. It commits first
  (`commit_for_idle_transaction` inside `AudioTrack._batch_encode_presets`),
  so no transaction is held; the connection goes back to SQLAlchemy's pool.
- 14:01:47, first statement after the encode (the `asset.text_tracks` lazy
  load in `import_all_subtitles_from_asset`): `consuming input failed: SSL
  error: sslv3 alert bad record mac`. The full chain is stored on the queue
  row (`queue_v1.exceptions`, id 491).
- The job failed, retried 14 seconds later, and succeeded. The moov-atom
  corruption that followed was a separate, now-fixed consequence (the retry
  overlapped the failed attempt's still-running encoder).

## Second manifestation (staging, 2026-07-28): same alert, different protocol

One day earlier, an encode job died inside `Preset.upload_output` with
`botocore.exceptions.SSLError: ... [SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3
alert bad record mac` while PUTting `pl.mp4` to OVH S3
(`s3.gra.io.cloud.ovh.net`), through `gevent/ssl.py` `recv_into`. Same alert,
same process family (encode worker), but the victim is a pooled **HTTPS**
connection from urllib3/botocore, not a Postgres connection from psycopg.

This matters for attribution: two different client libraries (psycopg,
urllib3), two different protocols (Postgres wire over TLS, HTTPS), two
different servers (Ubicloud PG, OVH S3), one shared factor: the gevent process
that forks ffmpeg constantly. Any explanation specific to psycopg, PgBouncer,
or the PG server is now untenable; the corruption happens to whatever
long-lived TLS connection the process holds. That is exactly what the
fork-poisoning mechanism predicts, since the pre-exec child inherits copies of
*every* open TLS session, not just Postgres ones.

## Ruled out

- **Server-side timeouts.** At incident time the database had no
  `idle_in_transaction_session_timeout` (SHOW returned 0, `pg_db_role_setting`
  was empty). The 15min limit (migration `6df78e153908`) landed later that day
  and kills idle transactions with a clean server message, not a TLS alert.
- **Ubicloud's PgBouncer** (the `:6432` in the DSN). The same database kept a
  40-hour idle-in-transaction backend alive that morning, so the pooler does
  not reap idle connections, and a pooler closing a connection produces EOF,
  not a MAC failure.
- **`pool_pre_ping`.** Already True on staging/production (`ConfigBase`,
  see `SQLALCHEMY_ENGINE_OPTIONS`). A ping only proves the connection was
  healthy moments before; this poisoning lands concurrently with the victim's
  own traffic.

## Mechanism (was a hypothesis; now reproduced, see the top of this file)

"Bad record mac" is the server refusing a TLS record whose sequence number or
MAC does not match its session state. For that to happen on an otherwise idle
connection, a **second writer must have written TLS records on the same
socket**. In this process there is exactly one plausible second writer:

`gevent.subprocess` forks and execs **in Python**, so between `fork()` and
`exec()` the child is briefly a whole interpreter holding a copy of every
greenlet, every open fd, and every in-memory OpenSSL session state, including
the parent's Postgres connections (Python sockets are CLOEXEC, but that closes
them at exec, not at fork). It is empirically established on this codebase
(2026-07-27 hunt, see `docs/design/self-hosting-migration.md` section 12.7)
that the child's hub can run copies of the parent's greenlets in that window;
that is how one `subprocess.run` returned two, four or eight complete copies
of its child's output. A greenlet copy that was about to write on a Postgres
connection needs no fork and no exec to do damage: one `send()` from the child
advances a TLS sequence the parent's copy of the session also advances, the
server's MAC check fails on whichever record arrives with the stale sequence,
and the server sends `bad_record_mac` and closes. The parent finds the alert
on its next read. The encode workers fork ffmpeg constantly, so the window
recurs thousands of times per day.

Confidence: the fork-family defect is proven on this process (pipes, children);
the specific PG-socket manifestation matches the symptom exactly and no other
candidate survived elimination, but it has not been reproduced in isolation.

## Where the fix belongs

**gevent, not anywhere else.**

- **Not psycopg**: it is the victim. No client library can defend an fd that a
  forked child of its host process writes to.
- **Not CPython**: stdlib `subprocess` does fork+exec in C
  (`_posixsubprocess`) with no Python running in the child, so this window
  does not exist there. gevent deliberately replaces that path with its own
  Python implementation (and disables `posix_spawn`) so its child watchers
  stay attached.
- **Not Ubicloud/PgBouncer**: exonerated above.
- **Not this app**: mitigations that exist are shipped (see below). An
  application cannot prevent a forked child of itself from touching inherited
  fds before exec.

The deployed pin (`ddorian/gevent` rev `05dd2f2c`, branch
`all-fixes-not-upstream`, see commit `810bcf837`) already carries four fixes,
one of which makes a pre-exec child **refuse to fork**. That closes the
double-children/pipe-splice manifestations, but it does not stop the child's
hub from **running** greenlet copies, and a socket write needs no fork. That
is the remaining gap.

### Upstream fix directions

1. Extend the existing guard from "a pre-exec child refuses to fork" to "a
   pre-exec child never runs the inherited hub at all": neuter or park the hub
   in the `gevent.subprocess` child path immediately after `fork()`, so no
   application greenlet copy can be scheduled before `exec()`/`_exit()`.
2. Longer term: an exec-in-C or `posix_spawn`-compatible spawn path in gevent
   (needs child-watcher rework, which is why gevent disables `posix_spawn`
   today).
3. Strategic, app-side: the planned move off gevent to free-threading
   (`docs/design/` gevent-to-FT work) retires `gevent.subprocess` entirely and
   with it this whole defect class.

## Already shipped (blast-radius containment, this repo)

- `40ca8de95`: a failed job attempt drains its spawned work before the queue
  retry can start, so a poisoning-induced failure can no longer race a second
  encoder against its own working directory.
- `9a7136f31`: every existence-trusted output (encode temps, stitched rungs,
  chunk files, S3 downloads) is written to a unique `.part` sibling and
  renamed into place only on success, so even an overlap that slips through
  (for example a hung straggler swept past the pulse gate) cannot publish or
  read a torn file.
- Net effect: the poisoning now costs one benign Sentry event
  (`OperationalError` then retry) and a re-run's wall clock. Correctness is
  unaffected.

## Next steps

1. Audit the pinned gevent rev's `subprocess` child path: enumerate every
   operation between `fork()` and `exec()` that can switch to the hub, and
   confirm whether any hub run in the child is still reachable.
2. Reproduce: a fork storm (tight ffmpeg spawn loop) beside a TLS Postgres
   connection under write load in one gevent process; count
   `bad record mac` occurrences per hour. The 2026-07-27 hunt's harness on s1
   is the starting point.
3. If confirmed, patch `ddorian/gevent` `all-fixes-not-upstream` (direction 1
   above), advance the pin, and open the upstream gevent issue with the
   reproducer.
4. Meanwhile, track recurrence. The signature is any `bad record mac` in
   Sentry, which so far arrives in two costumes: `psycopg.OperationalError:
   consuming input failed: SSL error: sslv3 alert bad record mac` (Postgres
   victim; the follow-on `PendingRollbackError` is noise) and
   `botocore.exceptions.SSLError: ... SSLV3_ALERT_BAD_RECORD_MAC` (S3 HTTPS
   victim, seen 2026-07-28 in `Preset.upload_output`). The
   `pool_drain_on_error` warning marks any failure that had spawned work to
   drain.

## References

- Queue row with the full stored traceback: staging `queue_v1` id 491.
- Fork defect proven: `docs/design/self-hosting-migration.md` section 12.7.
- Pin: https://github.com/ddorian/gevent/tree/all-fixes-not-upstream at
  `05dd2f2cb2193defd16d4a563a951cabc9f403e3` (repo commit `810bcf837`).
- Related upstream: https://github.com/gevent/gevent/issues/1865,
  https://github.com/gevent/gevent/issues/2194.
- Containment commits: `40ca8de95`, `9a7136f31`. Migration that later bounded
  idle transactions: `6df78e153908` (`867c882e3`).
