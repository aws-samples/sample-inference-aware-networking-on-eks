# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Direct SQS depth exporter. Polls the inference queue's ApproximateNumberOfMessages (visible,
waiting) and ApproximateNumberOfMessagesNotVisible (in flight, being processed) on a fixed
interval and exposes them as Prometheus gauges.

Why a direct exporter rather than the KEDA scaler metric: on this KEDA v2.20.1 install the
operator exposes only a gRPC metrics service (port 9666), and keda_scaler_metrics_value is not
available on any scrapeable Prometheus endpoint. KEDA also publishes only the scaler's target
metric (visible depth) and only while it is actively querying the scaler, so an idle queue reads
as a gap rather than a zero. This exporter reads the queue directly, always publishes (a real
zero, not a gap), and exposes BOTH visible and in-flight depth, which the reconciliation
(submitted minus claimed minus in-flight equals visible) requires. Its lag is one poll interval.

Read-only: it calls sqs:GetQueueAttributes and nothing else. It runs under the async-worker
Service Account, whose role already has GetQueueAttributes.

Environment:
    SQS_QUEUE_URL        the queue to observe
    SQS_POLL_INTERVAL    seconds between polls (default 15)
    METRICS_PORT         Prometheus scrape port (default 9101)
"""
import os
import time

import boto3
from prometheus_client import Gauge, start_http_server

SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
POLL_INTERVAL = int(os.environ.get("SQS_POLL_INTERVAL", "15"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9101"))
QUEUE = SQS_QUEUE_URL.rsplit("/", 1)[-1]

sqs = boto3.client("sqs")

VISIBLE = Gauge("sqs_queue_messages_visible", "Approximate visible (waiting) messages", ["queue"])
IN_FLIGHT = Gauge("sqs_queue_messages_in_flight", "Approximate not-visible (in-flight) messages", ["queue"])


def poll_once() -> None:
    a = sqs.get_queue_attributes(
        QueueUrl=SQS_QUEUE_URL,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    VISIBLE.labels(QUEUE).set(int(a["ApproximateNumberOfMessages"]))
    IN_FLIGHT.labels(QUEUE).set(int(a["ApproximateNumberOfMessagesNotVisible"]))


def main() -> None:
    start_http_server(METRICS_PORT)
    print(f"sqs-exporter up queue={QUEUE} interval={POLL_INTERVAL}s metrics=:{METRICS_PORT}", flush=True)
    while True:
        try:
            poll_once()
        except Exception as e:  # noqa: BLE001
            print(f"poll error: {e}", flush=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
