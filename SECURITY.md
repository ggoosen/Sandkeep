# Security Policy

## Current containment status

Sandkeep runs AI agents that execute arbitrary code. Containment depends entirely
on the active `SandboxProvider`:

| Backend | Status | Contains untrusted code? |
|---|---|---|
| Docker (default, alpha) | mechanics harness | **No.** Containers are not a security boundary. |
| microVM | planned | Yes (when shipped) |

**Until a microVM backend ships, do not run agents or code you do not trust.**
The Docker backend exists to prove the orchestration mechanics — provisioning,
isolation-of-intent, diff extraction, violation detection. It does **not**
guarantee the host is safe from a hostile agent.

## What is and isn't protected (Docker backend)

**Protected:**
- Your repo is mounted **read-only**; the agent works in an independent clone.
- Only a diff and a results contract return to the host.
- Your working tree and `.git` are not modified until you explicitly `accept`.

**Not protected:**
- Container escape, kernel exploits, and egress depend on Docker configuration and
  are **not** hardened. Treat the host as reachable by a determined agent.
- In Phase 0–1, `ANTHROPIC_API_KEY` is passed into the sandbox environment
  (tracked as a known limitation; a host-side secret broker is planned for Phase 2).

## Supported versions

Sandkeep is pre-1.0; only the latest released version receives security fixes.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(Security → Report a vulnerability), or email <security@your-domain>. Do **not**
open a public issue for security bugs. We aim to acknowledge reports within a few
business days and will coordinate disclosure with you.

When a fix ships, we credit reporters in the release notes unless you prefer to
remain anonymous.
