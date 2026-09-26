"""CPU contention around the opening burst, read straight from the kernel.

Written to answer one question: is the racer's second vCPU helping? The race is
decided by whether each burst member reaches the wire at its planned instant,
and the things that can make one late are all CPU: our own threads parsing
550-700KB refusals, Chrome running the parked page's timers in the same
container, and - with a one-vCPU quota - the kernel throttling the container
once that quota is spent. Each of those leaves a trace here:

* **run-queue delay** (``/proc/self/task/*/schedstat``) - time our own threads
  were runnable but waiting for a CPU. The most direct measure there is: with
  enough CPUs it stays near zero.
* **CPU pressure** (PSI, ``cpu.pressure``) - time some task in the container was
  stalled waiting for a CPU, Chrome's included.
* **throttling** (cgroup ``cpu.stat``) - periods in which the cgroup's quota ran
  out and everything in it was paused.
* **container vs process CPU** - how much of the container's CPU was not ours.

Every reader is best-effort. Cloud Run's layout differs between execution
environments and none of this may ever be the reason a booking fails, so a
file that is missing or unreadable reads as "not measured" (None).
"""

import os
import time as time_module
from dataclasses import dataclass
from typing import Any

_CGROUP_V2_DIR = "/sys/fs/cgroup"
_CGROUP_V1_CPU_DIR = "/sys/fs/cgroup/cpu"
_CGROUP_V1_CPUACCT_USAGE = (
    "/sys/fs/cgroup/cpuacct/cpuacct.usage",
    "/sys/fs/cgroup/cpu/cpuacct.usage",
)
_SYSTEM_CPU_PRESSURE = "/proc/pressure/cpu"
_OWN_TASKS_DIR = "/proc/self/task"


def _read(path: str) -> str | None:
    """A small kernel file's contents, or None when it cannot be read."""
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def parse_cpu_stat(text: str) -> dict[str, int]:
    """A cgroup ``cpu.stat`` file as ``{key: int}``; unparseable lines are skipped."""
    stats: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(" ")
        try:
            stats[key] = int(value)
        except ValueError:
            continue
    return stats


def parse_cpu_max(text: str) -> float | None:
    """A cgroup v2 ``cpu.max`` ("200000 100000") as a CPU count; None when unlimited."""
    parts = text.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return quota / period if period > 0 else None


def parse_cfs_quota(quota_text: str, period_text: str) -> float | None:
    """A cgroup v1 CFS quota and period as a CPU count; None when unlimited (-1)."""
    try:
        quota, period = int(quota_text.strip()), int(period_text.strip())
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def parse_psi_some_total_us(text: str) -> int | None:
    """The cumulative "some" stall time from a PSI file, in microseconds."""
    for line in text.splitlines():
        if not line.startswith("some "):
            continue
        for field in line.split():
            if field.startswith("total="):
                try:
                    return int(field[len("total=") :])
                except ValueError:
                    return None
    return None


def parse_schedstat_run_delay_ns(text: str) -> int | None:
    """A task's ``schedstat`` ("cpu_ns run_delay_ns timeslices") -> run delay in ns."""
    parts = text.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


@dataclass(frozen=True)
class CpuSnapshot:
    """Cumulative CPU counters at one instant; see :func:`cpu_delta`."""

    wall_s: float
    process_cpu_s: float
    cgroup_usage_us: int | None
    nr_periods: int | None
    nr_throttled: int | None
    throttled_us: int | None
    psi_some_us: int | None
    run_delay_ns: int | None
    threads: int | None


def _cgroup_cpu_stat() -> tuple[int | None, int | None, int | None, int | None]:
    """Container CPU usage and throttling counters: usage_us, periods, throttled, throttled_us."""
    v2 = _read(f"{_CGROUP_V2_DIR}/cpu.stat")
    if v2 is not None:
        stats = parse_cpu_stat(v2)
        return (
            stats.get("usage_usec"),
            stats.get("nr_periods"),
            stats.get("nr_throttled"),
            stats.get("throttled_usec"),
        )
    usage_us: int | None = None
    for path in _CGROUP_V1_CPUACCT_USAGE:
        raw = _read(path)
        if raw is not None:
            try:
                usage_us = int(raw.strip()) // 1000
            except ValueError:
                usage_us = None
            break
    v1 = _read(f"{_CGROUP_V1_CPU_DIR}/cpu.stat")
    if v1 is None:
        return usage_us, None, None, None
    stats = parse_cpu_stat(v1)
    throttled_ns = stats.get("throttled_time")
    return (
        usage_us,
        stats.get("nr_periods"),
        stats.get("nr_throttled"),
        None if throttled_ns is None else throttled_ns // 1000,
    )


