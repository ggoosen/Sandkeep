# Sandkeep browser sidecar (improvement plan, step 11).
# A headless Chromium that exposes ONLY a Chrome DevTools Protocol (CDP)
# endpoint on the task's internal network. The agent drives it over CDP
# (SANDKEEP_BROWSER_CDP) instead of launching its own browser, so a locked-down
# sandbox gains browser capability without GUI or direct network access. The
# browser's own egress is routed through the key-broker allowlist in proxy mode
# (HTTP(S)_PROXY), so page loads obey the same allowlist as everything else.
#
# Runs non-root with the container as its only boundary; --no-sandbox is
# required because we --cap-drop ALL (Chrome's own sandbox needs privileges we
# deliberately don't grant — the container is the boundary here).
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY launch.sh /opt/launch.sh
RUN chmod 0555 /opt/launch.sh \
    && useradd --create-home --uid 10002 chrome

USER chrome
ENV HOME=/home/chrome
EXPOSE 9222
CMD ["/bin/sh", "/opt/launch.sh"]
