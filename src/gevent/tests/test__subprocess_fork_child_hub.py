"""
A child of :mod:`gevent.subprocess` must run none of the parent's greenlets
before it ``exec``s.

The POSIX ``_execute_child`` forks and execs in Python, so for that window the
child is a whole interpreter holding a copy of the parent's greenlets, file
descriptors and TLS sessions. Our own child branch never switches, but
``os.register_at_fork(after_in_child=)`` handlers run inside ``fork()`` before
it begins, and under monkey-patching they yield.

The workers here are parked on a timer rather than merely runnable, so emptying
the loop's callback queue does not account for them: the loop resumes them
itself, so the child must not be running the parent's loop at all.
"""
from gevent import monkey; monkey.patch_all() # pragma: testrunner-no-monkey-combine

import os
import subprocess
import sys
import tempfile

import gevent
import gevent.subprocess

from gevent import testing as greentest

if hasattr(os, 'register_at_fork'):
    # The least a handler can do and still yield.
    os.register_at_fork(after_in_child=lambda: gevent.sleep(0))


class Test(greentest.TestCase):

    # Well under a second here, but a Python-level fork/exec ten times over is
    # slow enough under coverage on a busy CI runner to reach the 15 second
    # default, which it did.
    __timeout__ = 60

    WORKERS = 4
    SPAWNS = 10

    @greentest.skipOnWindows("Uses the POSIX fork/exec path")
    def test_child_runs_none_of_the_parents_greenlets(self):
        fileno, path = tempfile.mkstemp(prefix='gevent-subprocess-fork-child-')
        os.close(fileno)
        fileno = os.open(path, os.O_WRONLY | os.O_APPEND)
        parent = os.getpid()
        stop = []

        def worker(n):
            while not stop:
                # Parked on a timer, not sitting in the callback queue.
                gevent.sleep(0.002)
                # Stands in for the application's side effect: in the process
                # this came from, it was a write on a pooled TLS connection.
                os.write(fileno, b'worker=%d pid=%d\n' % (n, os.getpid()))

        workers = [gevent.spawn(worker, n) for n in range(self.WORKERS)]
        try:
            gevent.sleep(0.05)
            for _ in range(self.SPAWNS):
                subprocess.run([sys.executable, '-c', 'pass'], check=False)
                gevent.sleep(0.01)
        finally:
            stop.append(1)
            gevent.joinall(workers, timeout=10)
            os.close(fileno)

        try:
            with open(path, encoding='ascii') as f:
                strangers = sorted({
                    int(line.rsplit('pid=', 1)[1])
                    for line in f
                } - {parent})
        finally:
            os.unlink(path)

        self.assertEqual(strangers, [])

    @greentest.skipOnWindows("Uses the POSIX fork/exec path")
    def test_another_threads_fork_is_not_taken_for_the_pre_exec_child(self):
        # The window has to be invisible to the rest of the process: a plain
        # ``os.fork()`` on another thread at the same time makes a child that
        # is documented to keep what it inherited. ``fork`` is replaced with
        # a stub: racing two real spawns would hit the window only now and
        # then.
        statuses = []
        hub = gevent.get_hub()
        raw_fork = monkey.get_original('os', 'fork')

        def plain_fork_child_asks_where_it_is():
            pid = raw_fork()
            if pid == 0:
                os._exit(1 if gevent.subprocess._in_pre_exec_child() else 0)
            return os.waitpid(pid, 0)[1]

        def fork_stub():
            # We are now where ``fork()`` would be: the window is open.
            statuses.append(hub.threadpool.apply(plain_fork_child_asks_where_it_is))
            return 1 # Not 0, so the caller takes itself for the parent.

        saved, gevent.subprocess.fork = gevent.subprocess.fork, fork_stub
        try:
            gevent.subprocess._fork_marking_the_pre_exec_window()
        finally:
            gevent.subprocess.fork = saved

        self.assertEqual(os.waitstatus_to_exitcode(statuses[0]), 0)
        # And this thread's window is shut again.
        self.assertFalse(gevent.subprocess._in_pre_exec_child())


if __name__ == '__main__':
    greentest.main()
