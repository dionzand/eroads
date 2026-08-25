"""Copy a PBF onto fast local storage, surviving USB dropouts.

The Europe extract lives on a WD Elements USB drive, and a sustained
multi-pass read of 34.8 GB is exactly the workload that makes such an enclosure
drop off the bus: the scan died 24 minutes in with "The device does not
recognize the command", after which the file read perfectly well again.  The
drive is healthy; the connection is not, under load.

So stage the file once, resumably, and then read it from the internal disk as
many times as the build needs.  A resumable copy is worth more here than a
faster one: an ordinary copy that fails at 30 GB has to start again, while this
picks up from the byte it reached.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

CHUNK = 1 << 24        # 16 MB
MAX_ATTEMPTS = 40
BACKOFF_SECONDS = 5


def resumable_copy(source: Path, target: Path, verbose: bool = True) -> Path:
    """Copy ``source`` to ``target``, resuming after transient I/O failures."""
    total = source.stat().st_size
    target.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        done = target.stat().st_size if target.exists() else 0
        if done >= total:
            break
        if verbose:
            print("[stage] %s -> %s, from %.1f/%.1f GB (attempt %d)"
                  % (source.name, target, done / 1e9, total / 1e9, attempt),
                  file=sys.stderr, flush=True)
        started = time.time()
        copied_now = 0
        try:
            with open(source, "rb") as reader, open(target, "ab") as writer:
                reader.seek(done)
                while True:
                    block = reader.read(CHUNK)
                    if not block:
                        break
                    writer.write(block)
                    done += len(block)
                    copied_now += len(block)
                    if verbose and copied_now % (4 << 30) < CHUNK:
                        elapsed = max(time.time() - started, 0.001)
                        print("[stage]   %.1f/%.1f GB  %.0f MB/s"
                              % (done / 1e9, total / 1e9,
                                 copied_now / 1e6 / elapsed),
                              file=sys.stderr, flush=True)
        except OSError as error:
            if verbose:
                print("[stage]   dropped out at %.1f GB after %.0fs (%s); resuming"
                      % (done / 1e9, time.time() - started, error),
                      file=sys.stderr, flush=True)
            time.sleep(BACKOFF_SECONDS)
            continue

    final = target.stat().st_size if target.exists() else 0
    if final != total:
        raise OSError("copy incomplete: %d of %d bytes after %d attempts"
                      % (final, total, MAX_ATTEMPTS))
    if verbose:
        print("[stage] complete: %.1f GB" % (final / 1e9), file=sys.stderr, flush=True)
    return target


def staged_path(source: Path, directory: Path) -> Path:
    return directory / source.name


if __name__ == "__main__":
    source = Path(sys.argv[1])
    directory = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("C:/osm-staging")
    free = shutil.disk_usage(directory.anchor).free
    needed = source.stat().st_size
    if free < needed * 1.05:
        raise SystemExit("not enough room: need %.1f GB, have %.1f GB"
                         % (needed / 1e9, free / 1e9))
    resumable_copy(source, staged_path(source, directory))
