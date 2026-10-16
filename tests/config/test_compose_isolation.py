"""Static checks on docker/docker-compose.yml: the network and secret boundaries of each domain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).parents[2] / "docker/docker-compose.yml"

# Service -> the only network it may join. Domains never share a network.
EXPECTED_NETWORK = {
    "pg-audit": "net-core",
    "pg-core": "net-core",
    "redis-core": "net-core",
    "pg-trading": "net-trading",
    "pg-content": "net-content",
    "qdrant-content": "net-content",
    "pg-private": "net-private",
    "qdrant-private": "net-private",
    "redis-private": "net-private",
    "ollama": "net-inference",
}


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())
    return data


def test_every_service_is_known(compose: dict[str, Any]) -> None:
    assert set(compose["services"]) == set(EXPECTED_NETWORK)


@pytest.mark.parametrize("service", sorted(EXPECTED_NETWORK))
def test_service_joins_only_its_domain_network(compose: dict[str, Any], service: str) -> None:
    assert compose["services"][service]["networks"] == [EXPECTED_NETWORK[service]]


def test_all_networks_are_internal(compose: dict[str, Any]) -> None:
    for name, network in compose["networks"].items():
        assert network.get("internal") is True, f"{name} must be internal"


def test_no_host_ports(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        assert "ports" not in service, f"{name} publishes ports"


def test_images_are_pinned(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        image = service["image"]
        assert ":" in image and not image.endswith(":latest"), f"{name} image not pinned"


@pytest.mark.parametrize("service", sorted(EXPECTED_NETWORK))
def test_services_mount_only_their_domains_secrets(compose: dict[str, Any], service: str) -> None:
    domain = EXPECTED_NETWORK[service].removeprefix("net-")
    if service == "pg-audit":
        domain = "core"
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
