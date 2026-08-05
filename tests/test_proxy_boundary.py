"""Proxy-mode containment (improvement plan, step 1) — Docker-backed.

The adversarial proof that proxy mode delivers what the wiring promises:
inside a proxy-mode sandbox the API key is absent and direct egress to a
non-allowlisted host is refused. Skips without Docker; runs for real in CI
(which builds both the sandbox and broker images).
"""

from __future__ import annotations

import uuid

import pytest

from sandkeep.audit import new_trace_id
from sandkeep.models import Task
from sandkeep.provisioner import provision
from sandkeep.sandbox.docker_provider import DockerConfig, DockerProvider


@pytest.fixture
def proxy_sandbox(sandbox_image, broker_image, store, audit, host_repo):
    """A provisioned proxy-mode sandbox: no direct egress, no key, broker up."""
    provider = DockerProvider(DockerConfig(
        image=sandbox_image, network="proxy", broker_image=broker_image,
        broker_api_key="sk-ant-not-a-real-key", egress_allowlist="api.anthropic.com",
    ))
    t = Task(id=uuid.uuid4().hex, repo_path=str(host_repo), instruction="proxy test")
    store.create_task(t, new_trace_id())
    handle = provision(
        t, provider, store, audit,
        env={
            "ANTHROPIC_BASE_URL": "http://broker:8080/anthropic",
            "HTTPS_PROXY": "http://broker:8080",
            "HTTP_PROXY": "http://broker:8080",
        },
        trace_id=new_trace_id(),
    )
    try:
        yield provider, handle
    finally:
        provider.destroy(handle)


def test_sandbox_never_holds_the_api_key(proxy_sandbox):
    provider, handle = proxy_sandbox
    result = provider.exec(handle, ["sh", "-c", "echo ${ANTHROPIC_API_KEY:-EMPTY}"], timeout=30)
    assert result.stdout.strip() == "EMPTY"


def test_sandbox_base_url_points_at_broker(proxy_sandbox):
    provider, handle = proxy_sandbox
    result = provider.exec(handle, ["sh", "-c", "echo $ANTHROPIC_BASE_URL"], timeout=30)
    assert result.stdout.strip() == "http://broker:8080/anthropic"


def test_direct_egress_to_disallowed_host_is_refused(proxy_sandbox):
    provider, handle = proxy_sandbox
    # no direct route out of the internal network; only the broker (allowlist)
    # can reach the internet, and example.com is not on it
    result = provider.exec(
        handle,
        ["sh", "-c", "curl -s -m 8 --noproxy '' http://example.com >/dev/null && echo REACHED || echo BLOCKED"],
        timeout=30,
    )
    assert "BLOCKED" in result.stdout
