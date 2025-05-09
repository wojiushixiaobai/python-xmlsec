#!/bin/bash

if [[ "$(uname)" == Darwin ]]; then
    brew install perl
fi

command -v yum > /dev/null 2>&1 && {
    yum install -y perl-core
}

if [ -d "/host/tmp/xmlsec.build" ]
then
    echo "xmlsec already available: build skipped" >&2
    exit 0
fi

python3 scripts/build/build_deps.py
