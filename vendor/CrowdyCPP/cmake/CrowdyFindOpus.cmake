# libopus for CROWDY_WITH_OPUS. crowdy_find_opus() makes sure the target CrowdyOpus::opus exists:
# over opus's own CMake package (vcpkg, an upstream CMake build), else pkg-config (opus.pc:
# Debian/Ubuntu libopus-dev, Homebrew), else a plain search for opus.h and the library under
# CMAKE_PREFIX_PATH. The build uses it, and so does the installed CrowdyCPPConfig.cmake, so a
# consumer resolves the same target. It sets nothing when libopus is not there.
macro(crowdy_find_opus)
  if(NOT TARGET CrowdyOpus::opus)
    if(NOT TARGET Opus::opus)
      find_package(Opus CONFIG QUIET)
    endif()
    if(TARGET Opus::opus)
      add_library(CrowdyOpus::opus INTERFACE IMPORTED)
      set_property(TARGET CrowdyOpus::opus PROPERTY INTERFACE_LINK_LIBRARIES Opus::opus)
    else()
      find_package(PkgConfig QUIET)
      if(PKG_CONFIG_FOUND)
        pkg_check_modules(CROWDY_OPUS QUIET IMPORTED_TARGET opus)
      endif()
      if(TARGET PkgConfig::CROWDY_OPUS)
        add_library(CrowdyOpus::opus INTERFACE IMPORTED)
        set_property(TARGET CrowdyOpus::opus PROPERTY
          INTERFACE_LINK_LIBRARIES PkgConfig::CROWDY_OPUS)
      else()
        find_path(CROWDY_OPUS_INCLUDE_DIR opus.h PATH_SUFFIXES opus)
        find_library(CROWDY_OPUS_LIBRARY NAMES opus libopus)
        if(CROWDY_OPUS_INCLUDE_DIR AND CROWDY_OPUS_LIBRARY)
          add_library(CrowdyOpus::opus INTERFACE IMPORTED)
          set_property(TARGET CrowdyOpus::opus PROPERTY
            INTERFACE_INCLUDE_DIRECTORIES "${CROWDY_OPUS_INCLUDE_DIR}")
          set_property(TARGET CrowdyOpus::opus PROPERTY
            INTERFACE_LINK_LIBRARIES "${CROWDY_OPUS_LIBRARY}")
        endif()
      endif()
    endif()
  endif()
endmacro()
