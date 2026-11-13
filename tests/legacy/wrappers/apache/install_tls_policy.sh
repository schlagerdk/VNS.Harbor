#!/bin/bash
# Install the shared Apache server-side TLS policy. It is now `harbor server install-tls-policy [--start-if-stopped]`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" server install-tls-policy "$@"
