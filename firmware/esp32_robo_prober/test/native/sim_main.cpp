// Native (desktop) simulator for the ROBOPROBE/1 firmware core.
//
// Build:  c++ -std=c++17 -I../../src ../../src/rp_core.cpp sim_main.cpp -o rp_sim
//
// Reads one line per command on stdin and writes exactly one line per command
// on stdout, so a test can run a script and compare replies line by line.
// Protocol lines are passed to the real core (rp_core.cpp). Lines starting
// with '@' control the simulation:
//   @TICK <ms>          set the clock to <ms> and run Core::tick   -> SIM t=<ms>
//   @OUT channel=<c>    what the output driver last did on <c>    -> SIM out=<us|off>
//   @DRIVER fail|ok     make the next driver calls fail or succeed -> SIM driver=<fail|ok>
//   @ESTOP_PIN          trip the hardware e-stop input             -> SIM estop_pin=1
//   @RAW <bytes>        feed <bytes> plus '\n' through LineBuffer  -> reply or SIM pending
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>

#include "rp_core.h"

namespace {

class RecordingDriver : public rp::OutputDriver {
 public:
  int pulse[rp::kChannels];
  bool fail = false;

  RecordingDriver() {
    for (int i = 0; i < rp::kChannels; ++i) pulse[i] = -1;
  }
  bool available() override { return true; }
  bool writePulseUs(uint8_t channel, uint16_t pulse_us) override {
    if (fail) return false;
    pulse[channel] = pulse_us;
    return true;
  }
  bool release(uint8_t channel) override {
    pulse[channel] = -1;
    return !fail;
  }
  bool releaseAll() override {
    for (int i = 0; i < rp::kChannels; ++i) pulse[i] = -1;
    return !fail;
  }
};

}  // namespace

int main() {
  RecordingDriver driver;
  rp::Core core(driver, "reference-emulator", "emulated", rp::kDefaultWatchdogMs);
  rp::LineBuffer buffer;
  uint32_t now = 0;
  core.begin(now);
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
      if (channel < 0 || channel >= rp::kChannels) {
        std::printf("SIM out=bad_channel\n");
      } else if (driver.pulse[channel] < 0) {
        std::printf("SIM out=off\n");
      } else {
        std::printf("SIM out=%d\n", driver.pulse[channel]);
      }
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
