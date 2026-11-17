# Kubernetes Lab — Workloads, Scheduling, and Resource Isolation

Local Kubernetes lab using **kind + Podman** on an M2 MacBook.

The labs follow one question from the view of a shared ML cluster: *why does (or doesn't) my workload end up as a running Pod?* They start with workload types (Job vs Deployment), then move through scheduling (requests, taints) to multi-team resource isolation (namespaces, quotas).

## Contents

- [Start the Cluster](#start-the-cluster)
- [Kind vs Object](#kind-vs-object)
- [Pod & Namespace](#pod--namespace)
- [Useful `kubectl` Concepts](#useful-kubectl-concepts)
- [Job Lab](#job-lab)
- [Deployment Lab](#deployment-lab)
- [Job vs Deployment](#job-vs-deployment)
- [Job Running vs Pod Running](#job-running-vs-pod-running)
- [Service Discovery and DNS Lab](#service-discovery-and-dns-lab)
- [CrashLoopBackOff](#crashloopbackoff)
- [Resource requests](#resource-requests)
- [Admission Failure vs Scheduling Failure](#admission-failure-vs-scheduling-failure)
- [LimitRange: default requests/limits (K4 part 1)](#limitrange-default-requestslimits-k4-part-1)
- [PriorityClass and preemption (K4 part 2)](#priorityclass-and-preemption-k4-part-2)
- [Who reads which field (kubectl apply → traffic)](#who-reads-which-field-kubectl-apply--traffic)

## Start the Cluster

```bash
podman machine start
podman start mle-lab-control-plane

kubectl config use-context kind-mle-lab
kubectl get nodes
```

## Kind vs Object

*What is an "object" in Kubernetes?* One instance of a **kind**, stored in the cluster (e.g. the Pod named `web`). A **kind** is the type (Pod, Deployment, Job, Namespace, Node…).

```cpp
class Pod { std::string name, image; };  // kind   = class
Pod web{"web", "nginx"};                 // object = instance
```

```text
apply / create        → make an object
get / describe        → read it
delete                → remove it
pods "web" not found  → that object is gone; the kind Pod still exists
```

Namespace and Node are objects too, but cluster-wide (they don't belong to any namespace).

## Pod & Namespace

*Does a namespace decide where a Pod runs?* No — it's only a name scope.

A **namespace** is a logical group of objects for **names**: a name must be unique only inside its namespace (`k1-demo/web` and `default/web` can both exist). It is independent of nodes and, on its own, limits no resources.

```bash
kubectl create namespace k1-demo
kubectl run web --image=nginx:alpine -n k1-demo
kubectl run web --image=nginx:alpine                # same name, default namespace
kubectl get pods -A -o wide | grep web              # NAMESPACE differs, NODE is the same
```

```text
cluster
├── Nodes       (machines, shared by every namespace)
└── Namespaces  (name scopes, like C++ namespaces)
a Pod = one namespace + one node — two independent labels
```

### describe vs logs

```bash
kubectl describe pod web -n k1-demo   # K8s's view: Events (scheduled, pulled, started, restarts)
kubectl logs web -n k1-demo           # the app's view: stdout/stderr (--previous after a restart)
kubectl delete pod web -n k1-demo     # a Pod created directly has no owner → it does not come back
```

So what:
- Image typo / stuck Pod → `describe` (ErrImagePull, ImagePullBackOff); app crash → `logs`.

## Useful `kubectl` Concepts

```text
kubectl <verb> <kind> <name> -n <namespace> [flags]
```

| verb | what it does |
| ---- | ------------ |
| `get` | list objects |
| `describe` | details + Events (Kubernetes' view) |
| `logs` | the app's stdout (the app's view) |
| `create` / `apply` | make objects (`create` = imperative, `apply` = from yaml) |
| `delete` | remove objects |
| `expose` | create a Service in front of a Deployment ("expose" = make it reachable) |
| `run` | start a single bare Pod |

Don't memorize flags — `kubectl <verb> --help`, `kubectl explain <kind>.spec`.

```bash
kubectl get jobs       # Controller/workload state
kubectl get pods       # Execution state
kubectl get pods -w    # Watch Pod state changes
```

```text
Controller → Pod → Container → Process
```

A **controller** is a loop that compares the desired state (the yaml spec) with the actual state and fixes the gap. A **Pod** is the execution unit, running containers, each a process.

```cpp
while (true) {
    int want = spec.replicas, have = count_running_pods();
    if (have < want) create_pod();
    if (have > want) delete_pod();
}
```

```text
Deployment ──owns──> ReplicaSet ──owns──> Pods   keep N running forever  (servers, e.g. inference)
Job        ──owns──> Pod                         run until success, stop (e.g. a training run)
bare Pod   (no owner)                            deleted = gone          (see Pod & Namespace)
```

So what:
- Delete 1 Pod of a `replicas: 3` Deployment → the **ReplicaSet** controller sees 2 < 3 and creates a **new** Pod (new name). The deleted one is not revived. The Deployment only manages ReplicaSets (rollouts). Restart behavior: see [Job vs Deployment](#job-vs-deployment).
- Node lost → no instant replacement: its Pods are replaced only after they are evicted (~5 min by default).

### Resource relationships: everything centers on the Pod

| Kind | What it is | Relationship to Pod | Analogy |
| ---- | ---------- | ------------------- | ------- |
| **Pod** | The actual unit of execution | This is the real workload: a Pod runs the process/container | One employee doing the actual work |
| **Deployment** | A controller that manages ReplicaSets (rollouts) | Its ReplicaSet creates and maintains Pods according to the desired replica count | HR/recruiting manager that hires and supervises employees |
| **Job** | A workload controller for finite tasks | Creates Pods to run a batch job until completion | Project manager assigning a one-time task |
| **Namespace** | A logical boundary/folder for objects | Every Pod must live inside a Namespace | A C++ namespace: a name scope, not a place |
| **ResourceQuota** | Namespace-level total budget | Limits the combined resource requests of all Pods in the Namespace | A department's office budget cap |
| **LimitRange** | Default/min/max resource policy for a Namespace | Fills in missing resource values for Pods; enforces minimum and maximum constraints | Default equipment rules for new employees |
| **PriorityClass** | A named priority level with an integer value | Pods reference the PriorityClass by name to indicate scheduling priority | VIP tier / priority label |

`--context` selects which Kubernetes cluster/context to use. Once `kind-mle-lab` is the current context, it can be omitted.

<details>
<summary>Scheduler — taints, tolerations &amp; failure reasons</summary>

### Scheduler

The scheduler is the control-plane component that decides which node a Pending Pod runs on:

```text
Job/Deployment creates a Pod → Pod has no node → Scheduler evaluates each node:
    taint without a matching toleration?  → rejected
    insufficient CPU / memory / GPU?       → rejected
    other placement constraints unmet?     → rejected
    all satisfied?                         → assign Pod to node
no node qualifies → Pod stays Pending (FailedScheduling)
```

#### Pod vs Node: what to inspect

```text
POD                              NODE
Demand                           Supply
- CPU / memory / GPU request     - CPU / memory / GPU allocatable
Permission                       Restriction
- tolerations                    - taints
```

```bash
kubectl describe pod <pod>        # why was the Pod not scheduled?
kubectl get pod <pod> -o yaml     # what does the Pod request/tolerate?
kubectl describe node <node>      # what can the Node provide/restrict?
```

#### `taint` and `toleration`

```text
NODE                              POD
🔒 taint                           🔑 toleration
workload=reserved:NoSchedule      workload=reserved:NoSchedule
        └────────── matches ───────────────┘
                    ↓
       This taint does NOT block this Pod
```

A **taint** on a node says "do not schedule Pods onto me unless they tolerate this"; a **toleration** on a Pod says "I am allowed through this specific block". **Taint blocks. Toleration unblocks that specific taint — it removes the barrier, but does not schedule the Pod by itself**; CPU/memory/GPU fit is still checked afterward.

```bash
kubectl taint node <node> workload=reserved:NoSchedule
kubectl describe node <node>
```

#### Resource failure vs taint failure

```bash
0/4 nodes are available:
2 Insufficient nvidia.com/gpu
2 node(s) had untolerated taint {workload: reserved}
```

```text
2 nodes: Pod asks for GPU → not enough schedulable GPU → Insufficient nvidia.com/gpu  (resource-fit failure)
2 nodes: node has workload=reserved taint, Pod lacks toleration → untolerated taint   (scheduling-policy failure)
```

Neither means the training application started and then failed.

</details>

---

## Job Lab

Generate:

```bash
kubectl create job finite-job \
  --image=busybox:1.36 \
  --dry-run=client \
  -o yaml \
  -- sh -c 'echo "job started"; sleep 5; echo "job finished"' \
  > job.yaml
```

Relevant configuration:

```yaml
spec:
  template:
    spec:
      restartPolicy: Never
```

Apply and observe:

```bash
kubectl apply -f job.yaml
kubectl get jobs
kubectl get pods -w
```

Expected: `Running → Completed`. A successful process exits `0`, satisfying the Job's completion requirement.

---

## Deployment Lab

Generate:

```bash
kubectl create deployment finite-deployment \
  --image=busybox:1.36 \
  --dry-run=client \
  -o yaml \
  > deployment.yaml
```

Amend the container:

```yaml
spec:
  replicas: 1
  template:
    spec:
      restartPolicy: Always
      containers:
        - name: busybox
          image: busybox:1.36
          command:
            - sh
            - -c
            - |
              echo "started"
              sleep 5
              echo "finished"
```

Apply and observe:

```bash
kubectl apply -f deployment.yaml
kubectl get deployments
kubectl get pods -w
```

Observed: `Running → Completed → restarted → Running → Completed → CrashLoopBackOff → restarted...`

The process exits successfully, but the Deployment expects a continuously running replica.

Observed (k1-demo): delete one Pod of a `replicas: 3` Deployment → a Pod with a **new** name appears (AGE 0s); `kubectl delete` waits until the old Pod is gone, so `Terminating` may not show.

---

## Job vs Deployment

```text
Job:         finite process exits 0 → completion satisfied → no restart
Deployment:  finite process exits 0 → restartPolicy: Always → restarted → repeated termination → backoff
```

Use a **Job** for finite workloads such as training/batch processing. Use a **Deployment** for continuously running workloads such as inference services.

---

## Job Running vs Pod Running

**Pod Running** = the process is alive (kubelet's view). **Job Running** = the controller has not yet observed completion (Job controller's view).

```text
Scheduler      Can the Pod be placed?     → Pending / Scheduled
Container      Is the process alive?      → Running / Terminated
Application    Is training progressing?   → epochs / checkpoints
```

Kubernetes only observes the first two layers — it does not understand your training loop.

| Job       | Pod                     | Meaning / next step                                                    |
| --------- | ----------------------- | ---------------------------------------------------------------------- |
| Running   | Running                 | Normal — verify the app is progressing (logs, epochs, GPU utilization) |
| Running   | Pending                 | Scheduling problem — check resources, taints, GPU (see Scheduler)      |
| Running   | Running, logs stuck, GPU idle | Application stuck (deadlock, DDP rank waiting, dataloader, infinite loop) — the process never exits, so the Job stays Running |
| Completed | Completed               | Process exited `0`, completion satisfied                               |

Mental model: Kubernetes may report everything healthy while the ML workload is not making progress. When both Job and Pod are Running, ask layer 3: *is the application actually advancing?*

---

## Service Discovery and DNS Lab

Inference Pods are disposable: their IPs change after a restart or rollout. A **Service** gives clients a stable endpoint; a **Deployment** keeps the desired inference Pods running.

```text
client Pod → http://inference-service:80 → Service (stable ClusterIP)
    → EndpointSlice (current Ready Pod IPs) → inference Pod A / B
```

### Label vs selector

*How does a Service know which Pods to send to?* By label, not by name.

```text
Deployment template  → stamps label app=web on every Pod it creates (new Pods too)
Pod name             → web-5d6b5c7df9-59fdh  (changes when replaced)
Pod label            → app=web               (same on every replica)
Service selector     → app=web  → "send to Pods wearing this label"
```

```bash
kubectl get pods -n k1-demo --show-labels
kubectl describe svc web -n k1-demo     # Selector, ClusterIP, Endpoints = current Pod IPs
```

So what:
- A replaced Pod gets a new name and IP but the same label → the Service picks it up automatically.
- ClusterIP is reachable only inside the cluster; clients use the Service NAME (cluster DNS).

### Client vs server (and where ClusterIP reaches)

*Who is the client here, and who is the server?*
client = whoever SENDS the request; server = whoever ANSWERS it.
Inference Pods are servers (backends), not clients.

```text
┌──────────────────────── cluster ─────────────────────────┐
│                                                          │
│  client Pod                Service (ClusterIP)           │
│  (busybox tmp /     ──▶   "web" 10.96.x.x       ──┬──▶  Pod A  (server)
│   another app)            stable name + IP        ├──▶  Pod B  (server)
│                           selector: app=web       └──▶  Pod C  (server)
│                                                          │
└──────────────────────────────────────────────────────────┘
        ✗ outside the cluster (laptop, external user) cannot reach a ClusterIP
          → needs NodePort / LoadBalancer / Ingress (later)
```

```bash
kubectl run tmp --rm -it --restart=Never --image=busybox -n k1-demo -- wget -qO- http://web
#  tmp = client (inside the cluster) · web = Service · nginx Pods = servers
```

So what:
- A ClusterIP Service is reachable only from inside the cluster, so its clients must be Pods in the same cluster.
- Clients use the Service NAME; they never need to know which server Pod answers.

<details>
<summary>Hands-on: create, test, and debug Service discovery</summary>

Create two backend Pods, expose them, and confirm the Service tracks their IPs:

```bash
kubectl create deployment inference --image=nginx:alpine --replicas=2
kubectl expose deployment inference --name=inference-service --port=80 --target-port=80

kubectl get service inference-service
kubectl get endpointslice -l kubernetes.io/service-name=inference-service
```

`kubectl expose` creates a Service; it does not create Pods. Kubernetes matches the Service selector to ready Pod labels and records their IPs in EndpointSlices.

Test discovery from a temporary client, then delete a backend Pod and confirm the client is unaffected:

```bash
kubectl run client --image=busybox:1.36 --restart=Never --command -- sleep 3600
kubectl exec client -- nslookup inference-service    # resolves to the Service's ClusterIP, not a Pod IP
kubectl exec client -- wget -qO- http://inference-service

kubectl delete pod <inference-pod>                   # its ReplicaSet replaces it, likely with a new IP
kubectl get endpointslice -l kubernetes.io/service-name=inference-service
kubectl exec client -- wget -qO- http://inference-service   # unchanged — client never saw the IP change
```

When a Service has no endpoints, trace the request path and inspect selector vs labels:

```text
Client → DNS → Service → selector → EndpointSlice → backend Pods
```

```bash
kubectl get service inference-service -o yaml     # selector
kubectl get pods --show-labels                    # do Pod labels actually match it?
```

</details>

---

## CrashLoopBackOff

`CrashLoopBackOff` does **not necessarily mean the application crashed** — it means the container repeatedly terminates after Kubernetes restarts it, so Kubernetes applies increasing delay before the next attempt.

```bash
kubectl describe pod <pod-name>     # check Last State: Terminated
kubectl logs <pod-name> --previous  # the previous container's logs
```

| Reason | Exit Code | Meaning |
| --- | --- | --- |
| `Completed` | 0 | successful termination |
| `Error` | 1 | application failure |
| `OOMKilled` | 137 | killed for exceeding memory |

```text
CrashLoopBackOff = "container keeps terminating" ≠ "application definitely crashed"
    ↓
kubectl describe pod + kubectl logs --previous → Reason + Exit Code + logs
```

## Resource requests

```bash
kubectl run impossible-request \
  --image=busybox:1.36 \
  --restart=Never \
  --dry-run=client -o yaml > resource-demo.yaml
```

Amend [./resource-demo.yaml](./resource-demo.yaml) with a resources block the node can't satisfy:

```yaml
spec:
  containers:
    - name: impossible-request
      image: busybox:1.36
      command: ["sh", "-c", "sleep 300"]
      resources:
        requests:
          cpu: "6"
          memory: "4Gi"
        limits:
          cpu: "6"
          memory: "4Gi"
```

`requests` are what the scheduler uses for placement; `limits` are what the kubelet enforces at runtime — setting both equal gives guaranteed QoS.

* CPU can be overcommitted: the scheduler places Pods by `requests`, but summed `limits` may exceed node capacity, since CPU is time-shareable.
* GPUs normally cannot: a device plugin advertises discrete resources like `nvidia.com/gpu: 1`, and the scheduler never double-allocates one — requests must equal limits unless time-slicing/MIG/vGPU is explicitly configured.

```bash
kubectl apply -f resource-demo.yaml
kubectl describe pod impossible-request
```

```text
Warning  FailedScheduling  0/1 nodes are available: 1 Insufficient cpu.
```

Use the same [Pod-vs-Node inspection](#pod-vs-node-what-to-inspect) as the Scheduler section above to read it:

```text
Pod Pending → kubectl describe pod → read FailedScheduling reason
   resource problem? → compare Pod requests vs Node allocatable
   taint problem?    → compare Node taint vs Pod toleration
```

---

## Admission Failure vs Scheduling Failure

On a shared ML cluster, each team usually gets its own **Namespace** with a **ResourceQuota** — a check *before* the scheduler, so a workload can be blocked in two different places.

* **Namespace** — a named scope every Pod belongs to. Without `-n`, `kubectl` uses `default`.
* **ResourceQuota** — a budget for one namespace: the **sum** of `requests` across all its Pods (plus object counts) may not exceed `hard`.

```text
Namespace team-a
├── ResourceQuota team-a-quota   (requests.cpu ≤ 1, requests.memory ≤ 1Gi, pods ≤ 4)
├── Pod p1  requests.cpu=600m    ← counted against the quota
└── Pod p2  requests.cpu=600m    ← would make the sum 1200m > 1 → rejected
```

### Lab

```bash
kubectl create namespace team-a
kubectl create quota team-a-quota -n team-a \
  --hard=requests.cpu=1,requests.memory=1Gi,pods=4
kubectl describe quota -n team-a
```

Generate a Pod with requests (once a quota sets `requests.cpu`/`requests.memory`, every Pod in the namespace must declare them, unless a LimitRange fills in defaults):

```bash
kubectl run p1 -n team-a --image=busybox:1.36 --restart=Never \
  --dry-run=client -o yaml -- sleep 600 > quota-pod.yaml
```

```yaml
    resources:
      requests:
        cpu: 600m
        memory: 64Mi
```

Apply `p1`, then the same spec as `p2`:

```bash
kubectl apply -f quota-pod.yaml
sed 's/name: p1/name: p2/' quota-pod.yaml | kubectl apply -f -
kubectl get pods -n team-a
```

### Observed

```text
Error from server (NotFound): error when creating "pod.yaml": namespaces "team-a" not found

Error from server (Forbidden): error when creating "STDIN": pods "p2" is forbidden:
exceeded quota: team-a-quota, requested: requests.cpu=600m,
used: requests.cpu=600m, limited: requests.cpu=1
```

`requested` (600m) + `used` (600m) = 1200m > `limited` (1). `p2` never appears in `kubectl get pods` — the API server refused to create it. Compare with [Resource requests](#resource-requests): `impossible-request` passed admission (no quota in `default`), was created, and stayed `Pending` because no node could fit it.

### Mental model

```text
kubectl apply
    ↓
API server — admission: does the namespace exist? does the quota have room?
    ↓ no  → Forbidden / NotFound — Pod object is NEVER created
    ↓ yes
Pod created (no node yet)
    ↓
Scheduler — placement: requests vs node allocatable, taints/tolerations
    ↓ no  → Pod exists but stays Pending (FailedScheduling)
    ↓ yes
kubelet starts the container → Running
```

| | Admission failure | Scheduling failure |
| --- | --- | --- |
| Question | *Is this team allowed to use it?* (policy) | *Is there physically room?* (capacity) |
| Decided by | API server (ResourceQuota admission) | kube-scheduler |
| Pod object | Never created | Exists, `Pending` |
| Where to look | `kubectl describe job/deployment` → `FailedCreate`, then `kubectl describe quota` | `kubectl describe pod` → `FailedScheduling` |

The two are independent: a team can have quota left while every node is full (Pending), or an empty cluster can still reject a team that spent its budget (Forbidden). When a Job/Deployment creates the Pod, note that a quota rejection lands on the **controller** (`FailedCreate`), not on a Pod — there is no Pod to `describe`.

For an ML platform, this is how GPU budgets per research team are enforced — e.g. `requests.nvidia.com/gpu: "8"` in a team's ResourceQuota caps that team at 8 GPUs regardless of how many are free in the cluster.

---

## LimitRange: default requests/limits (K4 part 1)

*Why is a Pod without resources rejected in a namespace with a ResourceQuota, and how does LimitRange fix it?*

```bash
# from k8s/ — namespace k1-demo, quota hard requests.cpu: "1"
kubectl apply -f k4-limitrange/quota.yaml
kubectl -n k1-demo run nolimit --image=nginx
# → Error from server (Forbidden)   (quota can't count a Pod with no requests)

kubectl apply -f k4-limitrange/limitrange.yaml   # defaultRequest 100m/64Mi, default 200m/128Mi
kubectl -n k1-demo run nolimit --image=nginx
kubectl -n k1-demo get pod nolimit -o jsonpath='{.spec.containers[0].resources}'
# → {"limits":{"cpu":"200m","memory":"128Mi"},"requests":{"cpu":"100m","memory":"64Mi"}}
```

[k4-limitrange/limitrange.yaml](./k4-limitrange/limitrange.yaml):

```yaml
apiVersion: v1
kind: LimitRange
metadata:
  name: defaults
  namespace: k1-demo
spec:
  limits:
  - type: Container
    defaultRequest: {cpu: 100m, memory: 64Mi}   # filled into empty requests
    default: {cpu: 200m, memory: 128Mi}         # filled into empty limits
    # max: {cpu: 500m, memory: 256Mi}           # optional: reject containers above this
```

What you see: the same `kubectl run` goes from Forbidden to created, and the Pod spec now carries requests/limits I never wrote — the API server filled them in.

```text
Pod (no resources) → API server
   ① LimitRange    : fill empty requests/limits (+ min/max check)   ← mutating, first
   ② ResourceQuota : sum of requests in namespace ≤ hard ?          ← validating, then
   → over: Forbidden (Pod never created) · ok: stored → scheduler
```

Worked example — quota 1 CPU, 0.7 already requested, 4 Pods with no requests:

```text
LimitRange fills 100m each →  0.8 ✓   0.9 ✓   1.0 ✓   1.1 ✗ (Forbidden: exceeded quota)
→ 3 created
```

| Object | Scope | What it does |
|---|---|---|
| ResourceQuota | whole namespace | caps the SUM (e.g. requests.cpu) |
| LimitRange | each container | fills defaults, enforces min/max |
| requests / limits | in the Pod spec | the Pod's own declaration |

| | requests | limits |
|---|---|---|
| read by | scheduler (allocatable − already requested) and quota | kernel cgroups at runtime |
| if exceeded | Pending (scheduler) / Forbidden (quota) | CPU: throttled · memory: OOMKilled |

So what:
- LimitRange removes the "no requests" rejection, but not the "over the total" rejection.

---

## PriorityClass and preemption (K4 part 2)

*When the node is full, can a more important Pod still run — and what can priority NOT do?*

```bash
cd k8s/k4-priority
./run.sh         # step 0: free CPU · step 1: 2 low fillers · step 2: high vip
./run.sh clean   # deletes the namespace AND the cluster-scoped PriorityClasses
```

[k4-priority/](./k4-priority/): PriorityClasses `low` (100) and `high` (1000); Deployment `filler` (2 × `low`, sized to ~90% of free node CPU); Pod `vip` (`high`, asks for more than is left).

What you see: vip runs, one filler is evicted, and its replacement (a new name) stays Pending.

```text
before vip:  filler-a  Running · filler-b  Running
after vip:   vip       Running
             filler-b  Running
             filler-c  Pending   ← NEW name: the ReplicaSet controller replaced the evicted filler-a
```

```text
vip (priority 1000) Pending ── scheduler: no node has room
   → would evicting lower-priority Pods make room on a node?  yes
   → evict filler-a (priority 100) → place vip
filler-a gone → ReplicaSet controller: replicas 2 vs actual 1 → creates filler-c
filler-c (priority 100) → scheduler: no room, and it can't evict vip (higher) → Pending
```

| Who | Reads | Result |
|---|---|---|
| API server (admission) | priorityClassName → PriorityClass value | Pod gets a priority number |
| scheduler | requests vs node room, priority | higher priority goes first; may preempt LOWER-priority Pods |
| ReplicaSet controller | replicas vs actual Pods | creates the replacement Pod (new name) |

Order of checks for each Pod (first → last):

```text
0 ReplicaSet ctrl sends create
1 auth + schema
2 LimitRange      (fill)
3 PriorityClass   (name → number)
4 ResourceQuota   (sum ≤ hard, else Forbidden)
5 stored in etcd, Pending (no node)
6 scheduler       (requests, priority, preemption; else Pending)
7 kubelet         (image, limits, probes)
8 endpoints + kube-proxy
```

Precision checks (things I got wrong at first):
- The SCHEDULER doesn't create Pods; the ReplicaSet controller does. The scheduler only picks a node.
- Priority can't get past ResourceQuota. ResourceQuota is checked earlier, by the API server, so an over-quota Pod is Forbidden and never reaches the scheduler. Preemption frees NODE room, not quota.
- Equal priority → no preemption (the new Pod just stays Pending).

So what:
- PriorityClass decides who gets node room first and who can be evicted. It does nothing at admission, so ResourceQuota still applies to every Pod.

---

## Who reads which field (kubectl apply → traffic)

*After `kubectl apply`, which component reads which part of the YAML — and where does each failure show up?*

| Step | Who | Reads | If it fails |
|---|---|---|---|
| 1 | API server | apiVersion, kind, metadata, schema | kubectl error, nothing created |
| 2 | Deployment controller | replicas, selector, template, strategy | rollout stuck (`kubectl rollout status`) |
| 3 | ReplicaSet controller | replicas vs Pods matching selector; template | `FailedCreate` event on the ReplicaSet |
| 4 | API server: LimitRange | defaultRequest / default / max ↔ container resources | Forbidden if above max |
| 5 | API server: ResourceQuota | hard (e.g. requests.cpu) ↔ namespace sum | Forbidden; shows as `FailedCreate` on the ReplicaSet |
| 6 | scheduler | requests, priorityClassName, nodeSelector/affinity/taints ↔ node allocatable − requested | Pending, `FailedScheduling`; may preempt lower priority |
| 7 | kubelet | image, limits, livenessProbe, restartPolicy | ImagePullBackOff, CrashLoopBackOff, OOMKilled, CPU throttling |
| 8 | Endpoints controller + kube-proxy | Service selector ↔ Pod labels (ready only), ports | no endpoints → requests fail |

A deleted Pod is replaced by the ReplicaSet controller (new name); a crashed container is restarted by the kubelet (same name, RESTARTS +1).
