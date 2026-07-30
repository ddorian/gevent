# ffprobe/ffmpeg pipe splice: hunt log

**Status:** root cause found, reproduced on demand, fixed upstream in this checkout.
**Observed:** 2026-07-27. **Last worked:** 2026-07-29.

## 0. 2026-07-29 (later): a switch inside the forked child. Reproduced, then fixed

**One `os.register_at_fork(after_in_child=)` handler that yields to the hub is the
whole cause.** Not fd collision, not a double write, not gevent#1865.

`gevent.subprocess` forks and execs in Python, so between `fork()` and `exec()` the
child *is* a full interpreter holding a copy of every greenlet and of the hub. §8 was
right that the child branch itself cannot switch — and wrong that this settles it.
`after_in_child` handlers run **inside `posix.fork()`**, before the child branch
begins. gevent does not control what an application registers there, and under
monkey-patching the ordinary contents of such a handler yield: `Thread.start()` waits
on an `Event`, a patched lock may be held, `logging` takes one.

When one does, the child's hub runs the child's copies of the other greenlets. Those
copies are mid-spawn and still hold a pipe write end — and by then the child has
`dup2`'d it onto fd 1. They fork and exec again onto that same pipe. Hence complete
child lifetimes, hence both pipes doubling by the same factor, hence powers of two.

Measured directly, each child stamping its own identity into its output:

```
worker pid: 4086067
  --- one capture, 2 records:
      pid=4086084 ppid=4086067 fd1=pipe:[136448597]     <- the legitimate child
      pid=4086089 ppid=4086084 fd1=pipe:[136448597]     <- its child, same pipe inode
  --- one capture, 4 records:
      pid=4086083 ppid=4086067 fd1=pipe:[136448593]
      pid=4086085 ppid=4086083 fd1=pipe:[136448593]
      pid=4086087 ppid=4086083 fd1=pipe:[136448593]
      pid=4086088 ppid=4086085 fd1=pipe:[136448593]
```

Every extra writer is a descendant of the legitimate child and holds the *same pipe
inode* on fd 1. That is the splice, in one screen.

### 0.1 Necessity, measured (200 captures per cell, CPython 3.14.6, app venv)

| gevent#1865 wiring | child hook that yields | spliced | distinct forking pids |
|---|---|---|---|
| unfixed | yes | 96 / 200 | 6 |
| unfixed | no  | **0 / 200** | 1 |
| fixed   | yes | 101 / 200 | 148 |
| fixed   | no  | **0 / 200** | 1 |

The yielding hook is necessary and sufficient. gevent#1865 is neither: it parks a
greenlet inside `os.fork()` (measured, 3 of 15 forks began with
`_global_shutdown_lock` held) but it *serialises* forks, which damps the cascade —
the fixed-gevent row is the worse one.

### 0.2 Why staging and never local

`config.py` sets `UPTRACE_DSN=""` for local and testing; staging hardcodes it, and
`init_file.py` imports the OTel instrumentors and installs the TracerProvider only if
that DSN is truthy. So staging's process carries OTel's `after_in_child` handlers and
every local rig carried none. That is the structural ingredient §7.1 was hunting: not
Docker, not CPU starvation, not job shape.

