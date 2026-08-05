#!/bin/sh
# Launch headless Chromium with the CDP endpoint bound to the internal network
# (improvement plan, step 11). Reachable by the sandbox at http://browser:9222.
#
# --remote-debugging-address=0.0.0.0 + --remote-allow-origins=* are required so
# a *different* container (the sandbox) can attach over CDP; Chrome otherwise
# binds debugging to localhost only and rejects cross-origin DevTools clients.
# --no-sandbox: the container is the boundary (we run with --cap-drop ALL, so
# Chrome's own sandbox can't initialise). --user-data-dir under /tmp keeps the
# profile in a writable, disposable location.
exec chromium \
  --headless=new \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port=9222 \
  --remote-allow-origins=* \
  --user-data-dir=/tmp/sandkeep-chrome \
  about:blank
