# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Does a client disconnect free the vLLM sequence slot? (design v6.1 section 5.3, ruling 1)

Pre-registration: loadtest/results/cancellation/PRE-REGISTRATION.md. Read it before changing
anything here; the bound and the predictions are fixed in advance.

Method. Drive a long generation through the FULL external path the relay will use (external
entry, Envoy Gateway, Endpoint Picker, vLLM) over a raw socket, so the close is under this
script's control and is not softened by a client library's connection pooling. Poll the serving
pod's own metrics endpoint directly rather than Prometheus, because the 15 s scrape interval is
coarser than the 5 s bound being tested.

Usage (the caller supplies the gateway host and a port-forward to the pod's 8000):
  GATEWAY=<elb-host> POD_METRICS=http://localhost:8000/metrics \
    ./.venv/bin/python loadtest/cancellation_probe.py
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request


def _urlopen_http(target, *args, **kwargs):
    """urllib.request.urlopen restricted to http and https URLs (bandit B310)."""
    import urllib.request
    url = getattr(target, "full_url", target)
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("refusing a non-HTTP URL: %r" % (url,))
    return urllib.request.urlopen(target, *args, **kwargs)  # nosec B310 - scheme checked above


GATEWAY = os.environ["GATEWAY"]
POD_METRICS = os.environ.get("POD_METRICS", "http://localhost:8000/metrics")
MODEL = os.environ.get("MODEL", "NousResearch/Meta-Llama-3.1-8B-Instruct")
BOUND_S = float(os.environ.get("BOUND_S", "5"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "1024"))   # ~60 s of generation, far beyond the bound
POLL_S = float(os.environ.get("POLL_S", "0.25"))

_samples: list[tuple[float, float, float]] = []   # (t, running, preemptions)
_stop = threading.Event()


def _scrape() -> tuple[float, float]:
    """Read num_requests_running and num_preemptions_total from the pod's own endpoint."""
    with _urlopen_http(POD_METRICS, timeout=5) as r:
        body = r.read().decode()
    running = preempt = 0.0
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith("vllm:num_requests_running"):
            running = float(line.rsplit(" ", 1)[1])
        elif line.startswith("vllm:num_preemptions_total"):
            preempt = float(line.rsplit(" ", 1)[1])
    return running, preempt


def _poller() -> None:
    while not _stop.is_set():
        try:
            r, p = _scrape()
            _samples.append((time.time(), r, p))
        except Exception:
            pass
        time.sleep(POLL_S)


def _latest() -> tuple[float, float]:
    return (_samples[-1][1], _samples[-1][2]) if _samples else (0.0, 0.0)


def _wait_for(pred, timeout: float) -> bool:
    """Wait until pred(running) holds on a FRESH sample, or timeout."""
    deadline = time.time() + timeout
    n = len(_samples)
    while time.time() < deadline:
        if len(_samples) > n:
            n = len(_samples)
            if pred(_samples[-1][1]):
                return True
        # Deliberate fixed wait in a measurement or test script: poll interval while waiting for a metric condition.
        time.sleep(0.05)  # nosemgrep
    return False


def run_cell(stream: bool, rep: int) -> dict:
    """One cell: establish a generation, disconnect abruptly, time the slot release."""
    base_run, base_pre = _latest()
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": "Write a long detailed essay about distributed systems."}],
        "max_tokens": MAX_TOKENS,
        "stream": stream,
    })
    req = (
        f"POST /v1/chat/completions HTTP/1.1\r\nHost: {GATEWAY}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n{body}"
    )
    s = socket.create_connection((GATEWAY, 80), timeout=30)
    s.sendall(req.encode())
    # For streaming, read a little so the response has genuinely started on the wire.
    first_byte_at = None
    if stream:
        s.settimeout(30)
        try:
            if s.recv(1):
                first_byte_at = time.time()
        except socket.timeout:
            pass

    started = _wait_for(lambda r: r > base_run, timeout=30)
    if not started:
        s.close()
        return {"stream": stream, "rep": rep, "error": "generation never registered as running",
                "baseline_running": base_run}

    peak_run, _ = _latest()
    # Abrupt disconnect. A plain close sends FIN, which is what a relay abandoning an attempt
    # would do; this is deliberately NOT an RST, so the result reflects the realistic case.
    t_close = time.time()
    s.close()

    freed = _wait_for(lambda r: r <= base_run, timeout=60)
    t_freed = time.time()
    end_run, end_pre = _latest()
    return {
        "stream": stream,
        "rep": rep,
        "baseline_running": base_run,
        "peak_running": peak_run,
        "first_byte_at_offset_s": (first_byte_at - t_close) if first_byte_at else None,
        "freed": freed,
        "free_time_s": round(t_freed - t_close, 3) if freed else None,
        "within_bound": bool(freed and (t_freed - t_close) <= BOUND_S),
        "preemptions_before": base_pre,
        "preemptions_after": end_pre,
        "preemptions_moved": end_pre != base_pre,
        "final_running": end_run,
    }


def main() -> int:
    threading.Thread(target=_poller, daemon=True).start()
    # Deliberate fixed wait in a measurement or test script: settle the engine baseline before the first cell.
    time.sleep(3)  # nosemgrep
    print(f"baseline running={_latest()[0]} preemptions={_latest()[1]}", flush=True)

    cells = []
    for stream in (False, True):
        for rep in (1, 2):
            label = "streaming" if stream else "non-streaming"
            print(f"-- cell {label} rep{rep}", flush=True)
            c = run_cell(stream, rep)
            cells.append(c)
            print(f"   {json.dumps(c)}", flush=True)
            # Let the engine settle so one cell cannot contaminate the next baseline.
            time.sleep(10)  # nosemgrep

    _stop.set()
    ok = [c for c in cells if c.get("within_bound")]
    verdict = "PASS" if len(ok) == 4 else "FAIL"
    times = [c["free_time_s"] for c in cells if c.get("free_time_s") is not None]
    out = {
        "verdict": verdict,
        "bound_s": BOUND_S,
        "cells_within_bound": len(ok),
        "cells_total": len(cells),
        "free_time_distribution_s": sorted(times),
        "free_time_min_s": min(times) if times else None,
        "free_time_max_s": max(times) if times else None,
        "any_preemption_moved": any(c.get("preemptions_moved") for c in cells),
        "poll_interval_s": POLL_S,
        "max_tokens": MAX_TOKENS,
        "gateway": GATEWAY,
        "cells": cells,
    }
    print(json.dumps(out, indent=2))
    with open("loadtest/results/cancellation/RESULT.json", "w") as f:
        json.dump(out, f, indent=2)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
