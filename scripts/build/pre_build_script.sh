#!/bin/bash

if [[ "$(uname)" == Darwin ]]; then
    brew install perl
fi

command -v yum > /dev/null 2>&1 && {
    yum install -y perl-core
}

if [[ -z "${PYXMLSEC_STATIC_DEPS}" ]]; then
    echo "PYXMLSEC_STATIC_DEPS is not set, skipping static dependencies installation."
    exit 0
fi

python3 scripts/build/build_deps.py
