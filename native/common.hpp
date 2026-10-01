#pragma once

#include <nanobind/nanobind.h>

#include <cstdint>
#include <stdexcept>
#include <string>

#include "crowdy/core/bytes.hpp"
#include "crowdy/core/result.hpp"
#include "crowdy/core/uuid.hpp"
#include "crowdy/wire/codec.hpp"

namespace nb = nanobind;

namespace crowdypy {

/// Raised to Python as crowdypy._native.NativeError (a ValueError). The message
/// starts with the CrowdyCPP Errc name so the Python layer can branch on it.
struct NativeError : std::runtime_error {
  NativeError(crowdy::Errc code, const std::string& what)
      : std::runtime_error(std::string(crowdy::errcName(code)) + ": " + what) {}
};

[[noreturn]] inline void raise(crowdy::Errc code, const std::string& what) {
  throw NativeError(code, what);
}

/// A read-only view of any object that exports the buffer protocol (bytes,
/// bytearray, memoryview, numpy arrays), held for the lifetime of this object.
/// Nothing is copied; the exporter cannot be resized while the view is held, so
/// the span stays valid even with the GIL released.
class BufferView {
 public:
  explicit BufferView(nb::handle obj) {
    if (PyObject_GetBuffer(obj.ptr(), &view_, PyBUF_SIMPLE) != 0) throw nb::python_error();
    held_ = true;
  }
  BufferView(const BufferView&) = delete;
  BufferView& operator=(const BufferView&) = delete;
  BufferView(BufferView&& other) noexcept : view_(other.view_), held_(other.held_) {
    other.held_ = false;
  }
  ~BufferView() {
    if (held_) PyBuffer_Release(&view_);
  }

  crowdy::Bytes bytes() const {
    return {static_cast<const std::uint8_t*>(view_.buf), static_cast<std::size_t>(view_.len)};
  }
  std::size_t size() const { return static_cast<std::size_t>(view_.len); }

 private:
  Py_buffer view_{};
  bool held_ = false;
};

inline nb::bytes to_bytes(crowdy::Bytes b) {
  return nb::bytes(reinterpret_cast<const char*>(b.data()), b.size());
}

inline crowdy::wire::Token64 token_from(nb::handle obj) {
  if (PyUnicode_Check(obj.ptr())) {
    Py_ssize_t len = 0;
    const char* text = PyUnicode_AsUTF8AndSize(obj.ptr(), &len);
    if (!text) throw nb::python_error();
    if (len != static_cast<Py_ssize_t>(crowdy::wire::kTokenOctets))
      raise(crowdy::Errc::InvalidArgument, "an app token is exactly 64 octets");
    crowdy::wire::Token64 token;
    std::memcpy(token.octets, text, crowdy::wire::kTokenOctets);
    return token;
  }
  BufferView view(obj);
  if (view.size() != crowdy::wire::kTokenOctets)
    raise(crowdy::Errc::InvalidArgument, "an app token is exactly 64 octets");
  crowdy::wire::Token64 token;
  std::memcpy(token.octets, view.bytes().data(), crowdy::wire::kTokenOctets);
  return token;
}

/// Actor uuids are 32 ASCII octets on the wire, given as a str or bytes-like object.
/// Shorter values are NUL-padded and longer ones refused, which is CrowdyJS's
/// serializer contract for the uuid slot.
inline crowdy::core::ActorUuid uuid_from(nb::handle obj) {
  if (PyUnicode_Check(obj.ptr())) {
    Py_ssize_t len = 0;
    const char* text = PyUnicode_AsUTF8AndSize(obj.ptr(), &len);
    if (!text) throw nb::python_error();
    if (len > static_cast<Py_ssize_t>(crowdy::wire::kUuidSize))
      raise(crowdy::Errc::InvalidArgument, "an actor uuid is at most 32 octets");
    crowdy::core::ActorUuid uuid{};
    std::memcpy(uuid.data(), text, static_cast<std::size_t>(len));
    return uuid;
  }
  BufferView view(obj);
  if (view.size() > crowdy::wire::kUuidSize)
    raise(crowdy::Errc::InvalidArgument, "an actor uuid is at most 32 octets");
  crowdy::core::ActorUuid uuid{};
  std::memcpy(uuid.data(), view.bytes().data(), view.size());
  return uuid;
}

/// The uuid slot as Python bytes with trailing NUL padding stripped.
inline nb::bytes uuid_bytes(const char* uuid) {
  std::size_t len = crowdy::wire::kUuidSize;
  while (len > 0 && uuid[len - 1] == '\0') --len;
  return nb::bytes(uuid, len);
}

void register_wire(nb::module_& m);
void register_replication(nb::module_& m);

}  // namespace crowdypy
