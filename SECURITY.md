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
- Container escape and kernel exploits depend on Docker configuration. The
  default container drops all capabilities and sets `no-new-privileges`, but
  Docker is a shared-kernel boundary — treat the host as reachable by a
  determined agent. Use the E2B microVM backend when you need a boundary you can
  point to in a security review.
- Egress: by default the sandbox has open network in `egress` mode. Run with
  `SANDKEEP_NETWORK=proxy` to put it behind the key broker + egress allowlist —
  then the agent never holds the API key and can only reach the allowlist.

**Hardening knobs (Docker backend):**
- `--cap-drop ALL` + `no-new-privileges` are always on.
- `extra_run_args` is validated: boundary-breaching flags (writable mounts,
  `--privileged`, `--cap-add`, host namespaces, network/security overrides) are
  refused before `docker run`.
- `SANDKEEP_SECCOMP=/path/to/profile.json` applies a custom seccomp profile
  (empty leaves Docker's built-in default in force). Supply and verify a profile
  against your sandbox image.
- `SANDKEEP_READONLY_ROOTFS=on` runs with a read-only root filesystem and tmpfs
  for the writable workspace/HOME. Verify against your image before relying on it.
- **User-namespace remapping** is a daemon-level setting (`dockerd
  --userns-remap`) so in-container root ≠ host root — recommended, configured on
  the Docker daemon, not per run.

## Supported versions

Sandkeep is pre-1.0; only the latest released version receives security fixes.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(Security → Report a vulnerability), or email <security@your-domain>. Do **not**
open a public issue for security bugs. We aim to acknowledge reports within a few
business days and will coordinate disclosure with you.

When a fix ships, we credit reporters in the release notes unless you prefer to
remain anonymous.
