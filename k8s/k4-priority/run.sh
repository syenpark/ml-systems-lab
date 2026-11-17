#!/usr/bin/env bash
# PriorityClass + preemption lab.  Usage: ./run.sh   |   ./run.sh clean
set -euo pipefail
cd "$(dirname "$0")"

if [[ "${1:-}" == "clean" ]]; then
  kubectl delete ns k4-prio --ignore-not-found
  # PriorityClasses are cluster-scoped, so deleting the namespace doesn't remove them
  kubectl delete priorityclass low high --ignore-not-found
  exit 0
fi

echo "== Step 0: node CPU — allocatable vs already requested"
echo "   Predict first: how much CPU is free? (size filler.yaml to ~90% of it)"
kubectl describe node | sed -n '/^Allocatable/,/memory/p;/Allocated resources/,/memory/p'

echo
echo "== Step 1: PriorityClasses + 2 low-priority fillers"
echo "   Predict first: do both fillers fit? How much CPU is left after them?"
kubectl apply -f priorityclasses.yaml -f filler.yaml
kubectl -n k4-prio rollout status deploy/filler --timeout=60s
kubectl -n k4-prio get pods -o wide

echo
echo "== Step 2: high-priority vip (1500m, more than what's left)"
echo "   Predict first: does vip run? Which Pod is evicted, and what happens to it after?"
kubectl apply -f vip.yaml
sleep 15
kubectl -n k4-prio get pods -o wide
kubectl -n k4-prio get events --sort-by=.lastTimestamp | grep -Ei 'preempt|FailedScheduling|Scheduled' || true
