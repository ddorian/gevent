"""
A child of :mod:`gevent.subprocess` must run none of the parent's greenlets
before it ``exec``s.

gevent's POSIX ``_execute_child`` forks and execs in Python, so between
``fork()`` and ``exec()`` the child is a whole interpreter holding a copy of the
parent's greenlets, open file descriptors and in-memory TLS sessions. Our own
child branch never switches, but ``os.register_at_fork(after_in_child=)``
handlers run inside ``fork()`` before that branch begins, applications register
them (OpenTelemetry, Sentry, ``filelock``, ``coverage``), and under
monkey-patching such handlers yield.

Unlike :mod:`gevent.tests.test__fork_child_greenlets`, the workers here are
parked on a timer rather than merely runnable, so emptying the loop's callback
queue does not account for them: they are resumed by the loop itself. The child
must not be running the parent's loop at all.
"""
from gevent import monkey; monkey.patch_all() # pragma: testrunner-no-monkey-combine

import os
import subprocess
import sys
import tempfile

import gevent

from gevent import testing as greentest

if hasattr(os, 'register_at_fork'):
    # The least a handler can do and still yield.
    os.register_at_fork(after_in_child=lambda: gevent.sleep(0))


class Test(greentest.TestCase):

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


if __name__ == '__main__':
    greentest.main()
