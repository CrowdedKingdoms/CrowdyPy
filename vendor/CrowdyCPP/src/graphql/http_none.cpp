#include "crowdy/graphql/http.hpp"

namespace crowdy::graphql {

std::shared_ptr<IHttpTransport> makeCurlTransport() { return nullptr; }

}  // namespace crowdy::graphql
