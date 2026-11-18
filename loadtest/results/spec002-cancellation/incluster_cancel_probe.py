# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""In-pod disconnect-cancellation probe, Qwen3.

WHY THIS EXISTS. tests/smoke/probe_cancellation.sh polls the metrics endpoint through a
`kubectl exec` round trip, which costs about 2.6 s per sample. That is COARSER than the effect:
the published Llama figure is 0.55 to 0.86 s, so the shell probe can only ever report "freed
within one sample" and its own header says the warn tier is inoperative for that reason. This
probe runs INSIDE the serving pod and polls locally, so the sampling floor is the poll interval
rather than an API round trip.

METHOD, per cell: confirm the gauge is at baseline, start a long streaming generation, wait until
the engine reports it running, let it generate, then ABRUPTLY CLOSE THE SOCKET (no abort call,
which vLLM 0.24.0 gates behind a flag) and poll `vllm:num_requests_running` until it returns to
baseline. The reported number is disconnect to slot-free.
"""
import json, socket, threading, time, urllib.request

MODEL = "Qwen/Qwen3-8B"
POLL = 0.02          # 20 ms sampling floor, against the shell probe's ~2.6 s
GEN_BEFORE_CUT = 3.0
CELLS = 4

def running():
    # Fixed http://localhost URL inside the vLLM pod: the request never leaves the pod.
    raw = urllib.request.urlopen("http://localhost:8000/metrics", timeout=5).read().decode()  # nosec B310  # nosemgrep
    for line in raw.splitlines():
        if line.startswith("vllm:num_requests_running") and not line.startswith("#"):
            return float(line.rsplit(" ", 1)[1])
    return 0.0

def wait_for(pred, limit=60.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        if pred(running()):
            return time.time() - t0
        # Deliberate fixed wait in a measurement or test script: poll interval inside wait_for, bounded by the time limit.
        time.sleep(POLL)  # nosemgrep
    return None

def cell(i):
    if wait_for(lambda v: v == 0) is None:
        return {"cell": i, "error": "gauge never reached baseline"}
    body = json.dumps({"model": MODEL, "prompt": "Write an exhaustive essay on distributed systems.",
                       "max_tokens": 4000, "temperature": 0.0, "ignore_eos": True,
                       "stream": True}).encode()
    # Raw socket so the connection can be severed mid-stream without a graceful close.
    s = socket.create_connection(("127.0.0.1", 8000), timeout=10)
    s.sendall(b"POST /v1/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
              + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    stop = threading.Event()
    def drain():
        try:
            while not stop.is_set():
                if not s.recv(65536): break
        except Exception:
            pass
    t = threading.Thread(target=drain, daemon=True); t.start()
    if wait_for(lambda v: v >= 1) is None:
        s.close(); return {"cell": i, "error": "engine never reported the request running"}
    # Deliberate fixed wait in a measurement or test script: GEN_BEFORE_CUT is the generation time before the disconnect, the variable this probe measures.
    time.sleep(GEN_BEFORE_CUT)  # nosemgrep
    busy_before = running()
    stop.set()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")  # RST
    s.close()
    t_cut = time.time()
    freed = wait_for(lambda v: v == 0, limit=30.0)
    return {"cell": i, "busy_before_cut": busy_before,
            "seconds_to_slot_free": round(time.time() - t_cut, 3) if freed is not None else None,
            "baseline_restored": freed is not None}

out = {"model": MODEL, "poll_interval_s": POLL, "cells": []}
for i in range(1, CELLS + 1):
    r = cell(i); out["cells"].append(r)
    print("  cell %d: busy_before=%s  DISCONNECT -> SLOT FREE in %s s  baseline_restored=%s"
          % (i, r.get("busy_before_cut"), r.get("seconds_to_slot_free"), r.get("baseline_restored")))
    # Deliberate fixed wait in a measurement or test script: space the cells so one cannot affect the next.
    time.sleep(2)  # nosemgrep
vals = [c["seconds_to_slot_free"] for c in out["cells"] if c.get("seconds_to_slot_free") is not None]
if vals:
    out["summary"] = {"n": len(vals), "min": min(vals), "max": max(vals),
                      "mean": round(sum(vals)/len(vals), 3)}
    print("SUMMARY n=%d min=%.3f max=%.3f mean=%.3f s (Llama published: 0.55 to 0.86 s, 4 of 4)"
          % (len(vals), min(vals), max(vals), sum(vals)/len(vals)))
print("JSON " + json.dumps(out))
