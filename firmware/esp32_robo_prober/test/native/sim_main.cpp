// Native (desktop) simulator for the ROBOPROBE/1 firmware core.
//
// Build:  c++ -std=c++17 -I../../src ../../src/rp_core.cpp sim_main.cpp -o rp_sim
//
// Reads one line per command on stdin and writes exactly one line per command
// on stdout, so a test can run a script and compare replies line by line.
// Protocol lines are passed to the real core (rp_core.cpp). Lines starting
// with '@' control the simulation:
//   @TICK <ms>          set the clock to <ms> and run Core::tick   -> SIM t=<ms>
//   @OUT channel=<c>    what the output driver last did on <c>    -> SIM out=<us|high|low|off>
//                       (channel 0..15 PCA9685, 100+N GPIO N)
//   @PIN <g> open       detach whatever is on GPIO <g>            -> SIM pin=<g>
//   @PIN <g> <mv> <ohm> GPIO <g> sees <mv> through <ohm>          -> SIM pin=<g>
//                       (fake_board.h; e.g. a button press is 0 mV via 0 ohm)
//   @DRIVER fail|ok     make the next driver calls fail or succeed -> SIM driver=<fail|ok>
//   @ESTOP_PIN          trip the hardware e-stop input             -> SIM estop_pin=1
//   @RAW <bytes>        feed <bytes> plus '\n' through LineBuffer  -> reply or SIM pending
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>

#include "fake_board.h"
#include "rp_core.h"

namespace {

constexpr int kHigh = -3;
constexpr int kLow = -2;

class RecordingDriver : public rp::OutputDriver {
 public:
  int out[rp::kSlots];
  bool pwm[rp::kSlots];
  bool fail = false;

  RecordingDriver() {
    for (int i = 0; i < rp::kSlots; ++i) {
      out[i] = -1;
      pwm[i] = false;
    }
  }
  bool available() override { return true; }
  const char *startProblem(uint8_t slot) override {
    if (slot < rp::kChannels || pwm[slot]) return nullptr;
    int used = 0;
    for (int i = rp::kChannels; i < rp::kSlots; ++i) used += pwm[i] ? 1 : 0;
    return used >= sim::kMaxPwm ? "no_pwm_slot" : nullptr;
  }
  bool writePulseUs(uint8_t slot, uint16_t pulse_us) override {
    if (fail) return false;
    out[slot] = pulse_us;
    pwm[slot] = true;
    return true;
  }
  bool writeLevel(uint8_t slot, bool high) override {
    if (fail) return false;
    out[slot] = high ? kHigh : kLow;
    return true;
  }
  bool release(uint8_t slot) override {
    out[slot] = -1;
    pwm[slot] = false;
    return !fail;
  }
  bool releaseAll() override {
    for (int i = 0; i < rp::kSlots; ++i) {
      out[i] = -1;
      pwm[i] = false;
    }
    return !fail;
  }
};

}  // namespace

int main() {
  RecordingDriver driver;
  sim::FakeBoard board;
  sim::FakeI2c bus;
  rp::Core core(driver, board, "reference-emulator", "emulated", rp::kDefaultWatchdogMs);
  rp::LineBuffer buffer;
  uint32_t now = 0;
  core.begin(now);
  core.findI2c(bus);
  char reply[rp::kReplyMax];
  std::string line;
  while (std::getline(std::cin, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line.rfind("@TICK ", 0) == 0) {
      now = static_cast<uint32_t>(std::strtoul(line.c_str() + 6, nullptr, 10));
      core.tick(now);
      std::printf("SIM t=%lu\n", static_cast<unsigned long>(now));
    } else if (line.rfind("@OUT channel=", 0) == 0) {
      int channel = std::atoi(line.c_str() + 13);
      int slot = channel < rp::kChannels ? channel : rp::kChannels + channel - rp::kGpioBase;
      if (channel < 0 || slot < 0 || slot >= rp::kSlots) {
        std::printf("SIM out=bad_channel\n");
      } else if (driver.out[slot] == kHigh || driver.out[slot] == kLow) {
        std::printf("SIM out=%s\n", driver.out[slot] == kHigh ? "high" : "low");
      } else if (driver.out[slot] < 0) {
        std::printf("SIM out=off\n");
      } else {
        std::printf("SIM out=%d\n", driver.out[slot]);
      }
    } else if (line.rfind("@PIN ", 0) == 0) {
      int gpio = 0, mv = 0, ohms = 0;
      char word[8] = {0};
      if (std::sscanf(line.c_str() + 5, "%d %d %d", &gpio, &mv, &ohms) == 3) {
        board.set(gpio, mv, ohms);
      } else if (std::sscanf(line.c_str() + 5, "%d %7s", &gpio, word) == 2 && std::strcmp(word, "open") == 0) {
        board.open(gpio);
      }
      std::printf("SIM pin=%d\n", gpio);
    } else if (line == "@DRIVER fail" || line == "@DRIVER ok") {
      driver.fail = line == "@DRIVER fail";
      std::printf("SIM driver=%s\n", driver.fail ? "fail" : "ok");
    } else if (line == "@ESTOP_PIN") {
      core.externalEstop();
      std::printf("SIM estop_pin=1\n");
    } else if (line.rfind("@RAW ", 0) == 0) {
      std::string raw = line.substr(5);
      // Escapes so tests can inject control bytes: \xHH
      std::string bytes;
      for (size_t i = 0; i < raw.size(); ++i) {
        if (raw[i] == '\\' && i + 3 < raw.size() && raw[i + 1] == 'x') {
          bytes.push_back(static_cast<char>(std::strtol(raw.substr(i + 2, 2).c_str(), nullptr, 16)));
          i += 3;
        } else {
          bytes.push_back(raw[i]);
        }
      }
      bytes.push_back('\n');
      bool answered = false;
      for (char c : bytes) {
        switch (buffer.push(c)) {
          case rp::LineBuffer::Result::Line:
            core.handleLine(buffer.line(), now, reply, sizeof(reply));
            std::printf("%s\n", reply);
            answered = true;
            break;
          case rp::LineBuffer::Result::Overflow:
            std::printf("RP1 ERR reason=line_too_long\n");
            answered = true;
            break;
          case rp::LineBuffer::Result::BadBytes:
            std::printf("RP1 ERR reason=bad_bytes\n");
            answered = true;
            break;
          case rp::LineBuffer::Result::Pending:
            break;
        }
      }
      if (!answered) std::printf("SIM pending\n");
    } else {
      core.handleLine(line.c_str(), now, reply, sizeof(reply));
      std::printf("%s\n", reply);
    }
    std::fflush(stdout);
  }
  return 0;
}
