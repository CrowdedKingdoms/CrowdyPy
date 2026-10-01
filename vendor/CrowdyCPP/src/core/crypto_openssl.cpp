#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/params.h>
#include <openssl/rand.h>
#include <openssl/sha.h>

#include <atomic>
#include <cstdint>

#include "crowdy/core/crypto.hpp"

namespace crowdy::core {

namespace {

/// Fetched once; EVP_MAC is reference counted, so contexts outlive it safely.
/// Freed at exit so a leak checker has nothing to report.
EVP_MAC* hmacAlgorithm() {
  struct Holder {
    EVP_MAC* mac = EVP_MAC_fetch(nullptr, "HMAC", nullptr);
    ~Holder() {
      if (mac != nullptr) EVP_MAC_free(mac);
    }
  };
  static Holder holder;
  return holder.mac;
}

/// Identifies a keyed context for the per-thread cache below. Monotonic and
/// never reused, so a freed OpensslMac cannot be confused with a new one that
/// happens to land at the same address.
std::uint64_t nextMacId() {
  static std::atomic<std::uint64_t> counter{0};
  return counter.fetch_add(1, std::memory_order_relaxed) + 1;
}

/// A thread's working context, duplicated once from the keyed template and then
/// reset per message. Resetting keeps the key schedule; duplicating per call
/// throws half the benefit away (measured: 578 ns per dup+compute against
/// 311 ns for dup-once-then-reset, versus 1241 ns for the one-shot call).
struct ThreadMacContext {
  std::uint64_t owner = 0;
  EVP_MAC_CTX* ctx = nullptr;
  ~ThreadMacContext() {
    if (ctx != nullptr) EVP_MAC_CTX_free(ctx);
  }
};

class OpensslMac final : public IMac {
 public:
  static std::shared_ptr<IMac> create(Bytes key) {
    EVP_MAC* algo = hmacAlgorithm();
    if (algo == nullptr) return nullptr;
    EVP_MAC_CTX* keyed = EVP_MAC_CTX_new(algo);
    if (keyed == nullptr) return nullptr;
    char digest[] = "SHA256";
    OSSL_PARAM params[] = {OSSL_PARAM_construct_utf8_string("digest", digest, 0),
                           OSSL_PARAM_construct_end()};
    if (EVP_MAC_init(keyed, key.data(), key.size(), params) != 1) {
      EVP_MAC_CTX_free(keyed);
      return nullptr;
    }
    return std::shared_ptr<IMac>(new OpensslMac(keyed));
  }

  ~OpensslMac() override { EVP_MAC_CTX_free(keyed_); }

  bool compute(const Bytes* parts, std::size_t count, std::uint8_t* out) const override {
    // One slot per thread rather than per (thread, key). Two connections with
    // different tokens alternating on one thread will re-duplicate each time,
    // which costs about what the old one-shot call did and is still correct;
    // one connection per thread, the ordinary case, duplicates once.
    thread_local ThreadMacContext local;
    if (local.owner != id_) {
      if (local.ctx != nullptr) EVP_MAC_CTX_free(local.ctx);
      local.ctx = EVP_MAC_CTX_dup(keyed_);
      if (local.ctx == nullptr) {
        local.owner = 0;
        return false;
      }
      local.owner = id_;
    }
    // Back to the keyed state without redoing the key schedule.
    if (EVP_MAC_init(local.ctx, nullptr, 0, nullptr) != 1) return false;
    for (std::size_t i = 0; i < count; ++i) {
      if (parts[i].empty()) continue;
      if (EVP_MAC_update(local.ctx, parts[i].data(), parts[i].size()) != 1) return false;
    }
    std::size_t written = 0;
    return EVP_MAC_final(local.ctx, out, &written, ICrypto::kHmacTagSize) == 1 &&
           written == ICrypto::kHmacTagSize;
  }

 private:
  // keyed_ is only ever init'ed, never updated or finalised, so it stays a
  // pristine template for duplication.
  explicit OpensslMac(EVP_MAC_CTX* keyed) : keyed_(keyed), id_(nextMacId()) {}

  EVP_MAC_CTX* keyed_;
  std::uint64_t id_;
};

class OpensslCrypto final : public ICrypto {
 public:
  bool hmacSha256(Bytes key, Bytes message, std::uint8_t* out) const override {
    unsigned int outLen = 0;
    return HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()), message.data(),
                message.size(), out, &outLen) != nullptr &&
           outLen == kHmacTagSize;
  }

  std::shared_ptr<IMac> makeHmacSha256(Bytes key) const override {
    return OpensslMac::create(key);
  }

  bool sha256(Bytes message, std::uint8_t* out) const override {
    unsigned int outLen = 0;
    return EVP_Digest(message.data(), message.size(), out, &outLen, EVP_sha256(), nullptr) == 1 &&
           outLen == kHmacTagSize;
  }

  bool constantTimeEquals(const std::uint8_t* a, const std::uint8_t* b,
                          std::size_t len) const override {
    return CRYPTO_memcmp(a, b, len) == 0;
  }

  bool randomBytes(std::uint8_t* out, std::size_t len) const override {
    return RAND_bytes(out, static_cast<int>(len)) == 1;
  }
};

}  // namespace

const ICrypto& opensslCrypto() {
  static OpensslCrypto crypto;
  return crypto;
}

const ICrypto& defaultCrypto() { return opensslCrypto(); }

}  // namespace crowdy::core
