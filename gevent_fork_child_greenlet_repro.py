# /// script
# requires-python = ">=3.14"
# dependencies = ["gevent>=26.7"]
# ///
"""A forked child runs copies of the parent's greenlets, and they do real work.

    uv run gevent_fork_child_greenlet_repro.py          # children do work, exits 1
    YIELD=0 uv run gevent_fork_child_greenlet_repro.py  # control: clean, exits 0

No subprocess anywhere. A plain ``os.fork()``, one
``os.register_at_fork(after_in_child=)`` handler that yields, and worker greenlets
whose only job is to append a line naming the process that wrote it. The child does
nothing at all --- it calls ``os._exit(0)`` the instant ``fork()`` returns --- so
every line bearing a child's pid was written by a copy of somebody else's greenlet,
*inside* ``os.fork()``, before it returned to anyone.

In an application those lines are whatever the greenlets were about to do: a second
INSERT, a second POST, a second publish onto a queue.

WHY IT HAPPENS

Under monkey-patching the ordinary contents of an at-fork handler yield ---
``Thread.start()`` waits on an ``Event``, a patched lock may be held by another
greenlet, ``logging`` takes one. ``after_in_child`` handlers run inside ``posix.fork()``
itself, so that yield hands control to the child's copy of the hub, which resumes the
child's copies of every greenlet that was runnable at fork time.

gevent knows: ``gevent/os.py`` warns that greenlets "scheduled in the hub of the
forking thread in the parent remain scheduled in the child; compare this to how normal
threads operate", and calls that something that "may change is a subsequent major
release". ``_ForkHooks._stop_running_greenlets_in_child`` is a partial mitigation ---
it makes them *appear* stopped to :mod:`threading` and says so in its own comment:
"If gevent is still waiting to switch to them, that will still happen".

RELATION TO THE SUBPROCESS SPLICE

``gevent_fork_child_switch_repro.py`` is the same root cause with a sharper edge: there
the copies fork and exec onto the pipe fds of the spawn whose child they are running
inside, so one ``subprocess.run(capture_output=True)`` returns several complete copies
of its child's output. That consequence is fixed in ``gevent.subprocess``. This one ---
copies running at all --- is not.

MEASURED (CPython 3.14.6, gevent 26.7.1.dev0)

    default          -> 80 lines from 20 child pids, out of 20 forks
    YIELD=0          -> 0 lines, 0 pids

NOT FIXED

An attempt that did not work, recorded so it is not repeated: dropping the loop's
pending callbacks (``hub.loop._callbacks.clear()``, present on both backends) from
``_ForkHooks.after_fork_in_child``, which runs first among the child's handlers. The
hook runs and the queue is cleared --- verified --- and the copies still run. Whatever
resumes them is some other path.
"""

from gevent import monkey; monkey.patch_all()

import os
import sys
import tempfile

import gevent

YIELD = os.environ.get("YIELD", "1") == "1"
WORKERS = int(os.environ.get("WORKERS", "4"))
FORKS = int(os.environ.get("FORKS", "20"))

LOG = os.path.join(tempfile.gettempdir(), "gevent-fork-child-work-%d" % os.getpid())
fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
PARENT = os.getpid()

if YIELD:
    # The least a handler can do and still yield.
    os.register_at_fork(after_in_child=lambda: gevent.sleep(0))

stop = [False]


def worker(n):
    i = 0
    while not stop[0]:
        # Stands in for the application's side effect.
        os.write(fd, b"worker=%d i=%d pid=%d\n" % (n, i, os.getpid()))
        i += 1
        gevent.sleep(0)


print("gevent %s | CPython %s | yielding at-fork child handler: %s"
      % (gevent.__version__,
         ".".join(str(v) for v in sys.version_info[:3]),
         YIELD))

workers = [gevent.spawn(worker, n) for n in range(WORKERS)]
gevent.sleep(0.05)

for _ in range(FORKS):
    pid = os.fork()
    if pid == 0:
        # Do nothing whatsoever, and leave.
        os._exit(0)
    os.waitpid(pid, 0)
    gevent.sleep(0.01)

stop[0] = True
gevent.joinall(workers, timeout=5)
os.close(fd)

counts = {}
with open(LOG) as log:
    for line in log:
        pid = int(line.rsplit("pid=", 1)[1])
        counts[pid] = counts.get(pid, 0) + 1
os.unlink(LOG)

strangers = {pid: n for pid, n in counts.items() if pid != PARENT}

print("parent pid          : %d, wrote %d lines" % (PARENT, counts.get(PARENT, 0)))
print("work done by children: %d lines from %d of %d forks"
      % (sum(strangers.values()), len(strangers), FORKS))

sys.exit(1 if strangers else 0)
