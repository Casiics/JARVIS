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
    "egress-private": {"net-mcp-private", "net-private-assistant", "net-egress"},
    "n8n-private": {"net-mcp-private"},
    "opa-private": {"net-mcp-private"},
    "mcp-gateway-private": {"net-private", "net-mcp-private"},
    "private-assistant": {"net-private-assistant", "net-inference-private"},
    "audit-keeper": {"net-core"},
    "pki-ca": {"net-pki"},
    "pki-init": set(),
    "pki-issue": {"net-pki"},
    "ollama": {"net-inference", "net-inference-private"},
    # Host network, bound to the tailnet address only; see test_deck_edge_*.
    "edge-caddy": set(),
}

DOMAIN_OF_NETWORK = {
    "net-core": "core",
    "net-pki": "core",
    "net-trading": "trading",
    "net-mcp-trading": "trading",
    "net-content": "content",
    "net-mcp-content": "content",
    "net-sandbox-egress": "content",
    "net-private": "private",
    "net-mcp-private": "private",
    "net-private-assistant": "private",
    "net-inference-private": "private",
}

# Isolation services added in Phase 4 run with a read-only root and no capabilities.
HARDENED = [
    s
    for s in EXPECTED_NETWORKS
    if s.startswith(("egress-", "n8n-", "opa-", "mcp-gateway-", "pki-", "edge-"))
    or s in {"private-assistant", "audit-keeper"}
]


