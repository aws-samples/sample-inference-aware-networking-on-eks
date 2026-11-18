# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Base image from ECR Public (a byte-identical mirror of the Docker Official Image), pinned by
# digest so a rebuild gets the scanned bytes. The tag moves; the digest does not. Pinned in
# versions.yaml (worker_base_image) and enforced by tests/test_addon_pins.sh.
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
# DEBIAN SECURITY UPDATES THE BASE IMAGE DOES NOT YET CARRY, pinned to exact versions so a rebuild
# installs the scanned packages (release candidate 7). Trivy 0.76.1 on 2026-11-07 reported these as
# the only High findings in the base with a released fix: CVE-2026-85091 and CVE-2026-76844 in
# zlib1g, CVE-2026-109518 in apt and libapt-pkg7.0. Remove this step once worker_base_image moves to
# a digest that already includes them; rerun Trivy whenever it changes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade \
      zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 apt=3.0.4 libapt-pkg7.0=3.0.4 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# One image, multiple entrypoints sharing jobstore.py (the claim/key/state logic):
#   worker.py       (consumer, default CMD)
#   submit_api.py   (producer, args ["submit_api.py"])
#   sweeper.py      (orphan reconciler CronJob, args ["sweeper.py"])
#   sqs_exporter.py (SQS depth Prometheus exporter, args ["sqs_exporter.py"])
#   relay_server.py (interactive front door and promotion actor, args ["relay_server.py"])
#
# ONE IMAGE RATHER THAN TWO, and it is a deliberate choice at the Phase 2 cutover. A separate relay
# image would have added a second provenance chain, a second pin for INV15 to keep in agreement, and
# a second build to keep reproducible, in exchange for nothing: the relay's code already lives in
# this directory and shares no runtime dependency the others lack. The relay differs by its command,
# not by its contents.
COPY jobkey.py jobstore.py worker.py submit_api.py sweeper.py sqs_exporter.py \
     guards.py relay.py promotion.py relay_server.py message_contract.py http_bounds.py ./
# Runs as non-root
RUN useradd -m worker
USER worker
ENTRYPOINT ["python"]
CMD ["worker.py"]
