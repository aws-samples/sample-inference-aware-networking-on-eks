# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
LT-3 exactly-once re-assertion from DURABLE state.

The original headline proof counted GPU runs from a pod-local signal (worker log lines /
increase(worker_claims_total)), which this project has since proven lossy across worker
scale-down. This re-assertion counts GPU executions from the durable per-job DynamoDB
claim_count, which survives worker churn, exactly as the reconciliation pass established.

For a run of N distinct jobs, each submitted DUP times (at-least-once duplicate delivery),
the exactly-once property is:
  1. distinct jobs that reached status=complete == N.
  2. sum(claim_count) over those jobs == N (one GPU execution per distinct job).
  3. no single job has claim_count > 1 (no distinct job ran twice; non-interrupted run).

A claim_count of exactly N with none exceeding 1 is the durable, worker-churn-proof statement
of "duplicate submissions ran exactly one GPU job each".

Run (submit-api port-forwarded):
  AWS_PROFILE=eksinference ./.venv/bin/python tests/validation/lt3_durable.py --tag <run-tag> --n <N>
"""
import argparse

import boto3

TABLE = "inference-jobs"
REGION = "us-west-2"


def scan_run(tag):
    ddb = boto3.client("dynamodb", region_name=REGION)
    prefix = tag if tag.endswith("#") else tag + "#"
    distinct = complete = claims = 0
    max_cc = 0
    over = []
    kwargs = dict(TableName=TABLE, FilterExpression="begins_with(job_id, :p)",
                  ExpressionAttributeValues={":p": {"S": prefix}})
    while True:
        page = ddb.scan(**kwargs)
        for it in page.get("Items", []):
            distinct += 1
            if it.get("status", {}).get("S") == "complete":
                complete += 1
            cc = int(it.get("claim_count", {}).get("N", "0"))
            claims += cc
            max_cc = max(max_cc, cc)
            if cc > 1:
                over.append((it["job_id"]["S"], cc))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return distinct, complete, claims, max_cc, over


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--n", type=int, required=True, help="distinct jobs submitted")
    args = ap.parse_args()

    distinct, complete, claims, max_cc, over = scan_run(args.tag)
    print(f"LT-3 durable re-assertion for run {args.tag} (N={args.n})")
    print(f"  distinct jobs in DynamoDB : {distinct}")
    print(f"  reached status=complete   : {complete}")
    print(f"  sum(claim_count)          : {claims}")
    print(f"  max(claim_count)          : {max_cc}")

    fails = 0
    def check(name, ok, detail):
        nonlocal fails
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            fails += 1

    check("distinct completed == N", complete == args.n, f"{complete} == {args.n}")
    check("sum(claim_count) == N (one GPU execution per distinct job)", claims == args.n,
          f"{claims} == {args.n}")
    check("no job ran more than once (max claim_count <= 1)", max_cc <= 1,
          f"max={max_cc}" + (f", offenders={over[:5]}" if over else ""))
    print(f"=> {fails} assertion(s) failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