def networks_of(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks", {})
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
    assert spec.get("user"), f"{service} must set its user"
    if service != "pki-issue":  # root with only CHOWN, see the compose comment
        assert not str(spec["user"]).startswith("0"), f"{service} must not run as root"
    # pki-ca and edge-caddy: their binaries need this one capability to exec; pki-issue hands
    # certificates to their consumers' users. See the compose comments.
    expected = {"pki-ca": ["NET_BIND_SERVICE"], "edge-caddy": ["NET_BIND_SERVICE"]}
    expected["pki-issue"] = ["CHOWN"]
    assert spec.get("cap_add", []) == expected.get(service, [])


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


def test_private_assistant_reaches_only_its_stream_proxy_and_ollama(
    compose: dict[str, Any],
) -> None:
    service = compose["services"]["private-assistant"]
    proxy = compose["services"]["egress-private"]["networks"]["net-private-assistant"]
    assert "HTTPS_PROXY" not in service["environment"]
    assert service["extra_hosts"]
    assert all(entry.endswith(f":{proxy['ipv4_address']}") for entry in service["extra_hosts"])
    assert service["environment"]["JARVIS_OLLAMA_URL"] == "http://ollama:11434"
    on_inference = {
        n for n, s in compose["services"].items() if "net-inference-private" in networks_of(s)
    }
    assert on_inference == {"private-assistant", "ollama"}
    on_assistant_net = {
        n for n, s in compose["services"].items() if "net-private-assistant" in networks_of(s)
    }
    assert on_assistant_net == {"private-assistant", "egress-private"}


def test_private_assistant_reads_secrets_where_the_provider_looks(
    compose: dict[str, Any],
) -> None:
    service = compose["services"]["private-assistant"]
    assert service["profiles"] == ["tools"], "one-shot command, never started by `up`"
    for secret in service["secrets"]:
        key = secret["source"].removeprefix("private_")
        assert secret["target"] == f"/run/jarvis/secrets/private/{key}"


def test_pki_init_has_no_network() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    assert compose["services"]["pki-init"]["network_mode"] == "none"


def test_only_the_issuer_joins_the_ca_and_writes_certificates(compose: dict[str, Any]) -> None:
    on_pki = {n for n, s in compose["services"].items() if "net-pki" in networks_of(s)}
    assert on_pki == {"pki-ca", "pki-issue"}
    for name, service in compose["services"].items():
        for volume in service.get("volumes", []):
            if str(volume).startswith("pki_ca_data:"):
                assert name in {"pki-ca", "pki-init"} or str(volume).endswith(":ro"), name
            if str(volume).startswith("certs_"):
                assert name == "pki-issue", f"{name} mounts {volume}"
            if isinstance(volume, dict) and str(volume.get("source")).startswith("certs_"):
                assert name != "pki-issue"
                assert volume["read_only"] is True, name
                # One identity's directory, never the whole domain volume.
                assert volume["volume"]["subpath"], name
    issued = {
        v.split(":")[0]: v.split(":")[1]
        for v in compose["services"]["pki-issue"]["volumes"]
        if v.startswith("certs_")
    }
    assert issued == {
        f"certs_{d}": f"/certs/{d}" for d in ("core", "trading", "content", "private")
    }


def test_identities_stay_in_their_domain() -> None:
    identities = (COMPOSE.parent / "pki/identities").read_text().splitlines()
    domains = {line.split()[0] for line in identities if line and not line.startswith("#")}
    assert domains <= {"core", "trading", "content", "private"}


def test_audit_keeper_reads_its_key_where_the_provider_looks(compose: dict[str, Any]) -> None:
    secrets = compose["services"]["audit-keeper"]["secrets"]
    key = next(s for s in secrets if s["source"] == "core_audit_signing_key")
    assert key["target"] == "/run/jarvis/secrets/core/audit_signing_key"


EDGE_CADDYFILE = COMPOSE.parent / "edge/Caddyfile"


def test_only_the_deck_edge_uses_the_host_network(compose: dict[str, Any]) -> None:
    on_host = {n for n, s in compose["services"].items() if s.get("network_mode") == "host"}
    assert on_host == {"edge-caddy"}
    assert "ports" not in compose["services"]["edge-caddy"]


def test_deck_edge_refuses_to_start_without_a_tailnet_address(compose: dict[str, Any]) -> None:
    script = " ".join(compose["services"]["edge-caddy"]["command"])
    assert 'case "$$JARVIS_TAILNET_IP" in 100.*)' in script
    assert "exit 1" in script
    assert script.rstrip().endswith("--adapter caddyfile")


def test_deck_edge_terminates_tls13_on_the_tailnet_only() -> None:
    lines = [
        line.strip()
        for line in EDGE_CADDYFILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "admin off" in lines
    assert "bind {$JARVIS_TAILNET_IP}" in lines
    assert "protocols tls1.3" in lines
    assert "tls /certs/deck.crt /certs/deck.key {" in lines
    assert any(line.startswith("Strict-Transport-Security") for line in lines)
    # The upstream is the gateway on the host's loopback, nothing else.
    upstreams = [line for line in lines if line.startswith("reverse_proxy")]
    assert upstreams == ["reverse_proxy 127.0.0.1:{$JARVIS_DECK_UPSTREAM_PORT:8080} {"]


IDENTITIES = COMPOSE.parent / "pki/identities"


def _identities() -> list[list[str]]:
    lines = IDENTITIES.read_text().splitlines()
    return [line.split() for line in lines if line.strip() and not line.startswith("#")]


@pytest.mark.parametrize("domain", ["trading", "content", "private"])
def test_mcp_gateways_mount_only_their_own_certificate(
    compose: dict[str, Any], domain: str
) -> None:
    service = compose["services"][f"mcp-gateway-{domain}"]
    mounts = [v for v in service["volumes"] if isinstance(v, dict)]
    assert mounts == [
        {
            "type": "volume",
            "source": f"certs_{domain}",
            "target": "/certs",
            "read_only": True,
            "volume": {"subpath": "mcp-gateway"},
        }
    ]
    uid = str(service["user"]).split(":")[0]
    assert [domain, "mcp-gateway", uid, f"mcp-gateway-{domain}"] in _identities()


def test_identities_are_unique_and_well_formed() -> None:
    identities = _identities()
    assert len({(i[0], i[1]) for i in identities}) == len(identities)
    for identity in identities:
        assert 3 <= len(identity) <= 4, identity
        assert identity[2].isdigit(), identity


def test_mcp_gateways_require_client_certificates() -> None:
    envoy = yaml.safe_load((COMPOSE.parent / "mcp-gateway/envoy.yaml").read_text())
    [listener] = envoy["static_resources"]["listeners"]
    [chain] = listener["filter_chains"]
    tls = chain["transport_socket"]["typed_config"]
    assert tls["require_client_certificate"] is True
    context = tls["common_tls_context"]
    assert context["tls_params"]["tls_minimum_protocol_version"] == "TLSv1_3"
    sans = context["combined_validation_context"]["default_validation_context"]
    assert sans["match_typed_subject_alt_names"] == [
        {"san_type": "URI", "matcher": {"prefix": "spiffe://jarvis.home/"}}
    ]
