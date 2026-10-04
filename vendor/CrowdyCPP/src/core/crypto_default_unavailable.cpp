#include "crowdy/core/crypto.hpp"

namespace crowdy::core {

const ICrypto& opensslCrypto() { return unavailableCrypto(); }

const ICrypto& defaultCrypto() { return unavailableCrypto(); }

}  // namespace crowdy::core
