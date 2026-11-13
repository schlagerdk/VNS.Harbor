#!/bin/bash
# Usage: create_site.sh [folder] [html|python|csharp] [umbraco|empty] [template-version] [project-name] [binding] [publish-folder] [Debug|Release] [y|n certificate] [template-name] [Development|Test|Staging|Production] [NuGet-template-package]
# It is now `harbor site create`.
exec "$(dirname "$(readlink -f "$0")")/../bin/harbor" site create "$@"
