# Copyright (c) 2026 gevent community
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
from __future__ import absolute_import, print_function, division


def wait_for_child(pid, timeout=30, _poll=0.005):
    """
    Reap the child *pid* cooperatively and return its wait status.

    Use this in a test that forks rather than ``os.waitpid(pid, 0)``, which
    under libuv can block until the caller gives up even though the child is
    long gone.

    The loop's ``SIGCHLD`` handler reaps *every* child it can find, in one
    ``wait3(WNOHANG)`` sweep, and hands each status to the watchers registered
    at that moment by way of an ``async`` watcher, so delivery lands an
    iteration later. A ``waitpid`` arriving in that gap sees the status as
    undelivered and starts a *fresh* child watcher for the pid, which nothing
    will ever feed: the reap has already happened, and no second ``SIGCHLD``
    is coming for a child that no longer exists. Measured on Linux at about
    one wait in 400 with a single child, and more often when several exit
    together. libev delivers synchronously, so the status is already recorded
    by the time ``waitpid`` looks and this path is never taken.

    Polling with ``WNOHANG`` consults the same bookkeeping without starting a
    watcher, so it sees the status whenever delivery happens: 0 hangs in 600
    waits under libuv.

    This is a test-side accommodation, not a fix. The underlying hang belongs
    to :func:`gevent.os.waitpid` under libuv and is not about forking.
    """
    # Imported here: this module is used by monkey-patched tests, which want
    # the patched versions, and by fork tests, which must not pay for imports
    # they do not use.
    import os
    from time import monotonic
    import gevent

    deadline = monotonic() + timeout
    while True:
        rpid, status = os.waitpid(pid, os.WNOHANG)
        if rpid:
            return status
        if monotonic() >= deadline:
            raise AssertionError(
                'child %d did not exit within %ss' % (pid, timeout)
            )
        gevent.sleep(_poll)
