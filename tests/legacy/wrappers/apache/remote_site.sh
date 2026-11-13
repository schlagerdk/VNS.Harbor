#!/bin/bash
# Usage: remote_site.sh <site> <status|start|stop|restart|logs|check_service|edit_service|edit_conf|shell|files|config>
# It is now `harbor env remote` (VNS_TARGET chooses the environment).
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env remote "$@"
