# Sandkeep egress broker image (improvement plan, step 1).
# Stdlib-only Python — this is host-trust-side code that holds the API key and
# enforces the egress allowlist, so it carries no third-party dependencies and
# a minimal surface. It runs as non-root; the controller attaches it to both
# the task's --internal network (facing the sandbox) and a normal bridge
# (facing the internet).
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 broker
COPY broker.py /opt/broker/broker.py

USER broker
EXPOSE 8080
CMD ["python", "/opt/broker/broker.py"]
