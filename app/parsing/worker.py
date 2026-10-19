"""The parse child: reads a workbook on stdin, writes one JSON message on stdout, and exits.

Started only by ``app.parsing.sandbox`` as ``python -I -c <bootstrap> <memory_mb> <cpu_s> <target>``
with a minimal environment (no database URL, no secrets). It imports nothing of the web app, the
database layer or the settings: just the parser and the wire format.

Order matters. The bytes are read first, then (on Linux) the resource limits go on, then the
parser runs. ``sys.stdout`` is pointed at stderr before anything runs, so a stray ``print`` in a
library cannot corrupt the answer; the answer goes to the original stdout buffer.
"""

from __future__ import annotations

import importlib
import re
import sys
from collections.abc import Callable
from typing import Any

from app.parsing import wire
from app.parsing.amadeus import ParsedReport, ParseError

_resource: Any = None  # the ``resource`` module on POSIX; typed Any so mypy is platform-neutral
try:
    import resource

    _resource = resource
except ImportError:  # pragma: no cover - Windows
    pass

HAS_RLIMIT = sys.platform == "linux" and _resource is not None
MSG_CRASH = "the file could not be read"
MSG_MEMORY = "the file needs too much memory to read"
DEFAULT_TARGET = "app.parsing.amadeus:parse_report"
_TARGET_SPEC = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_][\w.]*")


def run_target(target: Callable[[bytes], ParsedReport], data: bytes) -> bytes:
    """Run the parser and encode its answer. Never raises: the child reports, it does not crash."""
    try:
        return wire.encode_report(target(data))
    except ParseError as exc:
        text = str(exc)
    except MemoryError:
        text = MSG_MEMORY
    except Exception:
        text = MSG_CRASH  # the exception's own text could hold file contents or paths
    return wire.encode_error(text)


def apply_limits(memory_mb: int, cpu_seconds: int, resource_module: Any = None) -> list[str]:
    """Linux: cap address space and CPU, forbid new processes and any file bigger than 0 bytes,
    and write no core file. Returns the names of the limits applied (none elsewhere)."""
    res = resource_module if resource_module is not None else _resource
    if res is None or (resource_module is None and not HAS_RLIMIT):
        return []
    cap = memory_mb * 1024 * 1024
    wanted = [
        ("RLIMIT_AS", (cap, cap)),
        ("RLIMIT_CPU", (cpu_seconds, cpu_seconds + 1)),
        ("RLIMIT_NPROC", (0, 0)),  # no fork/clone: the parser has no reason to start anything
        ("RLIMIT_FSIZE", (0, 0)),  # no file writes (pipes are not affected)
        ("RLIMIT_CORE", (0, 0)),
    ]
    applied: list[str] = []
    for name, limits in wanted:
        which = getattr(res, name, None)
        if which is None:
            continue
        try:
            res.setrlimit(which, limits)
        except (ValueError, OSError):  # pragma: no cover - a platform without this limit
            continue
        applied.append(name)
    return applied


def load_target(spec: str) -> Callable[[bytes], ParsedReport]:
    if not _TARGET_SPEC.fullmatch(spec):
        raise ValueError("bad target")
    module_name, _, attribute = spec.partition(":")
    module = importlib.import_module(module_name)
    target: Callable[[bytes], ParsedReport] = getattr(module, attribute)
    return target


def main(argv: list[str]) -> int:
    answer = sys.stdout.buffer
    sys.stdout = sys.stderr
    try:
        memory_mb, cpu_seconds, spec = int(argv[0]), int(argv[1]), argv[2]
        target = load_target(spec)
    except Exception:
        answer.write(wire.encode_error(MSG_CRASH))
        answer.flush()
        return 0
    data = sys.stdin.buffer.read()
    apply_limits(memory_mb, cpu_seconds)
    answer.write(run_target(target, data))
    answer.flush()
    return 0
