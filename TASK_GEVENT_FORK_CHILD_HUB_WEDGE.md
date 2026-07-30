# Task: a pre-exec gevent.subprocess child re-initialized the hub and RAN for two hours

Status: **reproduced and fixed** (2026-07-30), as `a590c21f` on
`all-fixes-not-upstream`. Not a separate defect: the same pre-exec window as the
two sibling files, and the fix is a revision of the one on the pin.

## Corrections to the autopsy below

Two things in it are wrong, and one of them mattered.

**The fresh epoll fds are not `ev_loop_fork`.** §4 infers a reinit ran by
accident and concludes from it that "the hub machinery *worked* in the child and
took over". The premise is wrong: `_execute_child` forks with
`monkey.get_original('os', 'fork')`, the raw one, so `gevent.hub.reinit` never
runs on that path at all. The fresh epoll fd is `set_hub(Hub(default=False))` --
the pin's own fix, by design. The conclusion happens to be right, for a blunter
reason: **that fix handed the child a working hub.**

**`close-on-exec` being powerless is a symptom, not a footnote.** It reads as bad
luck in the environment section. It is the whole mechanism: the child held 217
fds *because* it never exec'd, and it never exec'd because it never left the
loop.

## What actually happens

A fork handler that leaves anything on the loop and then waits for something only
the parent could provide waits forever. The loop has work, so it never raises
`LoopExit`; `fork()` never returns to `_execute_child`; no `exec` happens. That
explains every observation at once: no exec (§1), a loop of its own that runs
(§3, §4), a periodic timer still armed (§3 -- OpenTelemetry's
`BatchSpanProcessor` schedule delay is 5000ms), 217 retained fds (§5), and
survival for hours.

The parent's side is why nothing noticed: it blocks in `_execute_child` reading
the error pipe for an EOF only `exec` or `_exit` produces, and that read sits
inside `Popen.__init__`, so a `timeout=` on the spawn has not started counting.
A local repro hung a 6-second timeout past 45 seconds.

Reproduced deterministically in about a second: a handler that spawns a greenlet
and then waits on an `Event` nobody sets. Child: parent's cmdline, `ep_poll`,
utime climbing, two epoll fds, all fds retained. Ruled out along the way: a
handler that merely *starts* a thread execs fine (`Thread.start()` returns as
soon as the greenlet does), and the child branch's `os_close` is the raw
`os.close`, not a cooperative one.

## The fix, and what direction 4 gets wrong

Task direction 4 asks for a hub that aborts the child on any attempt to enter it.
I implemented exactly that first, and it is wrong: a handler that yields is the
*ordinary* case in a monkey-patched process, and every existing test here spawns
with a handler doing `gevent.sleep(0)`. All of them failed. In this app, whose
instrumentation registers such handlers, no ffmpeg would ever have run.

The distinction the crude form misses is time. So the loop runs and a timer says
for how long: one second, against a window normally under a millisecond. The
timer is unreferenced, so a handler with nothing to wait for still gets
`LoopExit` immediately, and it fires only when something else keeps the loop
alive. The child then reports itself down the error pipe and exits, and the spawn
raises `SubprocessError` rather than hanging. A failed spawn is retryable; a
starved queue is not.

Covered by `src/gevent/tests/test__subprocess_fork_child_wedge.py`, which is
load-bearing: without the deadline it was still hung at 40s.

**Still open:** the exact PG poisoning mechanism (§"Unknowns"). The deadline
bounds the child's life to one second, which removes the two-hour co-tenant, but
nothing here proves what a child does to a pooled connection inside that second.
The reproducer in item 5 with a real DB pool is still worth building.

---

Status when written: CONFIRMED, live specimen fully autopsied on staging
2026-07-29, ON the six-fix pin (`a9a334b3`). Needs a gevent-side fix on the
`ddorian/gevent` branch. Written for whoever is working on that branch;
self-contained.

