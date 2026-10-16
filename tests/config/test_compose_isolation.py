"""Static checks on docker/docker-compose.yml: the network and secret boundaries of each domain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).parents[2] / "docker/docker-compose.yml"

# Service -> the networks it joins. A service belongs to exactly one domain; it never joins
# another domain's network. Only egress proxies join net-egress, the one network with a route out.
EXPECTED_NETWORKS = {
    "pg-audit": {"net-core"},
    "pg-core": {"net-core"},
    "redis-core": {"net-core"},
    "egress-core": {"net-core", "net-egress"},
    "pg-trading": {"net-trading"},
    "egress-trading": {"net-mcp-trading", "net-egress"},
    "n8n-trading": {"net-mcp-trading"},
    "opa-trading": {"net-mcp-trading"},
    "mcp-gateway-trading": {"net-trading", "net-mcp-trading"},
    "pg-content": {"net-content"},
    "qdrant-content": {"net-content"},
    "egress-content": {"net-mcp-content", "net-egress"},
    "n8n-content": {"net-mcp-content"},
    "opa-content": {"net-mcp-content"},
    "mcp-gateway-content": {"net-content", "net-mcp-content"},
    "egress-reel": {"net-sandbox-egress", "net-egress"},
    "pg-private": {"net-private"},
    "qdrant-private": {"net-private"},
    "redis-private": {"net-private"},
    "egress-private": {"net-mcp-private", "net-egress"},
    "n8n-private": {"net-mcp-private"},
    "opa-private": {"net-mcp-private"},
    "mcp-gateway-private": {"net-private", "net-mcp-private"},
    "ollama": {"net-inference"},
}

DOMAIN_OF_NETWORK = {
    "net-core": "core",
    "net-trading": "trading",
    "net-mcp-trading": "trading",
    "net-content": "content",
    "net-mcp-content": "content",
    "net-sandbox-egress": "content",
    "net-private": "private",
    "net-mcp-private": "private",
}

# Isolation services added in Phase 4 run with a read-only root and no capabilities.
HARDENED = [
    s for s in EXPECTED_NETWORKS if s.startswith(("egress-", "n8n-", "opa-", "mcp-gateway-"))
]


def networks_of(service: dict[str, Any]) -> set[str]:
    networks = service["networks"]
    return set(networks) if isinstance(networks, dict | list) else set()


def domain_of(service_name: str) -> str:
    domains = {
        DOMAIN_OF_NETWORK[n] for n in EXPECTED_NETWORKS[service_name] if n in DOMAIN_OF_NETWORK
    }
    return domains.pop() if domains else "core"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())
    return data


def test_every_service_is_known(compose: dict[str, Any]) -> None:
    assert set(compose["services"]) == set(EXPECTED_NETWORKS)


@pytest.mark.parametrize("service", sorted(EXPECTED_NETWORKS))
def test_service_joins_only_its_expected_networks(compose: dict[str, Any], service: str) -> None:
    assert networks_of(compose["services"][service]) == EXPECTED_NETWORKS[service]


@pytest.mark.parametrize("service", sorted(EXPECTED_NETWORKS))
def test_service_stays_within_one_domain(service: str) -> None:
    domains = {DOMAIN_OF_NETWORK[n] for n in EXPECTED_NETWORKS[service] if n in DOMAIN_OF_NETWORK}
    assert len(domains) <= 1, f"{service} spans {domains}"


def test_all_networks_but_egress_are_internal(compose: dict[str, Any]) -> None:
    for name, network in compose["networks"].items():
        if name == "net-egress":
            assert not network.get("internal"), "net-egress is the route out"
            continue
        assert network.get("internal") is True, f"{name} must be internal"


def test_only_egress_proxies_join_net_egress(compose: dict[str, Any]) -> None:
    members = {n for n, s in compose["services"].items() if "net-egress" in networks_of(s)}
    assert members == {n for n in EXPECTED_NETWORKS if n.startswith("egress-")}


def test_agents_reach_mcp_servers_only_through_the_gateway(compose: dict[str, Any]) -> None:
    for domain in ("trading", "content", "private"):
        on_both = {
            name
            for name, service in compose["services"].items()
            if {f"net-{domain}", f"net-mcp-{domain}"} <= networks_of(service)
        }
        assert on_both == {f"mcp-gateway-{domain}"}


@pytest.mark.parametrize("service", HARDENED)
def test_isolation_services_are_hardened(compose: dict[str, Any], service: str) -> None:
    spec = compose["services"][service]
    assert spec.get("read_only") is True
    assert spec.get("cap_drop") == ["ALL"]
    assert "no-new-privileges:true" in spec.get("security_opt", [])
    assert spec.get("user"), f"{service} must not run as root"


@pytest.mark.parametrize("domain", ["trading", "content"])
def test_n8n_uses_its_own_domains_egress_proxy(compose: dict[str, Any], domain: str) -> None:
    env = compose["services"][f"n8n-{domain}"]["environment"]
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == f"http://egress-{domain}:3128"


def test_n8n_private_has_no_http_proxy_and_only_its_stream_proxy(compose: dict[str, Any]) -> None:
    service = compose["services"]["n8n-private"]
    assert "HTTPS_PROXY" not in service["environment"]
    proxy_ip = compose["services"]["egress-private"]["networks"]["net-mcp-private"]["ipv4_address"]
    assert all(entry.endswith(f":{proxy_ip}") for entry in service["extra_hosts"])


def test_n8n_is_locked_down(compose: dict[str, Any]) -> None:
    for domain in ("trading", "content", "private"):
        env = compose["services"][f"n8n-{domain}"]["environment"]
        assert env["N8N_PUBLIC_API_DISABLED"] == "true"
        assert env["N8N_DIAGNOSTICS_ENABLED"] == "false"
        assert env["N8N_COMMUNITY_PACKAGES_ENABLED"] == "false"
        assert env["N8N_BLOCK_ENV_ACCESS_IN_NODE"] == "true"
        assert "n8n-nodes-base.executeCommand" in env["NODES_EXCLUDE"]


@pytest.mark.parametrize("domain", ["trading", "content", "private"])
def test_each_opa_loads_only_its_domains_tools(compose: dict[str, Any], domain: str) -> None:
    volumes = compose["services"][f"opa-{domain}"]["volumes"]
    data = [v for v in volumes if v.endswith(":/policies/data.json:ro")]
    assert data == [f"../policies/mcp-tools/{domain}.json:/policies/data.json:ro"]


@pytest.mark.parametrize("domain", ["core", "trading", "content", "reel"])
def test_each_squid_loads_only_its_domains_allowlist(compose: dict[str, Any], domain: str) -> None:
    volumes = compose["services"][f"egress-{domain}"]["volumes"]
    allow = [v for v in volumes if v.endswith(":/etc/jarvis/egress.allow:ro")]
    assert allow == [f"../policies/egress/{domain}.allow:/etc/jarvis/egress.allow:ro"]


def test_no_host_ports(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        assert "ports" not in service, f"{name} publishes ports"


def test_images_are_pinned(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        image = service["image"]
        assert ":" in image and not image.endswith(":latest"), f"{name} image not pinned"


@pytest.mark.parametrize("service", sorted(EXPECTED_NETWORKS))
def test_services_mount_only_their_domains_secrets(compose: dict[str, Any], service: str) -> None:
    domain = domain_of(service)
    for secret in compose["services"][service].get("secrets", []):
        source = secret["source"]
        assert source.startswith(f"{domain}_"), f"{service} mounts {source}"
        assert f"/{domain}/" in compose["secrets"][source]["file"]


def test_no_plaintext_passwords_in_environment(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        for key, value in (service.get("environment") or {}).items():
            if "PASSWORD" in key or "KEY" in key:
                assert key.endswith("_FILE"), f"{name}.{key} must reference a secret file"
                assert str(value).startswith("/run/secrets/")