def _cpu_pressure_us() -> int | None:
    """Cumulative CPU "some" stall time: the container's own if exposed, else the system's."""
    for path in (f"{_CGROUP_V2_DIR}/cpu.pressure", _SYSTEM_CPU_PRESSURE):
        raw = _read(path)
        if raw is not None:
            total = parse_psi_some_total_us(raw)
            if total is not None:
                return total
    return None


def _own_run_delay_ns() -> tuple[int | None, int | None]:
    """Summed run-queue delay of this process's live threads, and how many were read."""
    try:
        tasks = os.listdir(_OWN_TASKS_DIR)
    except OSError:
        return None, None
    total = 0
    read = 0
    for task in tasks:
        raw = _read(f"{_OWN_TASKS_DIR}/{task}/schedstat")
        if raw is None:
            continue
        delay = parse_schedstat_run_delay_ns(raw)
        if delay is None:
            continue
        total += delay
        read += 1
    return (total, read) if read else (None, None)


def take_snapshot() -> CpuSnapshot:
    """Read every counter once. A few file reads - about a millisecond."""
    usage_us, periods, throttled, throttled_us = _cgroup_cpu_stat()
    run_delay_ns, threads = _own_run_delay_ns()
    return CpuSnapshot(
        wall_s=time_module.monotonic(),
        process_cpu_s=time_module.process_time(),
        cgroup_usage_us=usage_us,
        nr_periods=periods,
        nr_throttled=throttled,
        throttled_us=throttled_us,
        psi_some_us=_cpu_pressure_us(),
        run_delay_ns=run_delay_ns,
        threads=threads,
    )


def _diff(after: int | None, before: int | None) -> int | None:
    """``after - before`` when both were read, else None."""
    if after is None or before is None:
        return None
    return after - before


def cpu_delta(before: CpuSnapshot, after: CpuSnapshot) -> dict[str, Any]:
    """What the CPU did between two snapshots, as ledger-ready fields.

    ``busyCpus`` is container CPU over wall time: 1.0 means one CPU's worth of
    work was being done on average across the span. Run-queue delay counts the
    threads alive at ``after`` - a burst's worker threads are, and their whole
    lifetime falls inside the span, so their delay is counted in full.
    """
    wall_ms = (after.wall_s - before.wall_s) * 1000
    container_us = _diff(after.cgroup_usage_us, before.cgroup_usage_us)
    throttled_us = _diff(after.throttled_us, before.throttled_us)
    pressure_us = _diff(after.psi_some_us, before.psi_some_us)
    run_delay_ns = _diff(after.run_delay_ns, before.run_delay_ns)
    return {
        "wallMs": round(wall_ms, 1),
        "processCpuMs": round((after.process_cpu_s - before.process_cpu_s) * 1000, 1),
        "containerCpuMs": None if container_us is None else round(container_us / 1000, 1),
        "busyCpus": (
            None
            if container_us is None or wall_ms <= 0
            else round(container_us / 1000 / wall_ms, 2)
        ),
        "throttledPeriods": _diff(after.nr_throttled, before.nr_throttled),
        "throttledMs": None if throttled_us is None else round(throttled_us / 1000, 1),
        "cpuPressureMs": None if pressure_us is None else round(pressure_us / 1000, 1),
        "runQueueDelayMs": None if run_delay_ns is None else round(run_delay_ns / 1e6, 2),
        "threads": after.threads,
    }


def cpu_environment() -> dict[str, Any]:
    """How many CPUs this container can actually use, three ways.

    ``visibleCpus`` is what the kernel reports, ``usableCpus`` what this process
    may be scheduled on, and ``cgroupCpuLimit`` the quota the cgroup enforces -
    which on some runtimes is the only one of the three that reflects the
    configured vCPU count. Logged once per race, so a morning's metrics can be
    read against the CPU it actually had.
    """
    usable: int | None
    try:
        usable = len(os.sched_getaffinity(0))  # type: ignore[attr-defined,unused-ignore]
    except (AttributeError, OSError):
        usable = None
    limit: float | None = None
    v2 = _read(f"{_CGROUP_V2_DIR}/cpu.max")
    if v2 is not None:
        limit = parse_cpu_max(v2)
    else:
        quota = _read(f"{_CGROUP_V1_CPU_DIR}/cpu.cfs_quota_us")
        period = _read(f"{_CGROUP_V1_CPU_DIR}/cpu.cfs_period_us")
        if quota is not None and period is not None:
            limit = parse_cfs_quota(quota, period)
    return {
        "visibleCpus": os.cpu_count(),
        "usableCpus": usable,
        "cgroupCpuLimit": limit,
    }