`init_file.py` already neuters `BatchProcessor._at_fork_reinit` and
`PeriodicExportingMetricReader._at_fork_reinit` because they start a thread on fork
(that patch does survive OTel's `WeakMethod`, checked). Those are 2 of 8
`after_in_child` registrations in the venv; the rest are `otel sdk/trace:257`,
`sdk/trace:1361`, `sdk/_logs:854`, `sdk/metrics:540`, `sentry_sdk/_batcher:43`,
`_span_batcher:70`, `filelock/_read_write:776`. **Which one yields on staging is not
yet proven** — that needs the real process, and the detector below is for that.

### 0.3 Artifacts

- `gevent_fork_child_switch_repro.py` (gevent repo root) — minimal, self-contained.
  Three modes; `control` must come back clean. 2 greenlets x 10 spawns reproduced
  5 runs out of 5.
- Fix: `src/gevent/subprocess.py`, `_fork_only_outside_a_forked_child`. A child
  between `fork()` and `exec()` refuses to fork. Keyed on pid so the parent's own
  fork window is unaffected.
- Regression test: `src/gevent/tests/test__subprocess_fork_child_switch.py`. Fails on
  unfixed gevent with the multiples in the diff, passes 5/5 on fixed.
- A first attempt that did **not** work is worth recording: a `greenlet.settrace`
  hook refusing the switch. greenlet *disables a trace function that raises*, so the
  first refusal disarms the guard and everything after it proceeds. Prevention of the
  switch is not available; only prevention of its consequence.

### 0.4 The wider bug underneath — reproduced, NOT fixed (two attempts)

The splice is a sharp edge on a blunter problem: **a forked child runs copies of the
parent's greenlets, and they do real work.** No subprocess needed.
`gevent_fork_child_greenlet_repro.py` forks 20 times from a process with 4 worker
greenlets; each child does nothing but `os._exit(0)`:

```
work done by children: 80 lines from 20 of 20 forks     # yielding handler
work done by children: 0 lines from 0 of 20 forks       # control
```

Every line was written inside `os.fork()`, before it returned to anyone. In an
application those are a second INSERT, a second POST, a second publish.

gevent documents this (`gevent/os.py`: greenlets "scheduled in the hub of the forking
thread in the parent remain scheduled in the child ... may change is a subsequent
major release") and `_ForkHooks._stop_running_greenlets_in_child` is a partial
mitigation that says so itself: "If gevent is still waiting to switch to them, that
will still happen".

**Status: fixed 2026-07-29** (see the correction to attempt 2 below, and
`TASK_GEVENT_FORK_SSL_POISONING.md`). Two attempts at the time, both in
`_ForkHooks.after_fork_in_child` (which runs first among the child's handlers, so it
can act before a third-party one yields):

1. `hub.loop._callbacks.clear()` — **does nothing at all**. `clear` on the C loop's
   `CallbackFIFO` is a `cdef` method, invisible to Python, so the call raises
   `AttributeError`, and an exception inside a fork handler is reported as
   *unraisable*, i.e. swallowed. The hook ran and the fix could not execute.
   **A fix that cannot run looks exactly like a fix that does not work.** Rule 1
   applies to fixes, not only to instruments.
2. `loop._callbacks = type(queue)()` — replace rather than clear, works on both
   backends, and it *does* stop the copies: 80 lines becomes 0, regression test green
   3/3, and the 12-module battery clean. **It also breaks
   `test__threading_2.ThreadJoinOnShutdown.test_3_join_in_forked_from_thread`**, which
   forks from a greenlet and then uses `threading` in the child. The test hangs to its
   60s timeout. Backed out of the branch.

   ~~That child needs greenlets that were merely runnable at the fork, and discarding
   the queue strands them.~~ **Wrong, corrected 2026-07-29.** Nothing was stranded.
   `run_callback` takes an `ev_ref` per callback and `_run_callbacks` hands it back as
   it pops; a queue dropped on the floor leaks one reference per entry, so the child's
   loop can never become unreferenced and `loop.run()` never returns. Proved two ways:
   stopping each callback in place instead (`cb.stop()`, which both backends' callback
   objects support, and which lets the loop pop and unref them as usual) passes the
   test in 0.1s and still gives 0 lines; and dropping the queue *while handing back the
   matching unrefs by hand* also passes. The fix is `237110a2` on
   `fix-fork-ssl-poisoning`.

   The rule this cost: a hang is a resource-accounting symptom at least as often as a
   scheduling one. Before concluding "the child needed that", check what the structure
   you discarded was accounting for.

Dropping the queue is not, however, sufficient for the wider bug: a copy parked on a
timer or an `io` watcher is resumed by the loop itself, not by the callback queue
(measured: 28 lines from 7 of 20 forks with timer-parked workers, and the TLS
poisoning repro still 4/4 poisoned). Closing that needed the `gevent.subprocess`
pre-exec child to get a *fresh hub* — see `TASK_GEVENT_FORK_SSL_POISONING.md`, which
also carries the standalone TLS reproduction. A forked child that keeps using gevent
is still supported and gevent's own suite encodes it, so a plain `os.fork()` child
keeps the hub it inherited; that remains the semantics change `gevent/os.py` defers to
a major release.

**This is also the case for running the full suite, not a battery.** Attempt 2 passed
every targeted test aimed at it — including `test__core_fork` and
`test__threading_fork_from_dummy` — and only the full 136-module run caught it, as a
*hang* rather than a failure. See §0.6.

## 0.5 Earlier the same day: the five artifacts measured exactly

All five staging quarantine files are now local
(`scratchpad/staging-quarantine/`, copied from the s1 host bind mount).

- **Every firing is exactly two child-lifetimes on both pipes.** All four ffprobe
  firings: stdout 146,884 = 2 x 73,442 AND stderr 4,572 = 2 x 2,286 — including
  both "torn" ones, whose tearing is arrangement, not amount. The count_frames
  firing: stdout 10 = 2 x 5, stderr 4,572 = 2 x 2,286.
- **The torn firings are interleaved, not truncated.** Firing 4 decomposes as
  A-head (20,849) | B-complete (73,442) | A-tail (52,593): writer B pushed its
  whole document through the pipe while writer A was stalled mid-write. Two
  concurrent complete runs, strict pipe FIFO. Sequential retry cannot do this.
- **The stderr is mise shim output.** `ffprobe` is a bare argv[0] resolved via
  mise shims on PATH; the 2,286 clean bytes are the shim's `mise WARN` resolution
  lines. The doubling therefore duplicates the ENTIRE child lifetime
  (shim + tool), not bytes of one stream — a per-process phenomenon, not a
  per-pipe one. (Shim execs in place — verified comm transitions instantly, no
  intermediate process — and the incident image already had MISE_OFFLINE=1 /
  MISE_EXEC_AUTO_INSTALL=0, so the shim is not itself a forker here.)
- **The fd-collision family is dead.** If a second spawn's write ends were
  cross-wired into the victim's pipes, that spawn's own capture would read EOF
  empty and the tripwire would quarantine `b''` ("Expecting value: char 0").
  No empty-capture quarantine exists next to any firing.

**Conclusion: one spawn produced two child processes** (either two forks for one
`_execute_child`, or a pre-exec child forking again). The corrected fork census
is the arbiter: a grandchild appears as `CHILD-OF <non-worker-pid>`.

Repro rig (running): container `fr-worker` (staging image e5e3313ea, uwsgi +
pg-queue + vector, host net) with the live tree bind-mounted; `ForkReproJob`
storms self-requeue; census + ledger + tripwire armed; CPU-throttled to 6 to
recreate s1 starvation. Fixed this session: `_make_src` wrote srt tracks into
.mp4 (mkv now), and `_install_fork_sentinel` used to REPLACE the ledger census
on `gevent.subprocess.fork` (blind instrument #4) — it now chains it.

One pipe capture holds N complete copies of a process's output. Two surfaced symptoms:

- `SeamError: frame conservation failed`, raised by `Converter.assert_seam_integrity`
  when a spliced `count_frames` result makes the chunk audit disagree with the assembled
  output.
- `ValueError: invalid literal for int() with base 10: b'5755\n5755'`, raised by
  `Converter.count_frames` parsing its own capture.

Nothing here is a fix. No fix work was authorised and none was started.

---

## 0.6 The battery is not the suite

Both fixes were checked the same way at first: run the modules that look related.
For the subprocess fix that was adequate; for the fork-child fix (§0.4, attempt 2) it
was not, and the difference is worth stating because the battery *looked* more
thorough than it was.

- The battery was 12 modules chosen for relevance — `test__core_fork`,
  `test__threading_fork_from_dummy`, `test__subprocess*`, `test__threading*`,
  `test__monkey*`. All green. `test__threading_2` was **not** among them; nothing in
  its name says fork.
- The full run is 136 modules in 62 jobs and takes **1m40s** on this machine. Twelve
  modules take about 25s. The battery bought nothing but the four minutes of not
  writing the baseline down.
- It surfaced as a **hang**, not a failure: `EXIT=124` at the 1-hour cap, no test
  named, no traceback. A run that never ends reports nothing, so the timeout has to
  be low enough to leave you a corpse to read. `timeout 900` on the re-run gave one.
- Diffing needs a baseline from the *same bed*: master's 62 jobs here fail 2 for
  environmental reasons (a clock-sensitive assertion in `test__subprocess`, and the
  socket/DNS job timing out with no resolver in the sandbox). Without that list, "2
  failures" reads as a regression.
- Bisecting across a merge branch is cheap when each fix has its own bed: four beds
  (broken / subprocess-only / threading-only / combined), one run each, named the
  culprit in one pass.

Final state of the branch, all four fixes minus the reverted one:
`2/62 failed in 01:40`, both environmental, identical to the baseline, and
`test__subprocess_fork_child_switch` green in 0.4s.

---

## 1. Environment

Staging and the local project venv are byte-identical on the relevant modules (matching
md5 for `gevent/subprocess.py` = `aa972b2b1ded6b72a9ac95f010630905` and
`gevent/_fileobjectposix.py` = `b1e863531e9b032ebc46abf6e0c95569`).

```
gevent 26.7.1.dev0 | greenlet 3.5.4 | CPython 3.14.6
ffprobe/ffmpeg n8.1.1-20260509
stdlib _USE_POSIX_SPAWN=True   _USE_VFORK=ABSENT
gevent forces _USE_POSIX_SPAWN=False, so gevent always takes its own fork/exec path
```

**Trap:** `uv run --no-project` with PEP 723 inline metadata resolves a *different* gevent
(26.7.0). Roughly 38,000 captures were once run on the wrong build. Use
`uv run python <script>` so the project venv is used, and read the banner.

Staging runs the queue under Docker (Kamal). Job groups are greenlet pools, not processes:
`FULL` (1 worker, heavy encode), `SINGLE_CORE` (5 workers, includes probes and shot
detection), `CHEAP`. PgQueue is `MyGeventPool`/`TrackAllGeventPool`, so the 5 `SINGLE_CORE`
workers are **five greenlets in one process**, which is why concurrent probes share an fd
table at all.

---

## 2. The defect, as measured

Clean ffprobe JSON for the incident file is 73,442 bytes on staging (73,401 locally, the
delta being the shorter source path). Captures observed at 2x and 3x. The `count_frames`
variant is 5 bytes clean (`5755\n`), 10 corrupt (`5755\n5755\n`).

### 2.1 Quarantined firings

Five files, on staging at `/srv/heapstream/debug/data/logs/app/probe_quarantine/`. This is
a **host bind mount** (`/srv/heapstream/debug/data` -> `/home/stream_user/.data`), so they
survived the debug container's removal and are still there.

| file | failure | offset / size |
|---|---|---|
| `1785222304.285-pid4796.txt` | (earliest, 165,111 b artifact) | not analysed in detail |
| `1785222874.659-pid54368.txt` | `Extra data: line 2064 column 1` | char 73442, clean document boundary |
| `1785225864.524-pid505908.txt` | `Expecting ',' delimiter: line 2062 column 6` | char 73439, torn 3 bytes early |
| `1785227754.888-pid506827.txt` | `Expecting ',' delimiter: line 586 column 10` | char 20849, torn well inside |
| `1785232079.790-pid508771.txt` | `ValueError ... b'5755\n5755'` | 10 bytes |

Each firing is a different asset. Firing 2 and 3 carry near-sequential snowflake ids
(`7487589265833808896`, `7487589462088769536`), i.e. two near-simultaneous uploads of what
is very likely the same source, which is why both produce a 73,442-byte document.

### 2.2 Constraints any theory must satisfy

- **The whole set doubles.** Firing 2 carried stdout 146,884 (2 x 73,442) *and* stderr
  4,572 (2 x 2,286). Not one pipe of a spawn, both.
- **Both documents name the same input file.** Verified directly on firing 2:
  `grep -o '"filename": "[^"]*"'` returns exactly two occurrences of the same
  `asset.7487589265833808896.work/original.mkv`. This is **not** cross-job contamination
  between different assets, and it is not two different files that happen to be equal size.
- **Both clean concatenation and mid-document tearing occur** (offsets above). Not purely
  sequential appending, not purely interleaved.
- **Exact integer multiples**, 2x and 3x, never a fraction.
- **No extra pipes were allocated** for the doubled set.
- `count_frames` passes `-select_streams v:0` and **no timeout**, which rules out "two
  video streams matched" and "timeout retry" for firing 5.
- Firing 2 recorded **two live children** at the moment of failure
  (`live children by tid` -> `tid 54368: 54435 54444`). Ambiguous (an encode ffmpeg is
  also live) but unexplained.

### 2.3 Base rate

Under the untraced pipe path the tripwire fired **about once per 35 probes**. Under a
tracing spawn it fired **0 times in 114 probes** (see 5.1). On staging the standalone
`dupwatch` sampler recorded **96 sightings, 0 duplicate ffprobe processes**, though its
polling interval was coarse relative to probe lifetime, so absence there is weak.

That base rate matters for estimating a repro: ~185,000 clean local captures is far past
1-in-35, so whatever the missing ingredient is, it is *structural*, not a matter of volume.

---

## 3. Code map

Point at symbols, not line numbers.

- `Converter.count_frames` — builds the `-count_packets` args, calls `spawn_check(args)`
  with **no timeout**, parses `proc.stdout.strip()` with `int()`. This is where firing 5
  surfaced. **No healing path**: the quarantine is purely forensic and the failure is
  re-raised unchanged.
- `Converter.probe` — uses `probe_cmd` (in `util/converter/ffmpeg.py`, note this does *not*
  go through `ffprobe_path_options`), passes `timeout=probe_timeout_seconds` (60).
  Under the tripwire it tries the pipe path first, quarantines a corrupt capture, then
  **heals** via `output_via_files=True`.
- `Converter.spawn_check` — the single spawn helper. Pipe branch is
  `subprocess.run(cmds, capture_output=True, check=True, timeout=timeout)`. File branch
  (`output_via_files=True`) uses `tempfile.TemporaryFile` for stdout/stderr.
- `Converter._quarantine_probe_capture` — writes to `.data/logs/app/probe_quarantine`.
- `install_pipe_ledger` — all hunt instrumentation (see section 5).
- `Converter.probe_pipe_tripwire` — wired from `Config.PROBE_PIPE_TRIPWIRE`, staging only.
- `Converter._spawn` — the streaming encode path, deliberately unbounded, uses
  `_StallWatchdog`. Not a probe path but runs concurrently with probes.

### 3.1 The exact commands

`count_frames` (firing 5), via `ffprobe_path_options`:

```
ffprobe -hide_banner -v error -select_streams v:0 -count_packets \
        -show_entries stream=nb_read_packets -of csv=p=0 <chunk>
```

`probe` (firings 2-4), via `probe_cmd`:

```
ffprobe -hide_banner -loglevel error -print_format json -show_format -show_streams <file>
```

**Incidental:** `ffprobe_path_options` has **no `-nostdin`** while `ffmpeg_path_options`
does, so probes inherit the parent's stdin. Not implicated in the splice, but it is an
inconsistency worth knowing.

---

## 4. What is still open

**Answered** (§0): a second process obtained the write end, and it was a *descendant of
this spawn's own child*, forked while that child sat between `fork()` and `exec()`.
Both halves of the older "narrowed it to" claim were void: the fork half rested on a
blind instrument (5.1), the fd-collision half was a misread label (6.2). The conclusion
in §0.5 — "one spawn produced two child processes" — was right, and the grandchild
branch of it was the right one.

Still open: **which** of staging's eight `after_in_child` handlers yields (§0.2). The
mechanism does not depend on the answer; the remediation on our side does.

---

## 5. The thing that cost the most: three blind instruments

**gevent binds raw builtins at import, so patching the `os` module never reaches its
internals.** Three wrappers in `install_pipe_ledger` were attached to functions gevent
never calls. Every "no fork happened" and "no close happened" reading taken through them
was a wrapper that could not fire, not an absence.

| ledger wrapped | what gevent actually calls | fired |
|---|---|---|
| `os.fork` | `gevent.subprocess.fork`, the raw `posix.fork` builtin | never |
| `os.close` | `gevent.subprocess.os_close`, the raw `close` builtin | never |
| child's line appended to `_PROBE_PIPE_TRACE` | a heap list, erased by `exec` | never readable |

Three layers exist and only the first is on the spawn path:

- `gevent.subprocess.fork` is `<built-in function fork>` from `posix`, bound at import
  before monkey-patching. `_execute_child` passes it to `fork_and_watch`.
- `gevent.os._raw_fork = os.fork` is also captured at import; `fork_gevent` uses it.
  Not on the subprocess path.
- `os.fork` after `monkey.patch_all()` is `gevent.os.fork`, which subprocess never calls.

### 5.1 Verifying an instrument is not blind

Never trust a zero without this. Run before believing any absence:

```python
from gevent import monkey; monkey.patch_all()
import os, sys, subprocess, gevent
events = []
real = os.fork
def traced():
    pid = real(); events.append(pid); return pid
os.fork = traced
gevent.joinall([gevent.spawn(subprocess.run, [sys.executable,'-c','pass'],
                             capture_output=True) for _ in range(5)])
print('fired:', len(events))     # 0 == blind
```

Or simply assert identity: `gevent.subprocess.os_close is os.close` -> `False`.

**Also verify a detector can fire**, by inducing the event it looks for. The collision
detector was validated by marking an fd owned, closing it, and allocating a pipe.

### 5.2 The corrected instruments (now in `install_pipe_ledger`)

Fork census. The child must write to a **file** with `O_APPEND`; a forked copy's own
testimony is the only thing that settles whether a second process exists, and it cannot
survive `exec` in a heap list. Output goes to `.data/logs/app/probe_fork_census.log`.

```python
from gevent import subprocess as gevent_subprocess
real_fork = gevent_subprocess.fork
def fork() -> int:
    parent_pid = os.getpid()
    pid = real_fork()
    if pid == 0:
        os.write(census_fd, f"... p={os.getpid()} CHILD-OF {parent_pid}\n".encode())
    else:
        os.write(census_fd, f"... p={parent_pid} FORKED {pid}\n".encode())
    return pid
gevent_subprocess.fork = fork
```

Close tracing, resolving the fd **before** the close so each line says what was released:

```python
real_os_close = gevent_subprocess.os_close
def traced_os_close(fd: int) -> None:
    _PROBE_PIPE_TRACE.append(f"... close {fd} {target(fd)}")
    real_os_close(fd)
gevent_subprocess.os_close = traced_os_close
```

Verified: 6 spawns -> 6 `FORKED` + 6 `CHILD-OF`; 4 spawns -> 12 close lines.

A second writer must now appear either as a `CHILD-OF` line from a pid that is not the
worker, or as a close naming the inode it released.

### 5.3 Blind instrument #5: that census, as designed above

`parent_pid = os.getpid()` is read **before** the fork, and a greenlet can park inside
`os.fork()` and resume in a different process. The pid it captured is then a lie, and
every child reports the worker as its parent whatever actually forked it.

Measured on one run of the repro workload, from the same set of children:

| parent read from | distinct parents |
|---|---|
| `os.getpid()` captured before the fork (as in 5.2) | **1** — "no grandchildren, clean" |
| `os.getppid()` in the child | **141** |

The arbiter this hunt was about to trust would have cleared the true cause. Fix is one
call: `os.getppid()`. This is rule 3 a second time, on the instrument built to escape
rule 3.

Detector for staging, arm it immediately after `patch_all_fast()` and **before**
anything else imports (handler order is registration order): a `greenlet.settrace`
counter armed from an `after_in_child` hook, logging any switch that happens inside a
forked child. Validated both ways — 3604 switch lines and 105 grandchild ppids while
splicing, 0 and 1 on the control.

---

## 6. Conclusions reported and then retracted

Both were confident, and both failed the same way: reading an instrument's *label* instead
of what it records.

### 6.1 "One child, two writes, zero forks, complete coverage"

Rested entirely on the blind fork wrapper. That trace had no fork coverage at all.

### 6.2 "A premature close of a live spawn's fds"

From firing 5 I read fds 49, 56 and 58 being reissued to a new pipe 49ms before their
owning spawn forked, and called it hard evidence:

```
.6817 ccc0 pipe -> r=48 w=49 pipe:[8280069]
.6820 ccc0 spawn fds[48,49,54,56]
.6963 eb60 pipe -> r=49 w=56 pipe:[8280072]     <- read as a collision
.7314 ccc0 fork pid=703260 stdout=pipe:[8280069]
```

The `fork pid=` line is logged **after `_execute_child` returns**, which is after the fork,
after the parent closes `c2pwrite`/`errwrite`, and after it waits on `errpipe_read` for the
child to exec. Reproducing the instrument shape locally shows the identical ordering as
ordinary behaviour:

```
spawn-entry fds=[6,7,8,9]
ACTUAL-FORK pid=634530
pipe r=7 w=9                       <- freed numbers reissued, legitimately
spawn-entry fds=[7,9,11,12]
execute_child-RETURNED pid=634530  <- much later
```

Those fds were freed legitimately. No 49ms window, no collision. The line is now labelled
`spawn-returned`. The prior note in the ledger had warned about exactly this trap.

---

## 7. Everything tried

Roughly **185,000 real-ffprobe captures** this session, plus ~66,000 earlier captures with
Python children. Zero splices throughout.

| angle | scale | result |
|---|---|---|
| Real ffprobe, real incident file, concurrent encoders | 77,296 captures | 0 |
| Same plus real OS threads (gevent threadpool) during forks | 64,000 captures | 0 |
| Valid fork census | 67,022 forks | 1 forking pid, 0 grandchildren |
| Greenlet kills at a fixed point per round | 20,000 captures / 60,000 spawns | 0 |
| Kills injected *inside* another greenlet's spawn window | 20,512 captures / 30,768 spawns | 0 |
| GC pressure forcing late finalisation | included above | 0 |
| **Chunk-rate probes (13ms, 12x spawn rate)** | 9,600 captures / 14,400 spawns | 0 |
| Plain OS threads, no gevent, C `fork_exec` | 3,216 captures | 0 |
| `close_fds` leaking an in-flight pipe into a child | direct test | does not happen |
| Child yielding to the hub between fork and exec | source analysis | impossible |
| Any other fork source in the app | repo grep | none outside converter |
| ffprobe duplicating output on a partial write | direct test | no, it truncates |

Earlier sessions additionally refuted: stale-close, mis-bound child at fork, process fork,
gevent version difference, and four reader-side theories.

### 7.1 Why volume did not help

The failure needs an ingredient none of these had. Candidates not yet replicable locally:
the full application (Redis/PG/ClickHouse/boto3 socket churn, SQLite disk cache), Docker,
and real seam-assembly job structure rather than a synthetic burst.

**Answered** (§0.2): none of those. The ingredient was a registered
`os.register_at_fork(after_in_child=)` handler that yields, present on staging because
`UPTRACE_DSN` is set there and absent locally because it is empty. Every rig above ran
with an empty fork-handler set, so no amount of volume could have found it. With one
such handler registered, 2 greenlets and 20 spawns find it 5 times out of 5.

---

## 8. Side findings worth keeping

- **ffprobe silently loses output on a non-blocking stdout and still exits 0.** Measured:
  73,401 bytes clean vs 53,318 (0.726) with `O_NONBLOCK` on the write end, return code 0
  both times. Truncation, never duplication, which kills the write-retry theory. The
  silent-success-on-short-write behaviour matters independently.
- **The reentrant close leaks fds and GC never reclaims them.** 12 killed spawns leak 9
  pipe fds; `gc.collect()` closes none. A leaked write end means the reader never sees EOF,
  a clean mechanism for the **wedge** symptom, separate from the splice. Leaked fds are
  never reissued, so they cannot collide.
- **gevent's POSIX `_execute_child` is a pure-Python fork/exec.** The child runs ordinary
  Python between `os.fork()` and `exec`. `fork_and_watch` receives the raw `posix.fork`, so
  the child gets no `reinit`, and the child branch calls only unpatched syscalls, so it
  cannot switch greenlets. ~~A copied greenlet can never exec in the forked child.~~
  **That last sentence was the error that hid this bug for the whole hunt.** The child
  branch cannot switch; the `after_in_child` handlers that run *inside `posix.fork()`,
  before the child branch* can, and a copied greenlet then execs in the forked child
  routinely. See §0.
- **`Popen.__exit__` closes stdout/stderr unguarded** while `communicate()` guards the
  identical close against `RuntimeError: reentrant call` and names the failure in its own
  comment. Separate upstream gevent bug. Standalone repro at
  `gevent_popen_exit_reentrant_repro.py` (untracked, repo root, 130 lines, 10/10 at its
  measured floor of `ATTEMPTS=2, CONCURRENCY=8, settle 0.15`).
- **The "Close pipe fds" block in gevent's child branch is dead code** (`if not True:`).
  `close_fds` is handled later by `self._close_fds`, which does work correctly.

### 8.1 Instrumentation hazard

A tracing spawn that read one pipe by hand **deleted the defect**: 114 traced probes under
the tripwire fired zero times where the untraced path had been firing about once per 35.
Instrument *below* the mechanism, never through it. This is why the ledger observes
`os.pipe`, the raw `__read`, the fork and the close, rather than replacing any reader.

---

## 9. Harnesses

The scratchpad is **session-scoped and will be deleted**, so the designs are recorded here
rather than the paths. All take the real ffprobe binary and validate every capture.

- `real_hunt.py` — real ffprobe + long-lived ffmpeg encoders holding `stderr=PIPE`.
- `real_hunt2.py` — adds the fork census; asserts `forks_from_a_child == 0`.
- `real_hunt3.py` — adds `hub.threadpool` churners doing blocking file I/O in real threads.
- `real_hunt4.py` — collision detector. Registers each spawn's fds at `_execute_child`
  entry and **deregisters at its own fork** (via the `gevent.subprocess.fork` wrapper), then
  flags any `os.pipe()` returning a still-owned fd. Deregistering at *return* instead
  produces 30 false positives per 16 spawns, because the parent legitimately frees
  `c2pwrite`/`errwrite` between fork and return.
- `real_hunt5.py` — adds greenlets calling `gc.collect()` continuously.
- `real_hunt6.py` — fires kills from **inside** `_execute_child`, guaranteeing they land
  while another greenlet sits between its `os.pipe()` and its fork.
- `real_hunt7.py` — same against an encode chunk for the 12x spawn rate.
- `thread_hunt.py` — the no-gevent control, `ThreadPoolExecutor` + C `fork_exec`.

Chunk fixture: `ffmpeg -i <src> -t 4 -c:v libx264 -preset ultrafast -an chunk.mp4`
(64 KB, 96 packets, 13ms per probe).

---

## 10. Mitigation and fix

**Upstream fix, done in this checkout** (`src/gevent/subprocess.py`): a child between
`fork()` and `exec()` refuses to fork, so a copied greenlet cannot exec a second time
onto the spawn's pipes. Keyed on pid rather than a flag, so the parent's own fork
window — where a greenlet may legitimately be parked, gevent#1865 — is unaffected.
Regression test alongside it. Verified: repro 0/200 spliced in all three modes and no
grandchildren; `test__subprocess`, `test__os`, `test__monkey_fork_atomic` unchanged
(`test__subprocess` has one pre-existing timing failure,
`test_run_with_shell_timeout_and_capture_output`, identical before and after).

The fix stops the duplicate exec. It does **not** stop a copied greenlet from running
in the child at all, so a copy could still touch inherited fds. Closing that needs the
child to run no greenlets whatsoever, which is a larger change — gevent's own
`gevent/os.py` docstring already flags the behaviour as one that "may change in a
subsequent major release".

**App side, still not started, still not authorised.** `spawn_check(...,
output_via_files=True)` already exists and `probe()` uses it on the non-tripwire path.
A child writing to its own file descriptor cannot be handed another child's bytes, so
the class is gone regardless. **`count_frames` is the last caller still on pipes.**
Independently, finding which staging handler yields (§0.2) is worth doing: a handler
that yields at fork is broken under gevent whatever gevent does about it.

---

## 11. State of the tree and staging

Uncommitted, all hunt-only:

- `util/converter/__init__.py` — `install_pipe_ledger` with the corrected fork census,
  corrected close tracing, the `spawn-returned` relabel, the pipe/read ledger, and the
  `probe_pipe_tripwire` path in `probe`/`count_frames`. **The instrument corrections are
  the part worth keeping**; the old wrappers recorded nothing and would silently waste the
  next hunt.
- `webapp/fork_repro.py`, `QueueName.FORK_REPRO`, `ForkReproJob`, and a `config.py` change.
- `gevent_popen_exit_reentrant_repro.py`, untracked, repo root. Documents the upstream
  gevent issue, not this bug. Whether to track it is undecided.

The corrected ledger was **never deployed to staging**.

In the **gevent** checkout (`../gevent`), all new and untracked:

- `src/gevent/subprocess.py` — the fix (`_fork_only_outside_a_forked_child`) and its
  call site in `_execute_child`.
- `src/gevent/tests/test__subprocess_fork_child_switch.py` — regression test.
- `docs/changes/+fork-child-switch.bugfix` — towncrier fragment.
- `gevent_fork_child_switch_repro.py`, `gevent_fork_child_greenlet_repro.py` — standalone repros, repo root.

Note there is a second, **stale** copy of this document at `../stream/`
(`Last worked: 2026-07-28`, missing §0). This one is canonical.

Staging cleanup done at end of session: `hs-debug` container stopped and removed, both
`/root/hunt_watch.sh` watchers killed. The five quarantine artifacts survive on the host
bind mount. Seven stopped `heapstream-web-*` containers from prior Kamal deploys remain
and were left alone.

---

## 12. Rules this hunt paid for

1. Before believing any zero, prove the instrument can fire. Three wrappers here could not.
2. gevent binds raw builtins at import. Patching the `os` module never reaches its
   internals. Check `x is os.close`.
3. A log line's label is not its meaning. `fork pid=` was logged after `_execute_child`
   returned, and reading it as the fork produced a confident, wrong conclusion.
4. Instrument below the mechanism. Tracing through it deleted the defect outright.
5. Vary the *shape* before the volume. Spawn rate, child duration, and input size were
   each worth more than another 50,000 captures at the same shape. The shape that
   mattered here was not in the spawn at all — it was the process's set of registered
   fork handlers, which every local rig had empty.
6. Diff the environments on what the *fix* would touch, not on what the *bug* seems to
   touch. §1 established staging == local by md5 of `subprocess.py` and
   `_fileobjectposix.py`. Both matched, and both were irrelevant.
7. Make the child say who its parent is, not the parent say who its child is. A
   greenlet can park inside `os.fork()` and resume in another process, so anything the
   parent recorded before the fork may describe a different process than the one that
   forked (§5.3).
8. When a fix does not work, measure before redesigning. The first fix here refused the
   switch with `greenlet.settrace`; it armed, it fired, and it changed nothing, because
   greenlet disables a trace function that raises. Stamping pid/ppid/fd1 into the child's
   own output found the real chain in one run.
9. Price the full suite before settling for a battery. Here it was 1m40s against 25s,
   and the battery — 12 modules picked for relevance — missed the regression that the
   full run caught. Take a baseline on the same bed first, and cap the run low enough
   that a hang leaves a readable corpse (§0.6).
