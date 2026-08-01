"""
Measure waldengolf.com server clock offset and network RTT.

Stdlib-only (no project deps needed): python scripts/measure_server_clock.py

Why: the booking bot clicks Reserve at 6:30:00.000 *local* clock, but the race
is decided by the *server's* clock. If the server runs N ms ahead/behind, or
our request takes RTT/2 ms to arrive, a "perfectly timed" click can land late.

Technique: HTTP Date headers have 1-second resolution, so a single sample only
bounds the offset to +/-1s. Instead we poll rapidly and watch for the moment
the Date header ticks over to the next second. At that transition the server
clock is at X.000s (+/- poll interval), which lets us estimate sub-second
offset:

    offset = server_tick_time - local_midpoint_time

where local_midpoint_time = (t_send + t_recv) / 2 of the sample that first
observed the new second. Positive offset = server clock is ahead of ours.

Accuracy is roughly +/-(poll_interval + RTT/2). With 50ms polling and ~50ms
RTT that's ~75ms - not NTP-grade, but enough to know whether we're firing
tens or hundreds of milliseconds off target.
"""

import argparse
import email.utils
import http.client
import ssl
import statistics
import sys
import time

DEFAULT_HOST = "www.waldengolf.com"


def sample_date_header(
    conn: http.client.HTTPSConnection, path: str = "/"
) -> tuple[float, float, float]:
    """Send one request; return (t_send, t_recv, server_date_epoch)."""
    t_send = time.time()
    conn.request("HEAD", path)
    resp = conn.getresponse()
    t_recv = time.time()
    resp.read()  # drain so the connection can be reused
    date_header = resp.getheader("Date")
    if date_header is None:
        raise RuntimeError(f"No Date header in response (status {resp.status})")
    server_epoch = email.utils.parsedate_to_datetime(date_header).timestamp()
    return t_send, t_recv, server_epoch


def measure(host: str, samples: int, poll_interval: float) -> None:
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, timeout=10, context=ctx)

    print(f"Measuring against https://{host} ...")

    # Warm up the connection (TLS handshake) so RTT samples reflect steady state.
    try:
        sample_date_header(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"HEAD request failed ({exc}); retrying with a fresh connection")
        conn = http.client.HTTPSConnection(host, timeout=10, context=ctx)
        sample_date_header(conn)

    rtts: list[float] = []
    transitions: list[float] = []  # offset estimates, one per observed tick
    coarse_offsets: list[float] = []
    last_server_epoch: float | None = None

    for _ in range(samples):
        try:
            t_send, t_recv, server_epoch = sample_date_header(conn)
        except (http.client.HTTPException, OSError):
            conn.close()
            conn = http.client.HTTPSConnection(host, timeout=10, context=ctx)
            continue
        rtt = t_recv - t_send
        midpoint = (t_send + t_recv) / 2
        rtts.append(rtt)
        coarse_offsets.append(server_epoch - midpoint)

        if last_server_epoch is not None and server_epoch > last_server_epoch:
            # Server second just ticked: server clock was ~server_epoch.000 now.
            transitions.append(server_epoch - midpoint)
        last_server_epoch = server_epoch

        time.sleep(poll_interval)

    conn.close()

    if not rtts:
        print("All requests failed; cannot measure.")
        sys.exit(1)

    rtt_min = min(rtts) * 1000
    rtt_med = statistics.median(rtts) * 1000
    print(f"\nRequests: {len(rtts)}  (poll interval {poll_interval * 1000:.0f}ms)")
    print(f"RTT: min {rtt_min:.0f}ms, median {rtt_med:.0f}ms, max {max(rtts) * 1000:.0f}ms")
    print("One-way latency (~RTT/2): " f"~{rtt_min / 2:.0f}ms best case")

    coarse = statistics.median(coarse_offsets)
    print(
        f"\nCoarse offset (Date - local midpoint, 1s resolution): {coarse * 1000:+.0f}ms"
        "\n  (positive = server clock ahead of local clock)"
    )

    if transitions:
        est = statistics.median(transitions)
        spread = (max(transitions) - min(transitions)) * 1000 if len(transitions) > 1 else 0.0
        print(
            f"Tick-transition offset estimate ({len(transitions)} ticks observed): "
            f"{est * 1000:+.0f}ms  (spread {spread:.0f}ms)"
        )
        fire_early_ms = est * 1000 + rtt_min / 2
        print(
            "\nTo have a request ARRIVE when the server clock reads T, "
            f"send at local T {'-' if fire_early_ms >= 0 else '+'} "
            f"{abs(fire_early_ms):.0f}ms."
        )
    else:
        print(
            "No second-boundary transitions observed - increase --samples or "
            "decrease --interval for a sub-second estimate."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--samples", type=int, default=80)
    parser.add_argument("--interval", type=float, default=0.05, help="poll interval seconds")
    args = parser.parse_args()
    measure(args.host, args.samples, args.interval)
