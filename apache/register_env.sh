#!/bin/bash
# Usage: register_env.sh <main-site>, with VNS_PROD_IP, _USER, _PASSWORD, _SSH_PORT and _FOLDER in the environment.
# It is now `harbor env register`.
[[ $EUID -eq 0 ]] || exec sudo --preserve-env=VNS_PROD_IP,VNS_PROD_USER,VNS_PROD_PASSWORD,VNS_PROD_SSH_PORT,VNS_PROD_FOLDER "$0" "$@"
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env register "$@"
