"""
Nothing may switch greenlets in a forked child, between ``fork()`` and ``exec()``.

The child is a copy of a whole process worth of greenlets, including other calls to
``Popen._execute_child`` holding another spawn's pipe write ends. Any copy that runs
there forks and execs a second time, and its output lands on the *first* spawn's
pipes, so one capture comes back holding several complete copies of a child's
output --- on stdout and stderr alike, since both were inherited together.

``os.register_at_fork(after_in_child=)`` handlers run inside ``fork()`` itself, so an
application can put a switch there without gevent's child branch ever doing so.
"""
from gevent import monkey; monkey.patch_all() # pragma: testrunner-no-monkey-combine

import os
import subprocess
import sys

import gevent

from gevent import testing as greentest

STDOUT_LEN = 5000
STDERR_LEN = 300
CHILD = "import sys;sys.stdout.write('A'*%d);sys.stderr.write('B'*%d)" % (
    STDOUT_LEN, STDERR_LEN)

if hasattr(os, 'register_at_fork'):
    # The least a handler can do and still yield. Applications get here by
    # restarting a worker thread (``Thread.start()`` waits on an ``Event``), by
    # touching a patched lock another greenlet holds, or through ``logging``.
    os.register_at_fork(after_in_child=lambda: gevent.sleep(0))


class Test(greentest.TestCase):

    # Each capture is checked against one child lifetime; a splice shows up as an
    # exact multiple of it. Two greenlets are enough to overlap, three make it prompt.
    GREENLETS = 3
    SPAWNS = 10

    @greentest.skipOnWindows("Uses fork")
    def test_capture_holds_exactly_one_child(self):
        captures = []

        def probe():
            for _ in range(self.SPAWNS):
                # check=False: the exit status is the legitimate child's either way,
                # so raising on it would only hide the capture that is the diagnosis.
                completed = subprocess.run([sys.executable, '-c', CHILD],
                                           capture_output=True, check=False)
                captures.append((len(completed.stdout), len(completed.stderr)))

        gevent.joinall([gevent.spawn(probe) for _ in range(self.GREENLETS)],
                       raise_error=True)

        expected = (STDOUT_LEN, STDERR_LEN)
        self.assertEqual(len(captures), self.GREENLETS * self.SPAWNS)
        # Report the multiples, they are the diagnosis: (2, 2) is one extra child,
        # (4, 4) two more, and stdout and stderr always agree.
        spliced = [(out // STDOUT_LEN, err // STDERR_LEN)
                   for out, err in captures if (out, err) != expected]
        self.assertEqual(spliced, [])


if __name__ == '__main__':
    greentest.main()
