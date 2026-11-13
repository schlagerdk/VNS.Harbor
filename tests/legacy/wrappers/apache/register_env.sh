#!/bin/bash
# Usage: register_env.sh <main-site>, with VNS_PROD_IP, _USER, _PASSWORD, _SSH_PORT and _FOLDER in the environment.
# It is now `harbor env register`.
[[ $EUID -eq 0 ]] || exec sudo -E "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env register "$@"
