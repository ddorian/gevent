# /// script
# requires-python = ">=3.10"
# dependencies = ["gevent==26.5.0", "greenlet==3.5.3"]
# ///
"""Repro: ``monkey.patch_all()`` re-reads every installed distribution's metadata
once per patch event.

``gevent.events.notify_and_call_entry_points`` calls
``importlib.metadata.entry_points(...)`` on every event it is handed.  That call
re-reads the metadata of *every* installed distribution, so a single
``patch_all()`` -- which emits 25 events across 5 groups -- rebuilds a
``Distribution`` object for every installed package, 25 times over.

The cost is therefore ``25 x <packages installed>``, i.e. it grows with the size
of the environment rather than with anything gevent is doing.  It is paid at
startup by every gevent process.

Run it::

    uv run repro_entry_points_rescan.py

It counts the metadata reads, times ``patch_all()`` as shipped, then re-times it
with the entry points read once, and prints both.


A CPython detail that will shrink this number on 3.15, but not remove it
-----------------------------------------------------------------------

Part of what makes each ``Distribution`` construction expensive is a compatibility
shim in ``importlib.metadata``::

    class DeprecatedNonAbstract:
        # Required until Python 3.14
        def __new__(cls, *args, **kwargs):
            all_names = {
                name for subclass in inspect.getmro(cls) for name in vars(subclass)
            }
            abstract = {
                name
                for name in all_names
                if getattr(getattr(cls, name), '__isabstractmethod__', False)
            }
            ...

``Distribution`` inherits from it, so every construction runs an
``inspect.getmro()`` walk plus a ``vars()`` sweep and a ``getattr()`` per name --
about 7us a time, which at 25 events x N packages is not nothing.

**CPython ``main`` has already deleted it** (``Distribution`` is now plain
``metaclass=abc.ABCMeta``), so on 3.15 this shim's share of the cost disappears on
its own -- despite the comment above, it is still present in 3.14.  A maintainer
measuring on a 3.15 beta will therefore see a *smaller* number than the one below
and might reasonably wonder whether the bug is worth fixing.

It is: the shim is only part of it.  The rest is the metadata reading itself, which
no CPython change removes.  So this script measures both -- it re-runs with the shim
neutralised, which is what 3.15 will behave like, and shows what the fix is still
worth there.
"""

import subprocess
import sys
import textwrap

# Pad the environment out to a realistic size.  gevent's own test venv is small;
# a real application's is not, and this cost scales linearly with it.  We do not
# install anything -- we synthesise empty ``.dist-info`` directories, which is
# all ``importlib.metadata`` looks at.
_MAKE_FAKE_DISTS = """
import pathlib, sys, tempfile

def make_dists(n):
    d = pathlib.Path(tempfile.mkdtemp(prefix="fakedists-"))
    for i in range(n):
        info = d / f"fakepkg{i}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\\nName: fakepkg{i}\\nVersion: 1.0\\n")
        (info / "entry_points.txt").write_text("[some.other.group]\\nx = os:getpid\\n")
    sys.path.insert(0, str(d))
    return d
"""

