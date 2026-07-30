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
import threading
import time

import gevent

from gevent import subprocess as gevent_subprocess

from gevent import testing as greentest

READER, WRITER = (os.pipe() if hasattr(os, 'register_at_fork') else (-1, -1))

#: Stands in for the ones the registrants in the census really take: a
#: ``logging`` lock, a ``filelock``, the one inside a ``ThreadPoolExecutor``.
LOCK = threading.Lock()


def _mark(): # pragma: no cover
    # Runs in a forked child. Raw write: nothing here may yield.
    try:
        os.write(WRITER, b'x')
    except OSError:
        pass


def _wants_the_lock(): # pragma: no cover
    # The shape that lost the spawns: a handler that waits for something only
    # a greenlet could hand over, in a process where no greenlet may run.
    with LOCK:
        pass


if hasattr(os, 'register_at_fork'):
    # Registered *after* gevent.subprocess was imported, which is what makes it
    # ours to guard. gevent's own handler is registered before the guard is
    # installed and so keeps running in the pre-exec child, where it must.
    os.register_at_fork(after_in_child=_mark)
    os.register_at_fork(after_in_child=_wants_the_lock)


class TestPredicate(greentest.TestCase):
    # Deliberately outside the class below, which skips wherever
    # ``os.register_at_fork`` is missing. That is Windows, which is the only
    # platform where the Windows implementation of this exists, so a skip there
    # would leave it the one part of this never run anywhere.

    __timeout__ = 60

    def test_is_false_outside_the_window(self):
        # A question anyone may ask at any time, on any platform.
        self.assertFalse(gevent_subprocess.in_pre_exec_child())


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
    def test_spawn_survives_a_handler_that_wants_a_held_lock(self):
        # The reported failure, reduced: the handler waits for a lock a
        # greenlet holds across the spawn, and only that greenlet can give it
        # up. Scenario coverage rather than a tripwire --- what it does without
        # the guard depends on whether the child's loop has anything else to
        # do, and with nothing to do the wait ends in a LoopExit that CPython
        # prints and ignores rather than in a lost spawn.
        # ``test_handler_is_skipped_before_exec`` is the deterministic one.
        holder = gevent.spawn(self._hold_lock_for, 3)
        try:
            gevent.sleep(0.05)
            self.assertTrue(LOCK.locked())
            started = time.time()
            subprocess.run([sys.executable, '-c', 'pass'], check=True)
            # Promptly: not merely 'did not hang', but 'did not sit out the
            # pre-exec deadline either'.
            self.assertLess(time.time() - started, 10)
        finally:
            holder.kill(block=True)

    @greentest.skipOnWindows("Uses the POSIX fork/exec path")
    def test_concurrent_spawns_survive_a_cycling_lock(self):
        # Concurrency is what turned this from possible into certain: each fork
        # lands while another spawn's handlers are mid-critical-section. The
        # application that hit it ran two spawns at once and lost both, every
        # time, while its serial forks stayed clean.
        stop = []
        cycler = gevent.spawn(self._cycle_lock, stop)
        try:
            gevent.sleep(0.02)
            spawns = [
                gevent.spawn(subprocess.run, [sys.executable, '-c', 'pass'])
                for _ in range(self.CONCURRENT)
            ]
            gevent.joinall(spawns, timeout=60)
            for g in spawns:
                self.assertIsNone(g.exception)
                self.assertIsNotNone(g.value, "spawn did not finish")
                self.assertEqual(g.value.returncode, 0)
        finally:
            stop.append(1)
            cycler.kill(block=True)

    CONCURRENT = 4

    @staticmethod
    def _hold_lock_for(seconds): # pragma: no cover
        with LOCK:
            gevent.sleep(seconds)

    @staticmethod
    def _cycle_lock(stop): # pragma: no cover
        while not stop:
            with LOCK:
                gevent.sleep(0.001)
            gevent.sleep(0)

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
