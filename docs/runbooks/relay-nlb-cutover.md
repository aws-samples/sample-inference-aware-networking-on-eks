# Cutting the relay from a Classic Load Balancer to a Network Load Balancer

    Date  : 2026-08-07
    Cost  : zero accelerator. A CPU-only cutover.
    Status: PREPARED. Run it while the cluster is parked.

## Why a runbook rather than an apply

The Amazon EKS user guide, on the NLB annotations:

> "Do not edit the annotations after creating your service. If you need to modify it, delete the
> service object and create it again with the desired value for this annotation."

So `kubectl apply` over an existing Classic-provisioned Service is **not** the migration. It edits
annotations on a live Service, which the platform does not support, and the observable result is a
Service that keeps its Classic Load Balancer while its manifest describes a Network one. **That is
this project's oldest defect shape: the manifest describing something other than what runs.**

The guide adds a second warning worth reading twice:

> "Attempting to replace existing Network Load Balancers created with the AWS cloud provider load
> balancer controller can result in multiple Network Load Balancers that might cause application
> downtime."

## What changes, and what must survive

| Property | Before (Classic) | After (NLB) | Survives? |
|---|---|---|---|
| Provisioner | in-tree cloud provider | AWS Load Balancer Controller | n/a |
| Targets | node ports | **pod IPs** | improvement |
| Source restriction | `spec.loadBalancerSourceRanges` | same field, honoured by the controller | **YES, unchanged** |
| Chain B's L (idle 690) | `aws-load-balancer-connection-idle-timeout` | `listener-attributes.TCP-80: tcp.idle_timeout.seconds=690` | **YES, by a different mechanism** |
| **External hostname** | RELAY-LB.example.com` | **a NEW hostname** | **NO. This is the breaking change.** |

**The hostname change is the whole reason this is a runbook.** Anything holding the old name (a
client config, a firewall allowlist, an evidence file quoting it) is stale the moment the cutover
completes.

## Preconditions

    make batch-open                       credentials live
    kubectl -n inference get deploy        the cluster is PARKED (no serving replicas)
    kubectl -n kube-system get deploy aws-load-balancer-controller

The third must exist. `make addons` installs it; `infra/addons/install-aws-load-balancer-controller.sh`
is idempotent and can be run alone.

**Run it parked.** With no serving replicas the external door leads nowhere, so removing it for a
minute costs nothing. Under traffic it is an outage.

## The cutover

    1.  kubectl -n inference get svc inference-relay -o yaml > /tmp/relay-svc-before.yaml
        Keep it. It is the record of what the Classic Service was, and the only copy once deleted.

    2.  kubectl -n inference delete svc inference-relay
        The Classic Load Balancer is deleted by the in-tree provider. Watch it go:
        aws elb describe-load-balancers --region us-west-2 --query 'length(LoadBalancerDescriptions)'

    3.  kubectl apply -f manifests/08-relay/relay.yaml
        The controller provisions a Network Load Balancer with IP targets.

    4.  kubectl -n inference get svc inference-relay -w
        Wait for EXTERNAL-IP. NLB provisioning is typically slower than Classic.

    5.  VERIFY IT IS ACTUALLY AN NLB, from the AWS side rather than from the hostname, because the
        DNS name does not distinguish the types and that is how the Classic one went unnoticed:
          aws elbv2 describe-load-balancers --region us-west-2 \
            --query "LoadBalancers[?DNSName=='<new hostname>'].[Type,Scheme]" --output text
        Expect: network  internal   (internet-facing before 2026-10-31; see the scheme annotation in manifests/08-relay/relay.yaml)
        And expect the Classic list to be EMPTY:
          aws elb describe-load-balancers --region us-west-2 --query 'length(LoadBalancerDescriptions)'

    6.  VERIFY CHAIN B's L REACHED THE PLATFORM. This is the step that cannot be skipped, because
        the permitted range of tcp.idle_timeout.seconds is UNVERIFIED in this repository (two AWS
        doc pages were fetched for it and neither rendered). If 690 was rejected, the listener
        carries a default and INV14's committed ordering is false in the data plane while true on
        paper:
          aws elbv2 describe-listeners --region us-west-2 --load-balancer-arn <arn> \
            --query 'Listeners[].ListenerArn' --output text
          aws elbv2 describe-listener-attributes --region us-west-2 --listener-arn <arn>
        Expect tcp.idle_timeout.seconds = 690. IF IT IS NOT 690, STOP AND ESCALATE: the chain
        needs re-deriving against whatever maximum the platform permits, and that is a decision
        about the promotion deadline, not a value to quietly accept.

    7.  Re-apply the source restriction if the operator's egress address has moved:
          curl -s https://checkip.amazonaws.com
          kubectl -n inference patch svc inference-relay --type=merge \
            -p '{"spec":{"loadBalancerSourceRanges":["<that address>/32"]}}'

    8.  make verify-invariants        INV12 (sole external exposure) and INV14 must both pass.

## After

- Record the new hostname in `BUILD-NOTES.md`. The old one appears in committed evidence files;
  those are historical records and are NOT edited, but the batch note should say the name changed
  and when, so a reader who finds the old name knows why it does not resolve.
- `docs/specs/PRE-PUBLICATION-SANITIZATION.md` already lists load balancer hostnames as a class, so
  the new one is covered without an edit.

## Rollback

Delete the Service and apply the pre-cutover copy from step 1. The in-tree provider recreates a
Classic Load Balancer, with a third hostname. **Rollback is not free and does not restore the
original name**, which is a reason to do the cutover parked and once.
