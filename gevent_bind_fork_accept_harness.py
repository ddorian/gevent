# /// script
# requires-python = ">=3.10"
# dependencies = ["gevent==26.7.0", "greenlet==3.5.4"]
# ///
"""gevent: children must keep serving on a listener inherited across fork().

    $ uv run --script gevent_bind_fork_accept_harness.py
    $ SHAPE=accepting uv run --script gevent_bind_fork_accept_harness.py

Not a bug reproducer. This guards the fix that cancels the parent's pending
waits in a forked child, whose safety argument is that stopping a watcher does
not damage the object it belongs to, so a child can carry on using what it
inherited. gevent's own bind/fork/accept server pattern is the thing that would
break if that were wrong, so it has to pass before the fix as well as after.

The parent binds a listener, keeps some greenlets parked so the child has copies
worth cancelling, and forks. Each child accepts on the inherited listener until
a deadline and echoes what it reads. The parent drives load and checks every
reply, then waits for the children to exit.

    SHAPE=idle       the parent never accepts before forking (the usual pattern)
    SHAPE=accepting  a parent greenlet is already parked in accept() at the
                     fork, so the listener's io watcher is active and the fix
                     stops it in the child. The child's own accept() must
                     start it again.

A child that never serves, or a client that never gets its bytes back, is the
failure this is looking for. It shows up as a count, not as an exception.
"""
from gevent import monkey; monkey.patch_all()   # first, before anything it patches

import os
import socket
import sys
import tempfile
import time

import gevent

SHAPE = os.environ.get('SHAPE', 'idle')         # idle | accepting
WORKERS = int(os.environ.get('WORKERS', '4'))   # forked children
CLIENTS = int(os.environ.get('CLIENTS', '8'))   # concurrent client greenlets
BUSY = int(os.environ.get('BUSY', '4'))         # parked greenlets in the parent
DURATION = float(os.environ.get('DURATION', '3.0'))

print('gevent %s | CPython %s | shape=%s workers=%d duration=%.1fs'
      % (gevent.__version__, '.'.join(str(v) for v in sys.version_info[:3]),
         SHAPE, WORKERS, DURATION))

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(('127.0.0.1', 0))
listener.listen(128)
port = listener.getsockname()[1]

fileno, tally_path = tempfile.mkstemp(prefix='gevent-bind-fork-accept-')
os.close(fileno)
tally = os.open(tally_path, os.O_WRONLY | os.O_APPEND)

# The ingredient that lets a forked child run copies of the parent's greenlets
# at all. Without it there is nothing for the fix to cancel and the harness
# proves nothing.
os.register_at_fork(after_in_child=lambda: gevent.sleep(0))

stop = []


def busy(_n):
    # Parked on a timer at any given moment, which is what the fix cancels in
    # the child. These must not run there; the listener must still work.
    while not stop:
        gevent.sleep(0.002)


busies = [gevent.spawn(busy, n) for n in range(BUSY)]

parent_acceptor = None
if SHAPE == 'accepting':
    # Park a greenlet in accept() so the listener's io watcher is active across
    # the fork. This is the shape that would break if stopping a watcher
    # damaged it.
    parent_acceptor = gevent.spawn(listener.accept)

gevent.sleep(0.1)

deadline = time.monotonic() + DURATION
children = []
for _ in range(WORKERS):
    pid = os.fork()
    if pid == 0:
        served = 0
        listener.settimeout(0.2)
        try:
            while time.monotonic() < deadline:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    continue            # accept timeout; go round again
                try:
                    data = conn.recv(64)
                    if data:
                        conn.sendall(data)
                        served += 1
                finally:
                    conn.close()
        finally:
            os.write(tally, b'pid=%d served=%d\n' % (os.getpid(), served))
        os._exit(0)
    children.append(pid)

if parent_acceptor is not None:
    parent_acceptor.kill(block=False)

ok = [0]
bad = [0]


def client(n):
    i = n
    while time.monotonic() < deadline - 0.3:
        payload = b'%08d' % (i % 100000000)
        try:
            sock = socket.create_connection(('127.0.0.1', port), timeout=5)
            sock.sendall(payload)
            got = sock.recv(8)
            sock.close()
        except OSError:
            bad[0] += 1
            gevent.sleep(0.01)
            continue
        if got == payload:
            ok[0] += 1
        else:
            bad[0] += 1
        i += CLIENTS


clients = [gevent.spawn(client, n) for n in range(CLIENTS)]
gevent.joinall(clients, timeout=DURATION + 10)

stop.append(1)
gevent.joinall(busies, timeout=10)

reaped = []
timed_out = []
for pid in children:
    with gevent.Timeout(15, False):
        reaped.append(os.waitpid(pid, 0))
        continue
    timed_out.append(pid)

listener.close()
os.close(tally)
with open(tally_path, encoding='ascii') as f:
    tallies = [line.strip() for line in f if line.strip()]
os.unlink(tally_path)

served_by = {}
for line in tallies:
    pid_s, served_s = line.split()
    served_by[int(pid_s.split('=')[1])] = int(served_s.split('=')[1])

print('requests: %d served correctly, %d failed' % (ok[0], bad[0]))
print('children: %d of %d reported, %d never exited' % (
    len(served_by), WORKERS, len(timed_out)))
for pid, served in sorted(served_by.items()):
    print('    child %d served %d' % (pid, served))

problems = []
if timed_out:
    problems.append('%d child(ren) never exited: %s' % (len(timed_out), timed_out))
if len(served_by) != WORKERS:
    problems.append('only %d of %d children reported' % (len(served_by), WORKERS))
if [pid for pid, served in served_by.items() if served == 0]:
    problems.append('some child accepted nothing at all')
if bad[0]:
    problems.append('%d requests failed or came back wrong' % bad[0])
if ok[0] == 0:
    problems.append('no request was served')
if any(status != 0 for _, status in reaped):
    problems.append('a child exited non-zero: %r' % (reaped,))

if problems:
    print('BROKEN: ' + '; '.join(problems))
    sys.exit(1)
print('OK: the inherited listener kept working in every child')
