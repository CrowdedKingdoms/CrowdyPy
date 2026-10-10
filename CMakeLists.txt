cmake_minimum_required(VERSION 3.22...4.2)
project(crowdypy LANGUAGES C CXX)

if(NOT SKBUILD)
  message(WARNING
    "CrowdyPy's extension is built by scikit-build-core: run `pip install .` or "
    "`uv sync` rather than configuring this directory by hand.")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
# CrowdyCPP's static libraries are linked into a shared Python module.
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)

find_package(Python 3.12 REQUIRED
  COMPONENTS Interpreter Development.Module ${SKBUILD_SABI_COMPONENT})
find_package(nanobind CONFIG REQUIRED)

# A release wheel carries its own libcrypto: a user's system or Python may ship any OpenSSL
# (or none), and the replication core signs every datagram with it. Release builds point
# OPENSSL_ROOT_DIR at scripts/ci/build_openssl.sh's prefix (vcpkg on Windows); a source
# build without one links the system's shared libcrypto, because a distribution's static
# libcrypto drags in whatever that distribution compiled into it.
if(DEFINED OPENSSL_ROOT_DIR OR DEFINED ENV{OPENSSL_ROOT_DIR})
  set(_crowdypy_static_default ON)
else()
  set(_crowdypy_static_default OFF)
endif()
option(CROWDYPY_STATIC_OPENSSL "Link libcrypto statically" ${_crowdypy_static_default})
if(CROWDYPY_STATIC_OPENSSL)
  set(OPENSSL_USE_STATIC_LIBS TRUE)
  if(MSVC)
    set(OPENSSL_MSVC_STATIC_RT FALSE)
  endif()
  # FindOpenSSL asks pkg-config for a static libcrypto's private dependencies; make it ask
  # the chosen prefix rather than the system's openssl.pc.
  if(DEFINED ENV{OPENSSL_ROOT_DIR})
    set(ENV{PKG_CONFIG_PATH} "$ENV{OPENSSL_ROOT_DIR}/lib/pkgconfig:$ENV{PKG_CONFIG_PATH}")
  elseif(DEFINED OPENSSL_ROOT_DIR)
    set(ENV{PKG_CONFIG_PATH} "${OPENSSL_ROOT_DIR}/lib/pkgconfig:$ENV{PKG_CONFIG_PATH}")
  endif()
endif()
message(STATUS "CrowdyPy: static libcrypto: ${CROWDYPY_STATIC_OPENSSL}")
# Found here rather than only inside the vendored tree: imported targets are scoped to the
# directory that created them, and the module below links OpenSSL::Crypto directly.
find_package(OpenSSL REQUIRED COMPONENTS Crypto)

add_subdirectory(vendor/CrowdyCPP EXCLUDE_FROM_ALL)
get_directory_property(CROWDYCPP_VERSION DIRECTORY vendor/CrowdyCPP DEFINITION CrowdyCPP_VERSION)

nanobind_add_module(_native
  STABLE_ABI
  FREE_THREADED
  NB_STATIC
  LTO
  native/module.cpp
  native/replication_binding.cpp
  native/session_binding.cpp
  native/voice_binding.cpp
  native/wire_binding.cpp
)
target_include_directories(_native PRIVATE native)
target_link_libraries(_native PRIVATE crowdy crowdy_replication crowdy_crypto_openssl OpenSSL::Crypto)
if(WIN32)
  target_link_libraries(_native PRIVATE ws2_32)
endif()
target_compile_definitions(_native PRIVATE
  CROWDYPY_CROWDYCPP_VERSION="${CROWDYCPP_VERSION}")

if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  # A statically linked libcrypto must not export its symbols from the module: CPython's
  # own _ssl/_hashlib load a shared libcrypto, and an exported copy here could interpose.
  target_link_options(_native PRIVATE "LINKER:--exclude-libs,ALL")
endif()

install(TARGETS _native LIBRARY DESTINATION crowdypy COMPONENT python)
