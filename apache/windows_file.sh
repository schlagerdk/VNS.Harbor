#!/bin/bash
# Usage: windows_file.sh <site> <read|save> <file>, the environment in VNS_TARGET. It is now `harbor env windows-file`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" env windows-file "$@"
