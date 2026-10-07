#include "crowdy/core/logger.hpp"

#include <cstdio>

namespace crowdy::core {

namespace {

const char* levelName(LogLevel level) {
  switch (level) {
    case LogLevel::Trace: return "TRACE";
    case LogLevel::Debug: return "DEBUG";
    case LogLevel::Info: return "INFO";
    case LogLevel::Warn: return "WARN";
    case LogLevel::Error: return "ERROR";
    case LogLevel::Off: return "OFF";
  }
  return "?";
}

class StderrLogger final : public ILogger {
 public:
  bool enabled(LogLevel level) const override { return level >= LogLevel::Warn; }
  void log(LogLevel level, std::string_view message) const override {
    if (!enabled(level)) return;
    std::fprintf(stderr, "[crowdy %s] %.*s\n", levelName(level),
                 static_cast<int>(message.size()), message.data());
  }
};

}  // namespace

const ILogger& defaultLogger() {
  static StderrLogger logger;
  return logger;
}

}  // namespace crowdy::core
