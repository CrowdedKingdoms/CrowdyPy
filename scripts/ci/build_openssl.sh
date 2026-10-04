#!/usr/bin/env bash
# Build the static libcrypto that CrowdyPy's wheels link, and print its install prefix.
#
#   scripts/ci/build_openssl.sh <prefix>
#
# OpenSSL LTS, verified against its published sha256, configured position-independent with
# no shared libraries, no compression, no engines or loadable modules and no apps, so the
# wheel depends on nothing the user's system happens to ship. A distribution's own static
# libcrypto is not a substitute: Ubuntu's links libjitterentropy, zstd and zlib.
#
# Linux and macOS. Windows wheels take OpenSSL from vcpkg (x64-windows-static-md) instead,
# because OpenSSL's Windows build wants nmake inside an MSVC environment.
set -euo pipefail

VERSION=3.5.9
SHA256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
PREFIX=${1:?usage: build_openssl.sh <prefix>}
STAMP="$PREFIX/.crowdypy-openssl-$VERSION"

if [ -f "$STAMP" ]; then
  echo "$PREFIX"
  exit 0
fi

cache=${CROWDYPY_OPENSSL_CACHE:-${HOME:-/tmp}/.cache/crowdypy}
tarball="$cache/openssl-$VERSION.tar.gz"
mkdir -p "$cache"
if [ ! -f "$tarball" ]; then
  curl -fsSL -o "$tarball.part" \
    "https://github.com/openssl/openssl/releases/download/openssl-$VERSION/openssl-$VERSION.tar.gz"
  mv "$tarball.part" "$tarball"
fi
if command -v sha256sum >/dev/null 2>&1; then
  echo "$SHA256  $tarball" | sha256sum -c - >&2
else
  echo "$SHA256  $tarball" | shasum -a 256 -c - >&2
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tar -xzf "$tarball" -C "$work"
cd "$work/openssl-$VERSION"

target=()
extra=()
case "$(uname -s)" in
  Darwin)
    # CROWDYPY_OPENSSL_ARCH names the target when a job cross-builds (an x86_64 wheel on
    # Apple silicon); cibuildwheel's ARCHFLAGS says the same inside a build step.
    arch=${CROWDYPY_OPENSSL_ARCH:-$(uname -m)}
    if [[ "${ARCHFLAGS:-}" == *x86_64* ]]; then arch=x86_64; fi
    if [[ "${ARCHFLAGS:-}" == *arm64* ]]; then arch=arm64; fi
    target=("darwin64-$arch-cc")
    extra=("-mmacosx-version-min=${MACOSX_DEPLOYMENT_TARGET:-11.0}")
    jobs=$(sysctl -n hw.ncpu)
    ;;
  *)
    jobs=$(nproc)
    ;;
esac

./Configure "${target[@]}" \
  no-shared no-tests no-docs no-apps \
  no-zlib no-zstd no-brotli no-engine no-module no-quic \
  -fPIC "${extra[@]}" \
  --prefix="$PREFIX" --libdir=lib >&2
make -j"$jobs" build_libs >&2
make install_dev >&2
touch "$STAMP"
echo "$PREFIX"