CHILD = _MAKE_FAKE_DISTS + textwrap.dedent(
    """
    import sys, time
    from importlib import metadata

    N_DISTS = int(sys.argv[1])
    FIXED = sys.argv[2] == "fixed"
    DROP_SHIM = sys.argv[3] == "drop-shim"

    if DROP_SHIM:
        # Simulate Python 3.15, where importlib.metadata's DeprecatedNonAbstract
        # compat shim is gone.  Distribution.__new__ stops doing a getmro()/vars()
        # sweep per construction.  Everything else -- the metadata reading itself --
        # is unchanged.
        shim = getattr(metadata, "DeprecatedNonAbstract", None)
        if shim is not None:
            shim.__new__ = lambda cls, *a, **k: object.__new__(cls)

    make_dists(N_DISTS)

    import gevent.events

    # Count how many times the metadata of the whole environment is re-read.
    reads = [0]
    _real_entry_points = metadata.entry_points
    def counting_entry_points(*args, **kwargs):
        reads[0] += 1
        return _real_entry_points(*args, **kwargs)
    metadata.entry_points = counting_entry_points

    # Count the events too, so the "25 events" claim is checked, not asserted.
    events = [0]
    _impl = gevent.events.notify_and_call_entry_points

    if FIXED:
        # THE FIX, applied from outside so this runs against a released gevent:
        # read the entry points once, then select per group.
        _cache = []
        def _impl(event):
            gevent.events.notify(event)
            if not _cache:
                _cache.append(metadata.entry_points())
            for plugin in _cache[0].select(group=event.ENTRY_POINT_NAME):
                plugin.load()(event)

    def counting_notify(event):
        events[0] += 1
        return _impl(event)
    gevent.events.notify_and_call_entry_points = counting_notify

    from gevent import monkey
    t0 = time.perf_counter()
    monkey.patch_all()
    elapsed_ms = (time.perf_counter() - t0) * 1000

    shim_present = getattr(metadata, "DeprecatedNonAbstract", None) is not None
    n_installed = len(list(metadata.distributions()))
    print(f"RESULT {elapsed_ms:.1f} {events[0]} {reads[0]} {n_installed} {int(shim_present)}")
    """
)


def run(n_dists, variant, shim="keep-shim"):
    """Run patch_all() in a fresh interpreter. -> (ms, events, reads, dists, shim_present)"""
    best = None
    for _ in range(3):  # min-of-3: we want the cost, not the scheduler's noise
        proc = subprocess.run(
            [sys.executable, "-c", CHILD, str(n_dists), variant, shim],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        line = next(x for x in proc.stdout.splitlines() if x.startswith("RESULT"))
        _, ms, events, reads, dists, shim_present = line.split()
        row = (float(ms), int(events), int(reads), int(dists), bool(int(shim_present)))
        if best is None or row[0] < best[0]:
            best = row
    return best


def main():
    print(f"python {sys.version.split()[0]}\n")
    print(f"{'installed dists':>16} {'patch_all()':>13} {'events':>8} {'metadata reads':>15}")
    print("-" * 60)

    rows = []
    for n_dists in (0, 100, 300):
        stock = run(n_dists, "stock")
        fixed = run(n_dists, "fixed")
        rows.append((stock[3], stock[0], fixed[0]))
        print(f"{stock[3]:>16} {stock[0]:>10.0f} ms {stock[1]:>8} {stock[2]:>15}")
        print(f"{'^ with fix':>16} {fixed[0]:>10.0f} ms {fixed[1]:>8} {fixed[2]:>15}")

    dists, stock_ms, fixed_ms = rows[-1]
    print(
        f"\nAt {dists} installed distributions, patch_all() spends {stock_ms - fixed_ms:.0f}ms of "
        f"{stock_ms:.0f}ms re-reading\npackage metadata it already read. The read is identical every "
        f"time: the entry points\ncannot change while patch_all() is running."
    )
    print(f"\nGrowth from an empty environment to {dists} distributions: +{stock_ms - rows[0][1]:.0f}ms.")

    # ---- what this will look like on Python 3.15 -----------------------------
    shim_present = run(0, "stock")[4]
    print("\n" + "=" * 60)
    if not shim_present:
        print(
            "importlib.metadata.DeprecatedNonAbstract is already gone from this\n"
            "interpreter, so the numbers above are the post-3.15 ones."
        )
        return 0

    stock_ng = run(dists, "stock", "drop-shim")[0]
    fixed_ng = run(dists, "fixed", "drop-shim")[0]
    print(
        "Python 3.15 preview (importlib.metadata's DeprecatedNonAbstract shim\n"
        "neutralised -- CPython main has already deleted it):\n\n"
        f"  {'stock':<12} {stock_ms:>6.0f} ms  ->  {stock_ng:>6.0f} ms\n"
        f"  {'with fix':<12} {fixed_ms:>6.0f} ms  ->  {fixed_ng:>6.0f} ms\n\n"
        f"So 3.15 removes {stock_ms - stock_ng:.0f}ms of this on its own -- but the fix is still\n"
        f"worth {stock_ng - fixed_ng:.0f}ms there, because the shim was only part of it. The rest is\n"
        "the metadata reading itself, which no CPython change removes."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
