"""
An ``after_in_child`` handler must not run in a child that is about to ``exec``.

Those handlers are written for a child that *continues*: they reinitialise
locks and rebuild thread pools. gevent's POSIX spawn forks and ``exec``s in
Python, so they run in that child too, where none of what they rebuild can
outlive the ``exec`` --- and where most greenlets are copies that will never run
again, so a handler that takes a monkey-patched lock another greenlet held at
the fork blocks in a process that cannot unblock it. The spawn is then lost.

A child of a plain ``os.fork()`` is the opposite case: it continues, and needs
every handler. One pipe tells the two apart, because a handler that runs writes
to it and one that is skipped does not.
"""
from gevent import monkey; monkey.patch_all() # pragma: testrunner-no-monkey-combine

import os
import subprocess
import sys

from gevent import subprocess as gevent_subprocess

from gevent import testing as greentest

READER, WRITER = (os.pipe() if hasattr(os, 'register_at_fork') else (-1, -1))


def _mark(): # pragma: no cover
    # Runs in a forked child. Raw write: nothing here may yield.
    try:
        os.write(WRITER, b'x')
    except OSError:
        pass


if hasattr(os, 'register_at_fork'):
    # Registered *after* gevent.subprocess was imported, which is what makes it
    # ours to guard. gevent's own handler is registered before the guard is
    # installed and so keeps running in the pre-exec child, where it must.
    os.register_at_fork(after_in_child=_mark)


@greentest.skipIf(not hasattr(os, 'register_at_fork'),
                  "Needs os.register_at_fork")
class Test(greentest.TestCase):

    __timeout__ = 60

    def _drain(self):
        os.set_blocking(READER, False)
        try:
            return os.read(READER, 4096)
        except BlockingIOError:
            return b''
        finally:
            os.set_blocking(READER, True)

    def test_predicate_is_false_outside_the_window(self):
        # It is a question anyone may ask at any time, including on Windows,
        # where the answer is always no.
        self.assertFalse(gevent_subprocess.in_pre_exec_child())

    @greentest.skipOnWindows("Uses the POSIX fork/exec path")
    def test_handler_is_skipped_before_exec(self):
        self._drain()
        subprocess.run([sys.executable, '-c', 'pass'], check=True)
        self.assertEqual(self._drain(), b'')

    @greentest.skipOnWindows("Uses fork")
    def test_handler_still_runs_in_a_real_forked_child(self):
        # The other side, and the one that says the guard is narrow: this
        # child carries on, so everything registered has to run for it.
        self._drain()
        pid = os.fork()
        if pid == 0: # pragma: no cover
            os._exit(0)
        greentest.wait_for_child(pid, timeout=20)
        self.assertEqual(self._drain(), b'x')

    @greentest.skipOnWindows("Uses the POSIX fork/exec path")
    def test_guard_can_be_turned_off(self):
        # The control for the first test. Without it, that test would keep
        # passing if the handler stopped running for some other reason.
        script = (
            'from gevent import monkey; monkey.patch_all()\n'
            'import os, subprocess, sys\n'
            'ran = []\n'
            'os.register_at_fork(after_in_child=lambda: ran.append(1))\n'
            'subprocess.run([sys.executable, "-c", "pass"])\n'
            # ``ran`` is appended to in the child, so the parent cannot see it.
            # What the parent can see is whether the handler was wrapped.
            'from gevent import subprocess as gs\n'
            'print(os.register_at_fork is gs.register_at_fork)\n'
        )
        env = os.environ.copy()
        env['GEVENT_ATFORK_GUARD'] = '0'
        off = subprocess.run([sys.executable, '-c', script], env=env,
                             capture_output=True, text=True, check=True)
        self.assertEqual(off.stdout.strip(), 'False', off.stderr)

        env['GEVENT_ATFORK_GUARD'] = '1'
        on = subprocess.run([sys.executable, '-c', script], env=env,
                            capture_output=True, text=True, check=True)
        self.assertEqual(on.stdout.strip(), 'True', on.stderr)


if __name__ == '__main__':
    greentest.main()
