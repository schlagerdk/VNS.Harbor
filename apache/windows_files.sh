#!/bin/bash
# Usage: windows_files.sh <site> < operation.json, the environment in VNS_TARGET. It is now `harbor env windows-files`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env windows-files "$@"
