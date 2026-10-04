#include <openssl/crypto.h>

#include "common.hpp"

NB_MODULE(_native, m) {
  m.doc() = "CrowdyPy's native core: CrowdyCPP's wire codec and replication client.";

  nb::exception<crowdypy::NativeError>(m, "NativeError", PyExc_ValueError);
  // A connection a program never closed is still alive at interpreter exit (its
  // provider thread is a daemon); that is a normal exit, not a leak to report.
  nb::set_leak_warnings(false);

  m.attr("CROWDYCPP_VERSION") = CROWDYPY_CROWDYCPP_VERSION;
  m.attr("OPENSSL_VERSION") = OpenSSL_version(OPENSSL_VERSION);
#ifdef NB_FREE_THREADED
  m.attr("FREE_THREADED") = true;
#else
  m.attr("FREE_THREADED") = false;
#endif

  nb::module_ wire = m.def_submodule("wire", "The public Replication API wire codec.");
  crowdypy::register_wire(wire);

  nb::module_ replication =
      m.def_submodule("replication", "CrowdyCPP's replication Connection and video frames.");
  crowdypy::register_replication(replication);

  nb::module_ session = m.def_submodule("session", "CrowdyCPP's WorldSession and its stores.");
  crowdypy::register_session(session);
}
