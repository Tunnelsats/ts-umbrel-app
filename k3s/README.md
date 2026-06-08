# TunnelSats on k3s / Kubernetes

These manifests deploy TunnelSats v3 in `k3s` mode alongside an existing
LND (and/or CLN) node running in the same cluster.

```sh
# Deploy everything into the namespace where your Lightning node lives:
kubectl apply -k k3s/ --namespace=<your-namespace>

# Remove everything again (safe — see "Uninstall" below):
kubectl delete -k k3s/ --namespace=<your-namespace>
```

> [!IMPORTANT]
> The two most common setup failures are **namespace/RBAC** and **PVC mount
> paths**. Read the two sections below carefully — they cover the errors you are
> most likely to hit on a first install.

## 1. Namespace & RBAC

TunnelSats discovers and restarts your LND/CLN pod through the Kubernetes API. To
do that, its ServiceAccount needs `get`/`list`/`delete` on `pods` **in the
namespace where LND/CLN run** (`role.yaml` + `rolebinding.yaml`).

These are **namespaced** Role/RoleBinding objects, so pay attention to *where*
they land:

- The `Role` and `RoleBinding` must exist in the **same namespace as the
  LND/CLN pods**.
- The `RoleBinding`'s `subjects[].namespace` must point to the namespace where
  the TunnelSats **ServiceAccount** actually lives.

Applying with `kubectl apply -k k3s/ --namespace=<ns>` rewrites all of this for
you (resource namespaces *and* the ServiceAccount subject), so prefer it over
applying the files individually with `-f`.

**Symptom of getting this wrong** — pod lookups return HTTP 403 and TunnelSats
falls back to the Service ClusterIP, and "Configure Node" cannot find the pod:

```
k8s pod lookup failed (selector=app=lnd, ns=<ns>): 403 Client Error: Forbidden
Could not resolve LND pod IP, falling back to ClusterIP ...
LND container not found. Skipping configuration.
```

A 403 (not 401) means the token authenticates fine but the ServiceAccount lacks
RBAC permission. Verify with:

```sh
kubectl auth can-i list pods \
  --as=system:serviceaccount:<ns>:tunnelsats -n <ns>   # must print "yes"
```

If LND/CLN live in a **different** namespace than TunnelSats, also set
`LND_K8S_NAMESPACE` / `CLN_K8S_NAMESPACE` in `deployment.yaml`, and copy
`role.yaml` + `rolebinding.yaml` into that namespace (set the RoleBinding
subject namespace to wherever the ServiceAccount lives).

## 2. PVC mount paths (do not move them)

The server reads and writes the Lightning config at **fixed, hard-coded paths**:

| Node | Path inside the container        |
| ---- | -------------------------------- |
| LND  | `/lightning-data/lnd/lnd.conf`   |
| CLN  | `/lightning-data/cln/config`     |

So the `volumeMounts[].mountPath` in `deployment.yaml` **must** stay at
`/lightning-data/lnd` (and `/lightning-data/cln`). Adapt your storage to fit
these paths — **do not** change the mountPath to match your storage:

- Point `volumes[].persistentVolumeClaim.claimName` at the PVC that holds your
  Lightning data directory.
- If your PVC root has a nested `lnd/` (i.e. the config is at
  `<pvc-root>/lnd/lnd.conf`), uncomment `subPath: lnd` on the mount instead of
  changing the mountPath.

**Symptom of getting this wrong** — "Configure Node" returns HTTP 500 and the UI
shows *"Failed to modify LND config"*:

```
Error writing /lightning-data/lnd/lnd.conf for configure:
[Errno 2] No such file or directory: '/lightning-data/lnd/.lnd.conf.tmp.<hex>'
```

`Errno 2` (No such file or directory) means the config directory is not where
the server expects it — the PVC is mounted at the wrong path. Confirm with:

```sh
ls -l /lightning-data/lnd/lnd.conf   # must exist inside the tunnelsats pod
```

(`Errno 13`, *Permission denied*, would instead point at file ownership/mode —
the config and its directory must be readable/writable by uid/gid 1000.)

## Uninstall

`kubectl delete -k k3s/ --namespace=<ns>` removes only what these manifests
create: the ServiceAccount, Role, RoleBinding, Service, Deployment, and the
`tunnelsats-data` PVC. Your **LND/CLN PVC is never touched** — it is only
*referenced* by `deployment.yaml`, not managed by this kustomization — so your
`lnd.conf` and channel state are safe.

Verify a clean removal:

```sh
kubectl get all,pvc,sa,role,rolebinding -n <ns> | grep tunnelsats   # empty
```