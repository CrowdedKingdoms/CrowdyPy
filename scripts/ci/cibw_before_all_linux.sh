#!/usr/bin/env bash
# cibuildwheel's before-all inside the manylinux / musllinux build container: the perl
# modules OpenSSL's Configure needs, then the static libcrypto the wheel links.
set -euo pipefail

if command -v dnf >/dev/null 2>&1; then
  dnf install -y -q perl-IPC-Cmd perl-Pod-Html perl-Time-Piece >/dev/null
elif command -v yum >/dev/null 2>&1; then
  yum install -y -q perl-IPC-Cmd perl-Pod-Html >/dev/null
elif command -v apk >/dev/null 2>&1; then
  apk add --quiet perl linux-headers make
fi

bash "$(dirname "$0")/build_openssl.sh" /opt/crowdypy-openssl
