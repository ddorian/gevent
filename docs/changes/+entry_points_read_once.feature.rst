Read the installed distributions' entry points once per process instead of once
per patch event, making ``gevent.monkey.patch_all()`` substantially faster to
start up.

``gevent.events.notify_and_call_entry_points`` called
``importlib.metadata.entry_points()`` for every event it was handed, and each of
those calls re-reads the metadata of *every* installed distribution. A single
``patch_all()`` emits 25 events (across five groups), so it rebuilt a
``Distribution`` object for every installed package, 25 times over --- a cost that
grows with the size of the environment rather than with anything gevent does.

In a virtualenv with 300 distributions installed, ``patch_all()`` spent ~175ms of
its ~199ms re-reading metadata it had already read; it now takes ~23ms. The same
plugins are called, in the same order: entry points cannot change while
``patch_all()`` is running.
