# /// script
# requires-python = ">=3.10"
# dependencies = ["gevent==26.7.0", "greenlet==3.5.4", "trustme==1.2.1"]
# ///
"""gevent: a plain os.fork() poisons the process's TLS connections.

    $ uv run --script gevent_raw_fork_poisoning_repro.py
    POISONED: SSLError('[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac')

The child does nothing but os._exit(0), so every byte it put on the wire came
from a copy of the parent's greenlet, running inside fork(). An at-fork child
handler that yields (OpenTelemetry, Sentry and filelock each register one) hands
control to the child's copy of the hub, which resumes that copy; it writes a TLS
record under the sequence number the parent then reuses, and the server answers
bad_record_mac.

gevent_fork_ssl_poisoning_repro.py is this with the fork replaced by a spawn.
That child gets a hub of its own; this one keeps the hub it inherited, because a
child of a plain fork may legitimately keep using inherited gevent objects.

Delete the register_at_fork line for the control run, which passes.
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
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            conn.sendall(data)
    except OSError:                     # the MAC failure; the alert is already sent
        pass
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
    os.waitpid(pid, 0)  # not os.wait(), which would reap the server too
    gevent.sleep(0.01)

server.kill()
if talker.dead:
    print('POISONED: %r' % (talker.exception,))
    sys.exit(1)
print('TLS connection intact')
