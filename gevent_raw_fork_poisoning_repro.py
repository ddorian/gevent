# /// script
# requires-python = ">=3.10"
# dependencies = ["gevent==26.7.0", "greenlet==3.5.4", "trustme==1.2.1"]
# ///
"""gevent: a plain os.fork() poisons the process's TLS connections.

    $ uv run --script gevent_raw_fork_poisoning_repro.py
    POISONED: SSLError('[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac')

The sibling of gevent_fork_ssl_poisoning_repro.py with the spawn removed. The
child here does nothing whatsoever: it calls os._exit(0) the instant fork()
returns. Every byte it put on the wire was written by a copy of the parent's
greenlet, inside fork(), before it returned to anyone.

os.register_at_fork(after_in_child=) handlers run inside fork(), before it has
returned to its caller, and under monkey-patching they yield. OpenTelemetry,
Sentry and filelock all register one. The yield reaches the forked child's copy
of the hub, which resumes copies of the parent's greenlets. A resumed copy
writes a record under the sequence number its copy of the session state says is
next; the parent reuses that number; the server's MAC check fails and it answers
bad_record_mac.

Giving the gevent.subprocess child a hub of its own does not help here, because
this child is not a subprocess child. It keeps the hub it inherited, on purpose:
a child of a plain fork may legitimately keep using the gevent objects it
inherited, which is what gevent's own bind/fork/accept server pattern does.

Delete the register_at_fork line below for the control run, which passes.
"""
from gevent import monkey; monkey.patch_all()   # first, before anything it patches

import sys

if sys.argv[1:2] == ['--server']:       # this file re-executed as the TLS peer, in a
    import socket                       # separate process, so no fork of ours reaches it
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(sys.argv[2])
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    print(srv.getsockname()[1], flush=True)
    conn = ctx.wrap_socket(srv.accept()[0], server_side=True)
    while True:
        try:
            data = conn.recv(4096)
        except OSError:                 # the MAC failure; the alert is already sent
            break
        if not data:
            break
        conn.sendall(data)
    sys.exit()


import os
import socket
import ssl
import subprocess

import gevent
import trustme

print('gevent %s | CPython %s'
      % (gevent.__version__, '.'.join(str(v) for v in sys.version_info[:3])))

ca = trustme.CA()
cert = ca.issue_cert('localhost')
with cert.private_key_and_cert_chain_pem.tempfile() as pem:
    server = subprocess.Popen([sys.executable, os.path.abspath(__file__), '--server', pem],
                              stdout=subprocess.PIPE)
    port = int(server.stdout.readline())

# The least a fork handler can do and still yield. Delete for the control run.
os.register_at_fork(after_in_child=lambda: gevent.sleep(0))

ctx = ssl.create_default_context()
ca.configure_trust(ctx)
sock = ctx.wrap_socket(socket.create_connection(('127.0.0.1', port)),
                       server_hostname='localhost')

def talk():
    while True:
        sock.sendall(b'ping')
        sock.recv(4)
        gevent.sleep(0.002)

talker = gevent.spawn(talk)
gevent.sleep(0.1)

for _ in range(20):
    pid = os.fork()
    if pid == 0:
        os._exit(0)     # do nothing whatsoever, and leave
    os.waitpid(pid, 0)
    gevent.sleep(0.01)
    if talker.dead:
        break

server.kill()
if talker.dead:
    print('POISONED: %r' % (talker.exception,))
    sys.exit(1)
talker.kill()
print('TLS connection intact')
