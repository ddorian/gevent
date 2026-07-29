# /// script
# requires-python = ">=3.14"
# dependencies = ["gevent>=26.7"]
# ///
"""A greenlet switch inside a forked child makes one subprocess spawn exec N times.

    uv run gevent_fork_child_switch_repro.py           # reproduces, exits 1
    uv run gevent_fork_child_switch_repro.py thread    # same, via Thread.start() (OTel's shape)
    uv run gevent_fork_child_switch_repro.py control   # no fork hook: clean, exits 0

Read the banner: `uv run` resolves the PEP 723 dependency above, which is not necessarily
the gevent you meant to test. To pin one, run it with that interpreter directly
(`path/to/.venv/bin/python gevent_fork_child_switch_repro.py`).

Each spliced capture holds an exact integer multiple of one child's output -- on stdout AND
stderr at once, same multiple on both, never a fraction. No extra pipes are allocated.

WHY IT HAPPENS

gevent's POSIX Popen._execute_child is a pure-Python fork/exec: the child runs ordinary
Python between fork() and exec() (it never uses _posixsubprocess.fork_exec). That child
branch is careful -- it calls only unpatched syscalls, so it cannot switch greenlets.

But os.register_at_fork(after_in_child=) handlers run *inside posix.fork() itself*, before
that child branch begins. gevent does not control what an application registers there, and
under monkey-patching the most ordinary things yield: gevent.sleep(0), Thread.start()
(-> Event.wait()), a contended patched lock, logging.

When such a handler yields, the child's copy of the hub runs the child's copies of the other
greenlets -- which are sitting mid-subprocess.run() -- and they fork and exec again. Each
copy still holds the *original* spawn's c2pwrite/errwrite, because the parent closes the
write ends only after fork() returns (subprocess.py, "Parent" block). So a second, third,
fourth process exec the same argv onto the first spawn's pipes. Hence powers of two, and
hence the identical multiple on both pipes.

Two greenlets and one yielding handler are enough. A single greenlet never overlaps and
stays clean.

MEASURED, this script (CPython 3.14.6, gevent 26.7.1.dev0)

    GREENLETS=2 SPAWNS=10, sleep   -> 1-2 spliced / 20, 5 runs out of 5
    default 5 x 40, sleep          -> 13, 17 spliced / 200
    default 5 x 40, thread         -> 6, 11 spliced / 200
    default 5 x 40, control        -> 0 spliced, and 1 distinct ppid

Splice multiples observed: 2, 4, 8, 16 -- always identical on stdout and stderr.

READING THE FORK CENSUS

The census below takes the parent pid from os.getppid() *in the child*, not from an
os.getpid() captured before the fork. That distinction is not cosmetic: a greenlet can park
inside os.fork() and resume in a different process, so a pre-captured pid is a lie. Measured
on a heavier variant of this workload while developing the repro, the same set of children
reported 1 distinct parent read from a pre-captured pid, and 141 read from getppid().
"""

from gevent import monkey; monkey.patch_all()

import os
import sys
import tempfile
import threading

import gevent
import subprocess
from gevent import subprocess as gevent_subprocess

MODE = sys.argv[1] if len(sys.argv) > 1 else "sleep"
GREENLETS = int(os.environ.get("GREENLETS", "5"))
SPAWNS = int(os.environ.get("SPAWNS", "40"))

STDOUT_LEN, STDERR_LEN = 5000, 300
CHILD = "import sys;sys.stdout.write('A'*%d);sys.stderr.write('B'*%d)" % (
    STDOUT_LEN, STDERR_LEN)


def _yields_to_the_hub():
    # Anything cooperative does. `thread` is OpenTelemetry's BatchProcessor._at_fork_reinit
    # shape (restart a worker thread in the child); under gevent Thread.start() waits on an
    # Event, which is the same yield.
    if MODE == "thread":
        threading.Thread(target=lambda: None, daemon=True).start()
    else:
        gevent.sleep(0)


if MODE != "control":
    os.register_at_fork(after_in_child=_yields_to_the_hub)

# The child's testimony has to go to a file with O_APPEND. A heap object cannot carry it:
# the child is a separate process and then execs, so anything it appends to a set in memory
# is invisible to the parent and gone at exec.
_census_path = os.path.join(tempfile.gettempdir(), "gevent-fork-census-%d" % os.getpid())
_census_fd = os.open(_census_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

_real_fork = gevent_subprocess.fork


def _fork():
    pid = _real_fork()
    if pid == 0:
        os.write(_census_fd, b"%d\n" % os.getppid())
    return pid


gevent_subprocess.fork = _fork

spliced = []
captures = [0]


def prober():
    for _ in range(SPAWNS):
        done = subprocess.run([sys.executable, "-c", CHILD], capture_output=True)
        captures[0] += 1
        if len(done.stdout) != STDOUT_LEN or len(done.stderr) != STDERR_LEN:
            spliced.append((len(done.stdout) / STDOUT_LEN, len(done.stderr) / STDERR_LEN))


print("gevent %s | CPython %s | mode %s"
      % (gevent.__version__, ".".join(str(v) for v in sys.version_info[:3]), MODE))

gevent.joinall([gevent.spawn(prober) for _ in range(GREENLETS)], timeout=900)

os.close(_census_fd)
with open(_census_path) as census:
    parents = {int(line) for line in census if line.strip()}
os.unlink(_census_path)

print("captures            : %d" % captures[0])
print("spliced             : %d  %s" % (len(spliced), spliced[:8]))
print("distinct real ppids : %d (worker is %d; anything else is a grandchild)"
      % (len(parents), os.getpid()))

if MODE == "control":
    sys.exit(1 if spliced else 0)
if not spliced:
    print("did not reproduce this run; raise GREENLETS/SPAWNS and retry")
    sys.exit(0)
sys.exit(1)
