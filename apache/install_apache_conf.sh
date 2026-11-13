#!/bin/bash
# Install the server-wide Apache configs every site has. It is now `harbor server install-apache-conf`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" server install-apache-conf "$@"
