#include <openssl/crypto.h>

#include "common.hpp"

NB_MODULE(_native, m) {
  m.doc() = "CrowdyPy's native core: CrowdyCPP's wire codec and replication client.";

  nb::exception<crowdypy::NativeError>(m, "NativeError", PyExc_ValueError);

  m.attr("CROWDYCPP_VERSION") = CROWDYPY_CROWDYCPP_VERSION;
  m.attr("OPENSSL_VERSION") = OpenSSL_version(OPENSSL_VERSION);
#ifdef NB_FREE_THREADED
  m.attr("FREE_THREADED") = true;
#else
  m.attr("FREE_THREADED") = false;
#endif

  nb::module_ wire = m.def_submodule("wire", "The public Replication API wire codec.");
  crowdypy::register_wire(wire);
}