Sibling context: `TASK_GEVENT_FORK_SSL_POISONING.md` (committed as
`26891ea77`; five TLS bad_record_mac/decryption_failed sightings 07-27..07-29)
inferred that a pre-exec `gevent.subprocess` child touches inherited sockets. A
second file on the pipe costume (an ffmpeg capture coming back empty,
`returncode` disambiguation shipped in the app) inferred the same for pipes.
Both treated the child as a brief race window between `fork()` and `exec()`.
**This specimen shows the window is not brief: a child that misses its exec
becomes a persistent second interpreter that re-initializes the event loop,
runs greenlet code (26.7 CPU-seconds!), keeps its own timers firing for hours,
and holds a clone of every fd the parent had at fork time.** Every sibling
costume (TLS theft, pipe theft) is downstream of this: the thief is not a
racing fork-instant, it is a long-lived co-tenant process.

## TL;DR for the fixer

At 19:32:01 UTC the staging queue worker forked twice (one second apart is not
resolvable; both children have identical start times). Neither child ever
reached `exec()`: two hours later both still had the parent's cmdline
(`uwsgi --ini=uwsgi.ini`), were single-threaded, parked in `epoll_wait`, had
**burned 26.7s of user CPU**, had **re-initialized the libev backend** (their
epoll fds and registration sets differ from the parent's), woke on a ~5s
periodic timer, and held 217 cloned fds each: all 5 of the app's pooled
PgBouncer connections, 52 S3 TLS connections, a ClickHouse TLS connection.
34 minutes after the fork, the parent's every PostgreSQL round-trip began
hanging forever, freezing the whole job queue until a container restart.

The fix must make the child's post-fork, pre-exec state hub-proof: a child on
the fork+exec path must never be able to re-enter, re-initialize, or run the
hub, no matter what happens between fork and exec (including exceptions on
that path). Details and directions below.

## Environment (exact)

- CPython 3.14.6, python-build-standalone build (no `.PyRuntime` section, so
  PEP 768 remote attach does not work; py-spy cannot walk 3.14 frames either.
  Forensics below therefore use /proc, faulthandler, and app logs only.)
- gevent 26.7.1.dev0 pinned to
  `https://github.com/ddorian/gevent?rev=a9a334b3370d6bfb30d9f3a1f002de4495438cbf`
  (the six-fix / "fork-child hub fixes" branch; from the deployed image's
  `uv.lock`). The four-fix predecessor was `05dd2f2c`.
- Monkey-patched via `patch_all_fast()` (plain `patch_all` with the
  entry-point rescan cached; `gevent_patch.py` in the app repo).
- uwsgi: `master = 1`, `processes = 1` (a single worker runs both HTTP and
  the PgQueue job workers), `gevent = 100` async cores,
  `enable-threads = true`, `close-on-exec = true`. Note `close-on-exec` is
  powerless against this bug: CLOEXEC fires at exec, and the child never
  exec'd, so all 217 fds stayed open.
- Workload: a video-encode fleet worker that forks ffmpeg/ffprobe constantly
  through `gevent.subprocess`.
- Deployed image `62afe96d7`, booted 18:52:05 UTC 2026-07-29 (staging,
  host s1.server.heapstream.com). All timestamps below are UTC that day.

## Process map at autopsy time (~21:40)

| host PID | container PID | role | started |
|---|---|---|---|
| 3511002 | 19 | uwsgi master | 18:52:06 |
| 3511013 | 30 | **the** worker (HTTP + queue + all forks) | 18:52:06 |
| 3511014 | 31 | uwsgi http router | 18:52:06 |
| 3537247 | 2041 | **wedged pre-exec child** of 3511013 | 19:32:01 |
| 3537248 | 2042 | **wedged pre-exec child** of 3511013 | 19:32:01 |

## Direct observations

Each with the command used, so a future wedge can be autopsied identically.

**1. cmdline never replaced — exec never happened.**

```
$ ps -o pid,ppid,etime,cmd -p 3537247 3537248
    PID    PPID     ELAPSED CMD
3537247 3511013    02:06:28 uwsgi --ini=uwsgi.ini
3537248 3511013    02:06:28 uwsgi --ini=uwsgi.ini
```

**2. Single-threaded, sleeping in the event loop (kernel stack).**

```
$ awk '/^State|^Threads/' /proc/3537247/status   → State: S (sleeping), Threads: 1
$ cat /proc/3537247/stack
[<0>] ep_poll+0x2bb/0x380
[<0>] do_epoll_wait+0xba/0xe0
[<0>] __x64_sys_epoll_wait+0x60/0x100
```

**3. The child RAN — 26.7 user-CPU-seconds — and still wakes ~every 5s.**

```
$ awk '{print "utime="$14" stime="$15}' /proc/3537247/stat   → utime=2668 stime=136   (ticks @100Hz = 26.7s / 1.4s)
voluntary_ctxt_switches: 1463 → 1464 over a 5s window (one wake per ~5s: an armed periodic timer)
```

This is the single most important observation: the child is not blocked in
some pre-exec syscall. It executed a substantial amount of Python after
fork — greenlet code, timer callbacks — and settled into an idle-but-armed
event loop. (For contrast the 2h47m-old parent had utime=164.9s.)

**4. The child re-initialized the loop backend — it is running its OWN hub.**

```
parent 3511013: one epoll fd (fd 40), 17 registered fds
child  3537247: THREE epoll fds (fd 7, 41, 67) with small, DIFFERENT
                registration sets (4, 0, and 2 tfds)
```

(`ls -l /proc/PID/fd | grep eventpoll`; registrations via
`/proc/PID/fdinfo/<fd>`.) Fresh epoll fds in the child mean `ev_loop_fork`
style reinit ran there: the child detected the new pid, rebuilt the backend,
registered its own watchers, and entered `ep_poll` as a functioning loop.
This rules out "child passively blocked": the hub machinery *worked* in the
child and took over.

**5. 217 cloned fds retained (parent holds only 116 by now).**

Socket census by peer, decoded from `/proc/3537247/net/tcp` matched against
the child's fd inodes:

```
 52x  141.95.161.76:443    OVH S3 (the boto3 connection pool mid chunk-download)
  5x  136.243.238.181:6432 PgBouncer — clones of the app's ENTIRE PG pool
  1x  <s1 public IP>:9443  ClickHouse TLS
  ~6x 127.0.0.1:<various>  local (uwsgi/vector/OTLP)
```

Clone proof: same fd number → same socket inode in both processes
(`readlink /proc/{3511013,3537247}/fd/103` → both `socket:[32577638]`).
And fd 100: the parent has already closed it, the child still holds
`socket:[32595682]` — the child keeps connections alive that the parent
believes closed, defeating server-side connection cleanup.

**6. Production ran the same image, same boot hour, and was clean at 21:50** —
the wedge is probabilistic (as every sighting in the sibling files was).

## Collateral damage in the parent (why this class of bug is P0 for us)

The parent worker survived but its job system died in slow motion:

- 19:32:01 — the fork pair wedges. The parent's in-flight encode job keeps
  working (chunk downloads, lease pulses flowing) for another ~28 minutes.
- ~20:00 — the encode attempt goes silent (no more lease pulses).
- 20:05:35 — the queue's stale-lease sweep reclaims the job's row.
  **20:05:38 — that sweep's completion is the LAST successful PostgreSQL
  round-trip the process ever made.** The app log shows 121
  `resetting_stale_jobs` (start) lines, still firing every minute two hours
  later, and zero `reset_stale_jobs` (completion) lines after 20:05:38: every
  subsequent DB call hangs forever.
- Hung ticks pile up as greenlets → uwsgi logs `[DANGER] async queue is
  full !!!` repeatedly. HTTP kept serving (Redis-backed endpoints fine); only
  PG-dependent paths hang. An external psql THROUGH THE SAME PgBouncer worked
  the whole time, so the pooler and network are exonerated: the poisoning is
  process-local, on connections whose clones the child holds.
- The zombie job still occupies its worker slot (`job_pulse_lost job_id=585`
  logged every minute), so its queue row can never be re-claimed: the whole
  queue (webhooks, emails, captions, encodes) is frozen until restart.
- Parent's five PG sockets at autopsy: `Recv-Q 0 Send-Q 0` (via
  `nsenter -t 3511013 -n ss -tn '( dport = :6432 )'`). Empty receive queues
  while readers hang forever is consistent with replies consumed elsewhere
  (theft) and with replies never sent (a connection desynced by a child
  write); distinguishing these is part of the reproducer's job.

