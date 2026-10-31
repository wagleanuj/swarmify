#!/bin/sh
# Lets bubblewrap, and what it runs, make the user namespaces the connector sandbox needs on Ubuntu
# 23.10 and later (README, The connector sandbox). Ubuntu gives an unconfined process's new user
# namespace no capabilities (kernel.apparmor_restrict_unprivileged_userns), and from 25.04 its
# bwrap-userns-restrict profile does the same to the namespace a sandboxed command makes, which
# sandbox-runtime's seccomp helper needs. This profile replaces that one and grants bwrap `userns`,
# as Ubuntu's own profiles do for browsers, and leaves the restriction on for everything else.
# Run as root; a Docker host runs it too, its `sandbox` service's bwrap takes the same profile.
set -e
if [ -e /etc/apparmor.d/bwrap-userns-restrict ]; then
  apparmor_parser -R /etc/apparmor.d/bwrap-userns-restrict 2>/dev/null || true
  ln -sf /etc/apparmor.d/bwrap-userns-restrict /etc/apparmor.d/disable/
fi
cat > /etc/apparmor.d/bwrap <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
PROFILE
apparmor_parser -r /etc/apparmor.d/bwrap
