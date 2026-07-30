"""
A child of :mod:`gevent.subprocess` may not settle down in its own event loop.

The child gets a hub of its own so that it cannot resume the parent's greenlets
(:mod:`gevent.tests.test__subprocess_fork_child_hub`). A hub that merely works,
though, gives it somewhere to stay: a fork handler that leaves anything on the
loop and then waits for something only the parent could provide waits forever,
because the loop has work and so never raises ``LoopExit``. ``fork()`` never
returns, no ``exec`` happens, and the child lives on as a second interpreter
holding a clone of every descriptor the parent had. One was found two hours old
holding an application's whole database pool.

The parent is no better off. It blocks reading the error pipe inside
``Popen.__init__``, before any ``timeout=`` has started counting, so the spawn
hangs forever too. That is what makes a deadline the fix rather than a nicety:
there is nothing else left to notice.

The handler here is the shape that does it, reduced: keep the loop busy, then
wait for what will never come.
"""
from gevent import monkey; monkey.patch_all() # pragma: testrunner-no-monkey-combine

import os
import subprocess
import sys
import threading
import time

import gevent

from gevent import testing as greentest

PARENT = os.getpid()


def _busy(): # pragma: no cover
    # Runs in the child. Keeps its loop from ever going idle, which is what
    # withholds the ``LoopExit`` that would otherwise send it on to ``exec``.
    while True:
        gevent.sleep(0.005)


def _wedge_the_child(): # pragma: no cover
    if os.getpid() == PARENT:
        return
    gevent.spawn(_busy)
    # Whatever fed this in the parent is not in this process. A flush(), a
    # queue.get(), a join() on a worker whose queue nobody fills: all this shape.
    threading.Event().wait()


if hasattr(os, 'register_at_fork'):
    # Through the *raw* function, deliberately. gevent skips handlers
    # registered through the patched one in this child
    # (:mod:`gevent.tests.test__subprocess_atfork_guard`), which is the real
    # fix for this and would stop the wedge from ever happening. The deadline
    # tested here is the backstop for the handlers that guard cannot reach:
    # ones registered before :mod:`gevent.subprocess` was imported, such as a
    # ``coverage`` started from a site hook. Registering the ordinary way would
    # leave that backstop untested.
    monkey.get_original('os', 'register_at_fork')(
        after_in_child=_wedge_the_child)


@greentest.skipOnWindows("Uses the POSIX fork/exec path")
@greentest.skipIf(
    not hasattr(os, 'register_at_fork') or not os.path.isdir('/proc/self'),
    "Needs register_at_fork to wedge the child, and a Linux-style /proc to prove "
    "afterwards that none is left. The deadline itself is not platform-specific, "
    "but this way of observing it is."
)
class Test(greentest.TestCase):

    # The deadline itself, plus room for the spawn around it.
    __timeout__ = 60

    def test_child_that_stays_in_the_loop_is_killed(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.SubprocessError) as exc:
            subprocess.run([sys.executable, '-c', 'pass'], check=False)
        elapsed = time.monotonic() - started

        # The spawn fails, rather than hanging forever as it used to.
        self.assertIn('event loop before exec()', str(exc.exception))
        # Promptly: the point is the bound, not merely that it ends.
        self.assertLess(elapsed, 30)

        # And it says which handler did it. Worth asserting rather than trusting:
        # the greenlet reported has to be the one that never came back, and the
        # obvious choice --- the last to switch in --- is a bystander, whatever
        # has been keeping the loop alive. Getting that wrong still produces a
        # plausible-looking report naming the wrong function.
        self.assertIn('_wedge_the_child', str(exc.exception))
        self.assertIn('_wedge_the_child',
                      getattr(exc.exception, 'child_traceback', ''))

        # And nothing is left behind wearing our own cmdline, which is what a
        # child that never reached ``exec`` looks like.
        self.assertEqual(self._children_still_unexec(), [])

    @staticmethod
    def _children_still_unexec():
        with open('/proc/self/cmdline', 'rb') as f:
            mine = f.read()
        # The child is killed inside ``fork()``; give the kernel a moment to
        # reap it before concluding that it is still there.
        for _ in range(100):
            found = []
            for entry in os.listdir('/proc'):
                if not entry.isdigit():
                    continue
                try:
                    with open('/proc/%s/stat' % entry, 'rb') as f:
                        raw = f.read()
                    # The second field is the comm, which may hold spaces and
                    # parentheses; everything is positional after its close.
                    fields = raw[raw.rindex(b')') + 2:].split()
                    if int(fields[1]) != PARENT or fields[0] == b'Z':
                        continue
                    with open('/proc/%s/cmdline' % entry, 'rb') as f:
                        if f.read() == mine:
                            found.append(entry)
                except (OSError, ValueError):
                    continue
            if not found:
                return []
            time.sleep(0.05)
        return found


if __name__ == '__main__':
    greentest.main()
