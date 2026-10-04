function(crowdy_add_public_header_test target)
  file(GLOB_RECURSE _crowdy_public_headers CONFIGURE_DEPENDS
    "${PROJECT_SOURCE_DIR}/include/crowdy/*.hpp")
  set(_crowdy_generated_dir
    "${CMAKE_CURRENT_BINARY_DIR}/${target}_sources")
  file(MAKE_DIRECTORY "${_crowdy_generated_dir}")

  set(_crowdy_sources)
  foreach(_crowdy_header IN LISTS _crowdy_public_headers)
    file(RELATIVE_PATH _crowdy_relative_header
      "${PROJECT_SOURCE_DIR}/include" "${_crowdy_header}")
    string(MAKE_C_IDENTIFIER "${_crowdy_relative_header}"
      _crowdy_source_name)
    set(_crowdy_source
      "${_crowdy_generated_dir}/${_crowdy_source_name}.cpp")
    string(CONCAT _crowdy_source_content
      "#include <${_crowdy_relative_header}>\n"
      "void ${_crowdy_source_name}_standalone() {}\n")
    file(GENERATE OUTPUT "${_crowdy_source}" CONTENT
      "${_crowdy_source_content}")
    list(APPEND _crowdy_sources "${_crowdy_source}")
  endforeach()

  set(_crowdy_main "${_crowdy_generated_dir}/main.cpp")
  file(GENERATE OUTPUT "${_crowdy_main}" CONTENT
    "int main() { return 0; }\n")
  list(APPEND _crowdy_sources "${_crowdy_main}")

  add_executable(${target} ${_crowdy_sources})
  target_include_directories(${target} PRIVATE
    "${PROJECT_SOURCE_DIR}/include")
  target_compile_features(${target} PRIVATE cxx_std_20)
  add_test(NAME ${target} COMMAND ${target})
endfunction()