## Unknowns, honestly labeled

- **Which spawn produced the pair.** The app log has NO lines in
  19:31:55..19:32:10. Phase-dating from the 52 cloned S3 sockets: the fork
  happened while the encode attempt was mid parallel-chunk-download. Two
  forks with the same start second suggests a concurrently-spawned pair
  (this app runs ffprobe/ffmpeg pairs through a parallel helper). The
  parent's encode attempt kept pulsing for ~28 more minutes, so if the pair
  belonged to a probe with a timeout the code may have abandoned them and
  moved on. Call-site identification would need fork auditing (see task 5;
  note the sibling file's lesson: wrapping `os.fork` sees NOTHING for these,
  you must wrap `gevent.subprocess`'s own fork path).
- **The exact child-side path from fork to running hub.** That is THE task,
  below.
- **The exact PG poisoning mechanism** (steal-on-read by the child during its
  active phase vs a child write desyncing TLS/protocol state vs something
  subtler). The 34-minute delay between fork and first hang, and the 26.7s of
  child CPU, both need explaining by whatever theory wins.

## The task, on the gevent side (rev a9a334b3)

1. **Map the complete child-side code path.** In `gevent/subprocess.py`'s
   `_execute_child` child branch, list everything that runs between `fork()`
   and `exec()`: the dup2/close dance, error paths, and — critically — every
   route into `get_hub()` / greenlet switches. Include what an EXCEPTION on
   that path does: CPython's plain subprocess writes the errno to a pipe and
   `os._exit`s; if this fork's child branch can propagate an exception into
   generic Python machinery instead, that is a plausible route into the hub.
2. **Include the atfork hooks.** `os.register_at_fork(after_in_child=...)`
   hooks run in ANY `os.fork` child. In a monkey-patched process that
   includes gevent's own hub-reinit hooks (meant for fork+CONTINUE children,
   e.g. multiprocessing — actively harmful on a fork+EXEC path), CPython's
   `threading._after_fork`, and whatever else registered. Enumerate what
   actually fires in this child and in what order. The observed
   fresh-epoll-fds state means SOME reinit path ran to completion.
