cmake_minimum_required(VERSION 3.22)
project(CrowdyCPP VERSION 0.61.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

option(CROWDY_BUILD_TESTS "Build unit tests" ON)
option(CROWDY_BUILD_E2E "Build env-gated e2e tests" ON)
option(CROWDY_BUILD_BENCHMARKS "Build benchmarks" ON)
option(CROWDY_BUILD_EXAMPLES "Build example programs" ON)
option(CROWDY_WITH_CURL "Build the default libcurl HTTP transport" ON)
option(CROWDY_WITH_CURL_WEBSOCKETS
       "Build the optional libcurl GraphQL-WebSocket transport when supported" ON)
option(CROWDY_WITH_OPENSSL "Build the default OpenSSL crypto provider" ON)
option(CROWDY_NO_EXCEPTIONS
       "Build CrowdyCPP with non-throwing GraphQL paths and compiler exceptions disabled" OFF)
option(CROWDY_ENABLE_ASAN
       "Instrument CrowdyCPP and its tests with AddressSanitizer" OFF)
option(CROWDY_WITH_OPUS
       "Build the optional libopus wrapper (crowdy/media/opus.hpp, target crowdy_opus)" OFF)

if(CROWDY_NO_EXCEPTIONS)
  include(CheckCXXCompilerFlag)
  if(MSVC)
    set(CROWDY_NO_EXCEPTIONS_FLAG "/EHs-c-")
  else()
    check_cxx_compiler_flag("-fno-exceptions"
                           CROWDY_COMPILER_SUPPORTS_NO_EXCEPTIONS)
    if(NOT CROWDY_COMPILER_SUPPORTS_NO_EXCEPTIONS)
      message(FATAL_ERROR
        "CROWDY_NO_EXCEPTIONS requires compiler support for -fno-exceptions")
    endif()
    set(CROWDY_NO_EXCEPTIONS_FLAG "-fno-exceptions")
  endif()
endif()

function(crowdy_configure_exception_mode target)
  get_target_property(_crowdy_target_type ${target} TYPE)
  if(_crowdy_target_type STREQUAL "INTERFACE_LIBRARY")
    set(_crowdy_scope INTERFACE)
  else()
    set(_crowdy_scope PUBLIC)
  endif()
  if(CROWDY_NO_EXCEPTIONS)
    target_compile_definitions(${target} ${_crowdy_scope}
      CROWDY_NO_EXCEPTIONS=1)
    target_compile_options(${target} ${_crowdy_scope}
      "${CROWDY_NO_EXCEPTIONS_FLAG}")
  endif()
endfunction()

if(MSVC)
  # /Zc:preprocessor because the traditional preprocessor cannot carry a raw
  # string literal through a macro argument. The test macros stringify their
  # condition, so MSVC reads the JSON quoting inside R"(...)" as escape
  # sequences and rejects it (C2017) where every other toolchain accepts it.
  add_compile_options(/W4 /Zc:preprocessor)
else()
  add_compile_options(-Wall -Wextra -Wshadow -Wconversion)
endif()

if(CROWDY_ENABLE_ASAN)
  if(MSVC)
    add_compile_options(/fsanitize=address)
  elseif(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    add_compile_options(-fsanitize=address -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address)
  else()
    message(FATAL_ERROR
      "CROWDY_ENABLE_ASAN is unsupported by this compiler")
  endif()
endif()

find_package(Threads REQUIRED)

# HTTP requires curl; WebSockets merely use it when a compatible build exists.
if(CROWDY_WITH_CURL)
  find_package(CURL REQUIRED)
elseif(CROWDY_WITH_CURL_WEBSOCKETS)
  find_package(CURL QUIET)
endif()

set(CROWDY_HAS_CURL_WEBSOCKETS OFF)
if(CROWDY_WITH_CURL_WEBSOCKETS AND CURL_FOUND AND
   CURL_VERSION_STRING VERSION_GREATER_EQUAL "8.13.0")
  include(CheckCXXSourceCompiles)
  include(CheckCXXSourceRuns)
  set(_crowdy_required_libraries "${CMAKE_REQUIRED_LIBRARIES}")
  set(CMAKE_REQUIRED_LIBRARIES CURL::libcurl)
  check_cxx_source_compiles([=[
    #include <curl/curl.h>
    #if LIBCURL_VERSION_NUM < 0x080d00
    #error libcurl WebSocket fragmented-message semantics are too old
    #endif
    int main() {
      CURL* handle = nullptr;
      char buffer[1]{};
      size_t count = 0;
      const curl_ws_frame* metadata = nullptr;
      (void)curl_ws_recv(handle, buffer, sizeof(buffer), &count, &metadata);
      (void)curl_ws_send(handle, buffer, sizeof(buffer), &count, 0, CURLWS_TEXT);
      return 0;
    }
  ]=] CROWDY_CURL_WEBSOCKET_APIS_COMPILE)
  set(CROWDY_CURL_WEBSOCKET_PROTOCOLS_AVAILABLE ON)
  if(CROWDY_CURL_WEBSOCKET_APIS_COMPILE AND NOT CMAKE_CROSSCOMPILING)
    check_cxx_source_runs([=[
      #include <curl/curl.h>
      #include <cstring>
      int main() {
        const curl_version_info_data* info = curl_version_info(CURLVERSION_NOW);
        if (!info || !info->protocols) return 1;
        bool ws = false;
        bool wss = false;
        for (const char* const* protocol = info->protocols; *protocol; ++protocol) {
          ws = ws || std::strcmp(*protocol, "ws") == 0;
          wss = wss || std::strcmp(*protocol, "wss") == 0;
        }
        return ws && wss ? 0 : 1;
      }
    ]=] CROWDY_CURL_WEBSOCKET_PROTOCOLS_RUN)
    set(CROWDY_CURL_WEBSOCKET_PROTOCOLS_AVAILABLE
        ${CROWDY_CURL_WEBSOCKET_PROTOCOLS_RUN})
  endif()
  set(CMAKE_REQUIRED_LIBRARIES "${_crowdy_required_libraries}")
  unset(_crowdy_required_libraries)
  if(CROWDY_CURL_WEBSOCKET_APIS_COMPILE AND
     CROWDY_CURL_WEBSOCKET_PROTOCOLS_AVAILABLE)
    set(CROWDY_HAS_CURL_WEBSOCKETS ON)
  endif()
endif()

if(CROWDY_HAS_CURL_WEBSOCKETS)
  message(STATUS
    "CrowdyCPP: libcurl WebSocket API backend enabled "
    "(runtime ws/wss protocol support is checked by the factory)")
else()
  message(STATUS
    "CrowdyCPP: no compatible default libcurl WebSocket transport; "
    "injected IWebSocketTransport implementations remain available")
endif()

# configure_file only tracks its .in template, so the fixtures these read are
# registered as configure dependencies too. Without that, refreshing a fixture
# (a parity pin bump, say) leaves the embedded copy stale and the replay tests
# fail against the file you just corrected until someone reconfigures by hand.
set(CROWDY_EMBEDDED_FIXTURES
  "${CMAKE_CURRENT_SOURCE_DIR}/tools/parity/fixtures/crowdyjs-studio-layout.v1.json")
set_property(DIRECTORY APPEND
  PROPERTY CMAKE_CONFIGURE_DEPENDS ${CROWDY_EMBEDDED_FIXTURES})

file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/generated")

# Embed the pinned CrowdyJS layout fixture for native cross-SDK tests. The
# generated header is build-local and remains fully offline.
file(READ
  "${CMAKE_CURRENT_SOURCE_DIR}/tools/parity/fixtures/crowdyjs-studio-layout.v1.json"
  CROWDY_STUDIO_LAYOUT_FIXTURE_JSON)
configure_file(
  cmake/studio_layout_fixture.hpp.in
  "${CMAKE_CURRENT_BINARY_DIR}/generated/crowdy_studio_layout_fixture.hpp"
  @ONLY NEWLINE_STYLE LF)

# ---- vendored yyjson (JSON for the GraphQL layer only; never on the UDP path)
add_library(crowdy_yyjson STATIC third_party/yyjson/yyjson.c)
target_include_directories(crowdy_yyjson PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/third_party/yyjson>)
set_target_properties(crowdy_yyjson PROPERTIES POSITION_INDEPENDENT_CODE ON)

# ---- core + wire (no external deps beyond the ICrypto interface)
add_library(crowdy_core STATIC
  src/core/base64.cpp
  src/core/clock.cpp
  src/core/crypto_unavailable.cpp
  src/core/logger.cpp
  src/core/uuid.cpp
)
if(NOT CROWDY_WITH_OPENSSL)
  target_sources(crowdy_core PRIVATE
    src/core/crypto_default_unavailable.cpp)
endif()
target_include_directories(crowdy_core PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_compile_features(crowdy_core PUBLIC cxx_std_20)
target_link_libraries(crowdy_core PUBLIC Threads::Threads)
crowdy_configure_exception_mode(crowdy_core)

# wire is header-only; provide an interface target for consumers
add_library(crowdy_wire INTERFACE)
target_include_directories(crowdy_wire INTERFACE
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_compile_features(crowdy_wire INTERFACE cxx_std_20)
target_link_libraries(crowdy_wire INTERFACE crowdy_core)
crowdy_configure_exception_mode(crowdy_wire)

# ---- optional Opus wrapper. Off by default: the default configure, build and tests need no
# Opus. On, libopus must be found (cmake/CrowdyFindOpus.cmake says where it looks).
if(CROWDY_WITH_OPUS)
  include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/CrowdyFindOpus.cmake")
  crowdy_find_opus()
  if(NOT TARGET CrowdyOpus::opus)
    message(FATAL_ERROR
      "CROWDY_WITH_OPUS=ON but libopus was not found. Install it (libopus-dev, brew opus, "
      "vcpkg opus) or add its prefix to CMAKE_PREFIX_PATH.")
  endif()
  add_library(crowdy_opus STATIC src/media/opus.cpp)
  target_include_directories(crowdy_opus PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>)
  target_compile_features(crowdy_opus PUBLIC cxx_std_20)
  target_compile_definitions(crowdy_opus PUBLIC CROWDY_HAS_OPUS=1)
  target_link_libraries(crowdy_opus PUBLIC crowdy_core PRIVATE CrowdyOpus::opus)
  crowdy_configure_exception_mode(crowdy_opus)
  message(STATUS "CrowdyCPP: libopus wrapper enabled (crowdy_opus)")
endif()

# ---- default crypto provider (OpenSSL)
if(CROWDY_WITH_OPENSSL)
  find_package(OpenSSL REQUIRED)
  add_library(crowdy_crypto_openssl STATIC src/core/crypto_openssl.cpp)
  target_include_directories(crowdy_crypto_openssl PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
  target_compile_features(crowdy_crypto_openssl PUBLIC cxx_std_20)
  target_link_libraries(crowdy_crypto_openssl PUBLIC OpenSSL::Crypto crowdy_core)
  crowdy_configure_exception_mode(crowdy_crypto_openssl)
endif()

# ---- GraphQL core + generated surface + domains
add_library(crowdy_graphql STATIC
  src/graphql/json.cpp
  src/graphql/graphql_client.cpp
  src/graphql/subscription_client.cpp
  src/graphql/async_transport.cpp
  src/graphql/auth_state.cpp
  src/graphql/errors.cpp
)
if(NOT CROWDY_HAS_CURL_WEBSOCKETS)
  target_sources(crowdy_graphql PRIVATE src/graphql/websocket_none.cpp)
endif()
if(NOT CROWDY_WITH_CURL)
  target_sources(crowdy_graphql PRIVATE src/graphql/http_none.cpp)
endif()
target_include_directories(crowdy_graphql PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_compile_features(crowdy_graphql PUBLIC cxx_std_20)
target_link_libraries(crowdy_graphql PUBLIC crowdy_core crowdy_yyjson)
crowdy_configure_exception_mode(crowdy_graphql)

if(CROWDY_WITH_CURL)
  add_library(crowdy_http_curl STATIC src/graphql/http_curl.cpp)
  target_include_directories(crowdy_http_curl PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
  target_compile_features(crowdy_http_curl PUBLIC cxx_std_20)
  target_link_libraries(crowdy_http_curl PUBLIC CURL::libcurl crowdy_graphql)
  crowdy_configure_exception_mode(crowdy_http_curl)
endif()

if(CROWDY_HAS_CURL_WEBSOCKETS)
  add_library(crowdy_websocket_curl STATIC src/graphql/websocket_curl.cpp)
  target_include_directories(crowdy_websocket_curl PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:include>)
  target_compile_features(crowdy_websocket_curl PUBLIC cxx_std_20)
  target_link_libraries(crowdy_websocket_curl PUBLIC CURL::libcurl crowdy_graphql)
  target_compile_definitions(crowdy_websocket_curl
    PUBLIC CROWDY_HAS_CURL_WEBSOCKETS=1)
  crowdy_configure_exception_mode(crowdy_websocket_curl)
endif()

# ---- replication (native UDP client)
add_library(crowdy_replication STATIC
  src/replication/udp_socket.cpp
  src/replication/connection.cpp
)
target_include_directories(crowdy_replication PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_compile_features(crowdy_replication PUBLIC cxx_std_20)
target_link_libraries(crowdy_replication PUBLIC crowdy_wire crowdy_core)
crowdy_configure_exception_mode(crowdy_replication)
if(WIN32)
  target_link_libraries(crowdy_replication PUBLIC ws2_32)
endif()

# ---- client facade + domains + session + kit
file(GLOB CROWDY_DOMAIN_SOURCES CONFIGURE_DEPENDS src/domains/*.cpp)
file(GLOB CROWDY_SESSION_SOURCES CONFIGURE_DEPENDS src/session/*.cpp)
file(GLOB CROWDY_KIT_SOURCES CONFIGURE_DEPENDS src/kit/*.cpp)
if(CROWDY_NO_EXCEPTIONS)
  # Agent controllers and the native player-host adapter intentionally expose
  # exception-based validation APIs. They remain available in the normal
  # build; the strict no-exception facade omits those optional layers.
  set(CROWDY_PLAYER_HOST_SOURCES)
  set(CROWDY_STUDIO_SOURCES)
else()
  file(GLOB CROWDY_PLAYER_HOST_SOURCES CONFIGURE_DEPENDS src/player_host/*.cpp)
  file(GLOB CROWDY_STUDIO_SOURCES CONFIGURE_DEPENDS src/studio/*.cpp)
endif()
add_library(crowdy STATIC
  src/client.cpp
  ${CROWDY_DOMAIN_SOURCES}
  ${CROWDY_SESSION_SOURCES}
  ${CROWDY_KIT_SOURCES}
  ${CROWDY_STUDIO_SOURCES}
  ${CROWDY_PLAYER_HOST_SOURCES}
)
target_include_directories(crowdy PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_compile_features(crowdy PUBLIC cxx_std_20)
target_include_directories(crowdy PRIVATE
  "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_link_libraries(crowdy PUBLIC crowdy_graphql crowdy_replication)
crowdy_configure_exception_mode(crowdy)
if(CROWDY_WITH_CURL)
  target_link_libraries(crowdy PUBLIC crowdy_http_curl)
endif()
if(CROWDY_HAS_CURL_WEBSOCKETS)
  target_link_libraries(crowdy PUBLIC crowdy_websocket_curl)
endif()
if(CROWDY_WITH_OPENSSL)
  target_link_libraries(crowdy PUBLIC crowdy_crypto_openssl)
  target_compile_definitions(crowdy PRIVATE CROWDY_HAS_OPENSSL=1)
endif()
if(CROWDY_WITH_OPUS)
  target_link_libraries(crowdy PUBLIC crowdy_opus)
endif()

# ---- tests
if(CROWDY_BUILD_TESTS)
  enable_testing()
  add_subdirectory(tests)
endif()

if(CROWDY_BUILD_BENCHMARKS AND NOT CROWDY_NO_EXCEPTIONS)
  add_subdirectory(benchmarks)
endif()

if(CROWDY_BUILD_EXAMPLES AND NOT CROWDY_NO_EXCEPTIONS)
  add_subdirectory(examples)
endif()

# ---- installation + package config (find_package(CrowdyCPP)) ----------------
include(GNUInstallDirs)
include(CMakePackageConfigHelpers)

set(CROWDY_INSTALL_TARGETS crowdy crowdy_core crowdy_graphql crowdy_replication crowdy_yyjson)
if(CROWDY_WITH_CURL)
  list(APPEND CROWDY_INSTALL_TARGETS crowdy_http_curl)
endif()
if(CROWDY_HAS_CURL_WEBSOCKETS)
  list(APPEND CROWDY_INSTALL_TARGETS crowdy_websocket_curl)
endif()
if(CROWDY_WITH_OPENSSL)
  list(APPEND CROWDY_INSTALL_TARGETS crowdy_crypto_openssl)
endif()
if(CROWDY_WITH_OPUS)
  list(APPEND CROWDY_INSTALL_TARGETS crowdy_opus)
endif()

install(TARGETS ${CROWDY_INSTALL_TARGETS} crowdy_wire
        EXPORT CrowdyCPPTargets
        ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
if(CROWDY_NO_EXCEPTIONS)
  # The strict -fno-exceptions profile intentionally omits owning layers whose
  # validation contracts throw. Do not install headers that cannot be backed
  # by this package's reduced crowdy library.
  install(DIRECTORY include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}
          PATTERN "player_host" EXCLUDE
          PATTERN "studio" EXCLUDE
          PATTERN "kit" EXCLUDE
          PATTERN "crowdy_studio.hpp" EXCLUDE
          PATTERN "crowdy_studio_github.hpp" EXCLUDE
          PATTERN "controller.hpp" EXCLUDE
          PATTERN "diagnostics.hpp" EXCLUDE
          PATTERN "runtime.hpp" EXCLUDE
          PATTERN "models.hpp" EXCLUDE)
  install(FILES include/crowdy/studio/layout.hpp
          DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/crowdy/studio)
else()
  install(DIRECTORY include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
  install(FILES
          tools/parity/fixtures/crowdy-studio-diagnostics.v1.json
          DESTINATION ${CMAKE_INSTALL_DATADIR}/crowdy/studio)
endif()
install(EXPORT CrowdyCPPTargets
        NAMESPACE CrowdyCPP::
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/CrowdyCPP)

set(CROWDY_CONFIG_NEEDS_CURL OFF)
if(CROWDY_WITH_CURL OR CROWDY_HAS_CURL_WEBSOCKETS)
  set(CROWDY_CONFIG_NEEDS_CURL ON)
endif()
set(CROWDY_CONFIG_NEEDS_OPENSSL ${CROWDY_WITH_OPENSSL})
set(CROWDY_CONFIG_NEEDS_OPUS ${CROWDY_WITH_OPUS})
if(CROWDY_WITH_OPUS)
  install(FILES cmake/CrowdyFindOpus.cmake
          DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/CrowdyCPP)
endif()
configure_package_config_file(
  cmake/CrowdyCPPConfig.cmake.in
  ${CMAKE_CURRENT_BINARY_DIR}/CrowdyCPPConfig.cmake
  INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/CrowdyCPP)
write_basic_package_version_file(
  ${CMAKE_CURRENT_BINARY_DIR}/CrowdyCPPConfigVersion.cmake
  # CrowdyCPP is pre-1.0: patch releases preserve source/ABI compatibility,
  # while each minor may break it. Do not accept every 0.x package.
  COMPATIBILITY SameMinorVersion)
install(FILES
        ${CMAKE_CURRENT_BINARY_DIR}/CrowdyCPPConfig.cmake
        ${CMAKE_CURRENT_BINARY_DIR}/CrowdyCPPConfigVersion.cmake
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/CrowdyCPP)
