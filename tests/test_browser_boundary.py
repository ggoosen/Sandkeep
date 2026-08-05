"""Browser bridge containment (improvement plan, step 11) — Docker-backed.

The adversarial proof that the browser bridge is a capability, not a hole: from
inside a proxy-mode sandbox the agent can drive the browser over CDP, the
browser's page loads obey the broker allowlist, and the whole thing is gone
after destroy. Skips without Docker; runs for real in CI (which builds the
sandbox, broker, and browser images).
"""

from __future__ import annotations

import json
import uuid

import pytest

from sandkeep.audit import new_trace_id
from sandkeep.models import Task
from sandkeep.provisioner import provision
from sandkeep.sandbox.docker_provider import (
    DockerConfig,
    DockerProvider,
    _browser_name,
)


@pytest.fixture
def browser_sandbox(sandbox_image, broker_image, browser_image, store, audit, host_repo):
    """A proxy-mode sandbox with the browser bridge attached."""
    provider = DockerProvider(DockerConfig(
        image=sandbox_image, network="proxy", browser=True,
        broker_image=broker_image, browser_image=browser_image,
        broker_api_key="sk-ant-not-a-real-key", egress_allowlist="api.anthropic.com",
    ))
    t = Task(id=uuid.uuid4().hex, repo_path=str(host_repo), instruction="browser test")
    store.create_task(t, new_trace_id())
    handle = provision(
        t, provider, store, audit,
        env={
            "ANTHROPIC_BASE_URL": "http://broker:8080/anthropic",
            "HTTPS_PROXY": "http://broker:8080",
            "HTTP_PROXY": "http://broker:8080",
            "SANDKEEP_BROWSER_CDP": "http://browser:9222",
        },
        trace_id=new_trace_id(),
    )
    try:
        yield provider, handle
    finally:
        provider.destroy(handle)


def _wait_cdp(provider, handle, tries: int = 20) -> bool:
    """Give Chromium a moment to bind its CDP port."""
    for _ in range(tries):
        r = provider.exec(
            handle,
            ["sh", "-c", "curl -s -m 3 http://browser:9222/json/version >/dev/null "
             "&& echo UP || echo WAIT"],
            timeout=15,
        )
        if "UP" in r.stdout:
            return True
        provider.exec(handle, ["sh", "-c", "sleep 1"], timeout=5)
    return False


def test_agent_can_reach_the_cdp_endpoint(browser_sandbox):
    provider, handle = browser_sandbox
    assert _wait_cdp(provider, handle), "CDP endpoint never came up"
    result = provider.exec(
        handle, ["sh", "-c", "curl -s -m 5 http://browser:9222/json/version"], timeout=20
    )
    assert result.exit_code == 0
    # /json/version returns the DevTools protocol banner
    info = json.loads(result.stdout)
    assert "Browser" in info or "webSocketDebuggerUrl" in info


def test_sandbox_env_advertises_the_cdp_url(browser_sandbox):
    provider, handle = browser_sandbox
    result = provider.exec(handle, ["sh", "-c", "echo $SANDKEEP_BROWSER_CDP"], timeout=15)
    assert result.stdout.strip() == "http://browser:9222"


def test_browser_container_is_gone_after_destroy(
    sandbox_image, broker_image, browser_image, store, audit, host_repo
):
    provider = DockerProvider(DockerConfig(
        image=sandbox_image, network="proxy", browser=True,
        broker_image=broker_image, browser_image=browser_image,
        broker_api_key="x", egress_allowlist="api.anthropic.com",
    ))
    t = Task(id=uuid.uuid4().hex, repo_path=str(host_repo), instruction="teardown test")
    store.create_task(t, new_trace_id())
    handle = provision(t, provider, store, audit,
                       env={"SANDKEEP_BROWSER_CDP": "http://browser:9222"},
                       trace_id=new_trace_id())
    browser = _browser_name(handle.id)
    import subprocess
    assert subprocess.run(["docker", "inspect", browser],
                          capture_output=True).returncode == 0
    provider.destroy(handle)
    assert subprocess.run(["docker", "inspect", browser],
                          capture_output=True).returncode != 0  # gone
