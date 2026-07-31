from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT_PATH = REPO_ROOT / "k3s" / "deployment.yaml"


def load_deployment():
    with DEPLOYMENT_PATH.open(encoding="utf-8") as manifest:
        return yaml.safe_load(manifest)


def test_k3s_manifest_requires_lnd_colocation_by_default():
    deployment = load_deployment()
    pod_spec = deployment["spec"]["template"]["spec"]
    required_terms = pod_spec["affinity"]["podAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]

    assert len(required_terms) == 1
    term = required_terms[0]
    assert term["labelSelector"]["matchLabels"] == {"app": "lnd"}
    assert term["topologyKey"] == "kubernetes.io/hostname"
    # Omitting namespaces makes affinity use the Deployment's own namespace,
    # matching the default LND_K8S_NAMESPACE=K8S_NAMESPACE configuration.
    assert "namespaces" not in term
    assert "namespaceSelector" not in term


def test_k3s_manifest_exposes_tunnelsats_node_name_to_runtime():
    deployment = load_deployment()
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry for entry in container["env"]}

    assert env["TUNNELSATS_K8S_NODE_NAME"]["valueFrom"]["fieldRef"] == {
        "fieldPath": "spec.nodeName"
    }
    assert env["LND_K8S_POD_SELECTOR"]["value"] == "app=lnd"