3. **Explain the specimen.** A credible theory must produce: no exec, a
   re-initialized loop with its own watchers, ~27s of CPU, a ~5s periodic
   timer still armed, and survival for hours. "The child entered the hub once
   and blocked" does not explain the CPU time; "inherited greenlet copies
   resumed and the run loop simply kept going as if it were the parent" does.
4. **Fix direction: make the fork+exec child hub-proof.** Immediately after
   `fork()` in the child, before anything else: neuter the hub so ANY attempt
   to enter it aborts the child (`os._exit(127)` via a poisoned
   `get_hub`/switch stub is the crudest sufficient form), and ensure the
   fork+exec path never runs the fork+continue atfork machinery. The sibling
   file's fix directions (refuse-to-fork guards) proved insufficient: the
   four-fix branch stopped child re-forks, the six-fix branch was running
   here and the child still re-initialized and ran its hub. Longer-term
   direction worth weighing: move `gevent.subprocess` onto
   `_posixsubprocess`/`posix_spawn` like CPython, precisely so no general
   Python ever runs in the child.
5. **Reproducer + detection harness.** One gevent-patched process that:
   (a) runs steady TLS traffic (a DB pool with a query loop is ideal — it
   detects poisoning as a hang/protocol error), (b) spawns short-lived
   subprocess pairs concurrently at high rate from many greenlets, with
   periodic timers armed (mirror the app: ~100 greenlets, timers at 1-5s),
   (c) a watchdog thread in the PARENT that every second scans
   `/proc/self/task/../../<child>/cmdline` — any child whose cmdline equals
   the parent's own for >5s is a wedge; on detection dump the child's
   `/proc/<pid>/stat` (utime!), `wchan`, kernel stack, and epoll census
   before killing it. Success bar: hours at high rate with zero wedges and
   zero DB-loop disturbances. Also add a deterministic variant: inject a
   fault into the pre-exec child path (monkeypatch one of the dup2/close
   steps to raise) and confirm it produces this specimen's signature on the
   unfixed rev and a clean `os._exit` on the fixed one.

