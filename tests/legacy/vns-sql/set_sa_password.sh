#!/bin/bash
# Usage: set_sa_password.sh. It is now `harbor sql set-sa-password`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" sql set-sa-password "$@"
