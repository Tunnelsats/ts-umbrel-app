from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT_PATH = REPO_ROOT / "k3s" / "deployment.yaml"
ROLE_PATH = REPO_ROOT / "k3s" / "role.yaml"
NETWORK_POLICY_PATH = REPO_ROOT / "k3s" / "networkpolicy.yaml"
KUSTOMIZATION_PATH = REPO_ROOT / "k3s" / "kustomization.yaml"


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
    assert env["K3S_BYPASS_CIDRS"]["value"] == "10.42.0.0/16,10.43.0.0/16"


def test_k3s_manifest_uses_optional_management_secret_and_explicit_hosts():
    deployment = load_deployment()
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry for entry in container["env"]}

    assert env["MANAGEMENT_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "tunnelsats-management",
        "key": "password",
        "optional": True,
    }
    assert "tunnelsats" in env["MANAGEMENT_ALLOWED_HOSTS"]["value"].split(",")


def test_k3s_management_ingress_denies_ordinary_pods():
    with NETWORK_POLICY_PATH.open(encoding="utf-8") as manifest:
        policy = yaml.safe_load(manifest)
    with KUSTOMIZATION_PATH.open(encoding="utf-8") as manifest:
        kustomization = yaml.safe_load(manifest)

    assert "networkpolicy.yaml" in kustomization["resources"]
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app": "tunnelsats"}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    ingress = policy["spec"]["ingress"]
    assert ingress == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {"tunnelsats.io/management-client": "true"}
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 9739}],
        }
    ]


def test_k3s_role_can_install_last_resort_network_policy():
    with ROLE_PATH.open(encoding="utf-8") as manifest:
        role = yaml.safe_load(manifest)

    rules = {
        (tuple(rule["apiGroups"]), tuple(rule["resources"])): set(rule["verbs"])
        for rule in role["rules"]
    }
    assert rules[(("networking.k8s.io",), ("networkpolicies",))] == {
        "get",
        "list",
        "create",
        "delete",
    }
