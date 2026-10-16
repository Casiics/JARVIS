import pytest

from jarvis_core.contracts.classification import DataClassification, Domain
from jarvis_core.router.cache import cache_namespace
from jarvis_core.router.catalog import Endpoint, ModelClass
from jarvis_core.router.policy import RouteRequest, RoutingDenied, route

CLOUD = {ModelClass.SUPERVISOR: "cloud/supervisor", ModelClass.CODING: "cloud/coder"}


def _req(
    domain: Domain,
    label: str | None,
    *,
    model_class: ModelClass = ModelClass.CODING,
    prompt: str = "write a function",
    prefer_cloud: bool = False,
    pseudonymised: bool = False,
    context_tokens: int = 0,
) -> RouteRequest:
    return RouteRequest(
        caller_domain=domain,
        label=label,
        model_class=model_class,
        prompt=prompt,
        prefer_cloud=prefer_cloud,
        pseudonymised=pseudonymised,
        context_tokens=context_tokens,
    )


@pytest.mark.parametrize("label", ["public", "internal", "private", None, "garbage"])
@pytest.mark.parametrize("model_class", list(ModelClass))
def test_private_domain_never_reaches_cloud(label: str | None, model_class: ModelClass) -> None:
    result = route(_req(Domain.PRIVATE, label, model_class=model_class, prefer_cloud=True), CLOUD)
    assert result.endpoint is Endpoint.LOCAL


@pytest.mark.parametrize("domain", [Domain.CORE, Domain.TRADING, Domain.CONTENT])
def test_private_content_stays_local_in_any_domain(domain: Domain) -> None:
    result = route(_req(domain, "private", prefer_cloud=True), CLOUD)
    assert result.endpoint is Endpoint.LOCAL


def test_missing_label_is_treated_as_private() -> None:
    result = route(_req(Domain.CONTENT, None, prefer_cloud=True), CLOUD)
    assert result.endpoint is Endpoint.LOCAL
    assert result.classification is DataClassification.PRIVATE


def test_confidential_needs_pseudonymisation_for_cloud() -> None:
    local = route(_req(Domain.TRADING, "confidential", prefer_cloud=True), CLOUD)
    cloud = route(
        _req(Domain.TRADING, "confidential", prefer_cloud=True, pseudonymised=True), CLOUD
    )
    assert local.endpoint is Endpoint.LOCAL
    assert cloud.endpoint is Endpoint.CLOUD


def test_secret_is_never_routed() -> None:
    with pytest.raises(RoutingDenied):
        route(_req(Domain.CORE, "secret"), CLOUD)


def test_pii_blocks_cloud_call() -> None:
    with pytest.raises(RoutingDenied, match="email"):
        route(
            _req(Domain.CONTENT, "public", prefer_cloud=True, prompt="mail max@example.com"),
            CLOUD,
        )


def test_long_coding_context_uses_smaller_local_model() -> None:
    result = route(_req(Domain.TRADING, "internal", context_tokens=20000), CLOUD)
    assert "7b" in result.model


def test_cache_namespaces_are_per_domain_and_skip_sensitive() -> None:
    assert cache_namespace(Domain.CONTENT, "public") == "semcache:content:public"
    assert cache_namespace(Domain.TRADING, "public") == "semcache:trading:public"
    assert cache_namespace(Domain.PRIVATE, "public") is None
    assert cache_namespace(Domain.TRADING, "confidential") is None
