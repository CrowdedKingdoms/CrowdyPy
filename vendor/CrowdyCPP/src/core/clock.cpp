#include "crowdy/core/clock.hpp"

#include <chrono>

namespace crowdy::core {

namespace {

class SystemClock final : public IClock {
 public:
  std::int64_t epochMillis() const override {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
  }
  std::int64_t monotonicMillis() const override {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
  }
};

}  // namespace

const IClock& systemClock() {
  static SystemClock clock;
  return clock;
}

std::int64_t parseIso8601Millis(const char* text, std::size_t len) {
  // Expected shape: YYYY-MM-DDTHH:MM:SS[.fff...][Z]
  if (!text || len < 19) return 0;
  auto digits = [&](std::size_t pos, int count) -> int {
    int v = 0;
    for (int i = 0; i < count; ++i) {
      const char c = text[pos + static_cast<std::size_t>(i)];
      if (c < '0' || c > '9') return -1;
      v = v * 10 + (c - '0');
    }
    return v;
  };
  const int year = digits(0, 4), month = digits(5, 2), day = digits(8, 2);
  const int hour = digits(11, 2), minute = digits(14, 2), second = digits(17, 2);
  if (year < 0 || month <= 0 || day <= 0 || hour < 0 || minute < 0 || second < 0) return 0;

  // Days since epoch (civil-from-days algorithm, proleptic Gregorian).
  const int y = year - (month <= 2 ? 1 : 0);
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = static_cast<unsigned>(y - era * 400);
  const unsigned mp = static_cast<unsigned>(month + (month > 2 ? -3 : 9));
  const unsigned doy = (153 * mp + 2) / 5 + static_cast<unsigned>(day) - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  const std::int64_t days = static_cast<std::int64_t>(era) * 146097 +
                            static_cast<std::int64_t>(doe) - 719468;

  std::int64_t ms = ((days * 24 + hour) * 60 + minute) * 60000LL + second * 1000LL;
  if (len > 20 && text[19] == '.') {
    int frac = 0, scale = 100;
    for (std::size_t i = 20; i < len && scale > 0; ++i) {
      const char c = text[i];
      if (c < '0' || c > '9') break;
      frac += (c - '0') * scale;
      scale /= 10;
    }
    ms += frac;
  }
  return ms;
}

}  // namespace crowdy::core
