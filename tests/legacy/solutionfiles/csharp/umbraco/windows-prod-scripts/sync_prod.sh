#!/bin/bash
# Sync settings from the main site to this environment. The work is done by sync_env.sh, which only touches the
# uSync settings folders, Views, wwwroot/css and wwwroot/scripts: content and media on this environment are kept.
_self=$(realpath "$0")
_folder=$(dirname "${_self}")
_site=${_folder%/scripts*}
_site=${_site##*/}
_id=prod
[[ ${_folder} != */scripts/targets/* ]] || _id=${_folder##*/}
exec bash "@APACHE_DIR@/sync_env.sh" "${_site}" main "${_id}" "$@"
