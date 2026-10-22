// ROBOPROBE/1 protocol and safety core.
//
// Plain C++ with no Arduino dependency, so the same code runs on the ESP32 and
// in the native host-side simulator used by the test suite
// (test/native/sim_main.cpp). All motion goes through an OutputDriver.
//
// Safety rules enforced here:
//   * Outputs are released (PWM off) at boot, on DISARM, STOP, ESTOP, on a
//     watchdog timeout and on any driver error.
//   * MOVE needs ARM with cutoff=confirmed and the session token given at ARM.
//   * Every channel has a pulse band (min/neutral/max). The default band is a
//     conservative +/-100 us around 1500 us; the host can set another band
//     with LIMIT (on a driven channel only if its position is inside the new
//     band). Targets outside the band are rejected, and the written pulse is
//     clamped to the band as a second guard.
//   * A channel outputs nothing until the host sends START with an explicit
//     start pulse; MOVE then slews toward each target at no more than
//     slew_us_s. The firmware never picks a start pulse itself.
//   * A malformed or unknown command is rejected and never moves anything.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace rp {

constexpr const char *kProtocol = "ROBOPROBE/1";
constexpr const char *kFirmwareVersion = "0.2.0";
constexpr int kChannels = 16;
constexpr uint16_t kAbsMinUs = 500;
constexpr uint16_t kAbsMaxUs = 2500;
constexpr uint16_t kDefaultNeutralUs = 1500;
constexpr uint16_t kDefaultMinUs = 1400;
constexpr uint16_t kDefaultMaxUs = 1600;
constexpr uint16_t kDefaultSlewUsPerS = 250;
constexpr uint16_t kMaxSlewUsPerS = 2000;
constexpr uint32_t kDefaultWatchdogMs = 1000;
constexpr size_t kMaxLine = 160;
constexpr size_t kMaxToken = 32;
constexpr size_t kReplyMax = 224;

class OutputDriver {
 public:
  virtual ~OutputDriver() {}
  // Each call returns false on a hardware error; the core then releases
  // everything and disarms.
  virtual bool available() = 0;
  virtual bool writePulseUs(uint8_t channel, uint16_t pulse_us) = 0;
  virtual bool release(uint8_t channel) = 0;
  virtual bool releaseAll() = 0;
};

struct ChannelState {
  uint16_t min_us;
  uint16_t neutral_us;
  uint16_t max_us;
  uint16_t slew_us_s;
  bool active;
  uint16_t current_us;
  uint16_t target_us;
  uint32_t slew_acc;  // accumulated us*ms not yet applied
  uint32_t last_ms;
};

enum class Fault : uint8_t { None, Watchdog, Estop, Stop, Driver };

const char *faultName(Fault fault);

class Core {
 public:
  Core(OutputDriver &driver, const char *device_id, const char *outputs_name,
       uint32_t watchdog_ms = kDefaultWatchdogMs);

  // Release every output and reset all state. Call once at boot.
  void begin(uint32_t now_ms);
  // Handle one complete command line (without the newline). Always writes
  // exactly one reply line (without the newline) into `reply`.
  void handleLine(const char *line, uint32_t now_ms, char *reply, size_t reply_len);
  // Advance slew and check the watchdog. Call every loop iteration.
  void tick(uint32_t now_ms);
  // Latching emergency stop from a hardware input.
  void externalEstop();

  bool armed() const { return armed_; }
  bool estopLatched() const { return estop_; }
  Fault lastFault() const { return fault_; }
  uint32_t watchdogMs() const { return watchdog_ms_; }
  const ChannelState &channel(int index) const { return channels_[index]; }

 private:
  void resetChannels();
  void releaseAll(Fault reason);
  bool writeChannel(int index);
  void driverFault();

  OutputDriver &driver_;
  const char *device_id_;
  const char *outputs_name_;
  uint32_t watchdog_ms_;
  bool armed_ = false;
  bool estop_ = false;
  Fault fault_ = Fault::None;
  char token_[kMaxToken + 1] = {0};
  uint32_t last_feed_ms_ = 0;
  ChannelState channels_[kChannels];
};

// Non-blocking line assembler for a byte stream. Lines longer than kMaxLine
// or containing non-printable bytes are flagged so the caller can reply with
// an error instead of acting on a truncated command.
class LineBuffer {
 public:
  enum class Result : uint8_t { Pending, Line, Overflow, BadBytes };
  Result push(char c);
  const char *line() const { return buf_; }

 private:
  char buf_[kMaxLine + 1] = {0};
  size_t len_ = 0;
  bool overflow_ = false;
  bool bad_ = false;
};

}  // namespace rp