## Detection & recovery runbook (app side, keep until the fix ships)

Detect (run on the host; a hit is this bug). A wedged child shares the
worker's cmdline, but so do uwsgi's legitimate master->worker children — the
discriminator is DEPTH: a real worker's parent is the master and its
GRANDparent is the shell wrapper, while a wedged pre-exec child has uwsgi
processes as both parent and grandparent. (Verified against the live
specimen: flags 3537247/3537248, flags nothing on a healthy host.)

```
for p in $(pgrep -x uwsgi); do
  pp=$(ps -o ppid= -p $p | tr -d ' '); gp=$(ps -o ppid= -p $pp 2>/dev/null | tr -d ' ')
  [ "$(ps -o comm= -p $pp 2>/dev/null)" = uwsgi ] && [ "$(ps -o comm= -p $gp 2>/dev/null)" = uwsgi ] &&
    echo "WEDGED pre-exec child $p (of worker $pp, age $(ps -o etime= -p $p))"
done
```

Symptoms in the app when a child has poisoned the parent: sweep starts
without completions in `stream.log` (`resetting_stale_jobs` with no matching
`reset_stale_jobs`), `job_pulse_lost` every minute for a job whose row is
already back in `do`, uwsgi `async queue is full`, HTTP fine but every
PG-touching request hanging. Recovery: restart the container (the wedged
children die with it); the drained-out job row resumes on the next claim.
Staging was recovered this way right after this evidence was captured
(2026-07-29 ~22:00 UTC). Production (same image, booted 18:53) was checked
clean at 21:50; run the detector there until the fix lands.

## References

- Pin under test: https://github.com/ddorian/gevent/tree/all-fixes-not-upstream
  at `a9a334b3370d6bfb30d9f3a1f002de4495438cbf` (six-fix). Prior pin:
  `05dd2f2cb2193defd16d4a563a951cabc9f403e3` (four-fix).
- Upstream context: https://github.com/gevent/gevent/issues/1865,
  https://github.com/gevent/gevent/issues/2194.
- Sibling evidence: `TASK_GEVENT_FORK_SSL_POISONING.md` (commit `26891ea77`),
  the TLS costume tally; the pipe costume's app-side tripwire is the Sentry
  signature `ffmpeg produced no output (returncode=..., pid=...)`.
- Incident rows: staging `queue_v1` id 585 (the starved encode job, video
  7488283390585572352) and id 607 (chunk 21, the row whose starvation
  surfaced the incident). App-side hardening from the same incident: the
  chunk-waiter reclaim fix (`fix(encode): a chunk waiter re-claims a row that
  fell back to do`).
- Runbook for wedged-worker triage on this runtime:
  `docs/design/self-hosting-migration.md` §12.6 (`/ops/stacks`).
