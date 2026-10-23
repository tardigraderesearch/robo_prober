// Firmware-in-the-loop bench: the real ROBOPROBE/1 core behind a pseudo
// terminal, on a desktop (Linux / macOS).
//
// Build:  c++ -std=c++17 -I../../src ../../src/rp_core.cpp bench_main.cpp -o rp_bench
//
// The loop mirrors src/main.cpp: bytes from the serial port go through
// LineBuffer into Core::handleLine, replies go back on the port, and
// Core::tick runs every iteration on a millisecond clock. The "serial port"
// is the master side of a pseudo terminal; the slave path is printed so any
// serial client (pyserial, the robo-prober host) can open it like a USB
// device.
//
// stdout (one line per event, for the test harness):
//   PORT <slave path>          once, at start
//   PWM <ms> <slot> <us>       the output driver wrote a pulse (only on change; slot
//                              0..15 = PCA9685 channel, 16+N = GPIO N, us 0/1 = BLINK level)
//   PWM <ms> <channel> off     the output driver released a channel
//   ACK <command>              a stdin control command was applied
// stdin (control lines, not seen by the firmware):
//   ESTOP_PIN                  trip the hardware e-stop input (latches)
//   DRIVER fail|ok             make output driver calls fail or succeed
//   QUIT                       exit
#include <fcntl.h>
#include <poll.h>
#include <stdlib.h>
#include <termios.h>
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>

#include "fake_board.h"
#include "rp_core.h"

namespace {

uint32_t millisNow() {
  static const auto start = std::chrono::steady_clock::now();
  return static_cast<uint32_t>(
      std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - start).count());
}

class TelemetryDriver : public rp::OutputDriver {
 public:
  int pulse[rp::kSlots];
  bool fail = false;

  TelemetryDriver() {
    for (int i = 0; i < rp::kSlots; ++i) pulse[i] = -1;
  }
  bool available() override { return true; }
  bool writePulseUs(uint8_t channel, uint16_t pulse_us) override {
    if (fail) return false;
    set(channel, pulse_us);
    return true;
  }
  bool writeLevel(uint8_t channel, bool high) override {
    if (fail) return false;
    set(channel, high ? 1 : 0);  // digital level (BLINK), reported as a 0/1 "pulse"
    return true;
  }
  bool release(uint8_t channel) override {
    set(channel, -1);
    return !fail;
  }
  bool releaseAll() override {
    // A failing bus still drops the outputs here (as the PCA9685 OE pin would).
    for (int i = 0; i < rp::kSlots; ++i) set(static_cast<uint8_t>(i), -1);
    return !fail;
  }

 private:
  void set(uint8_t channel, int value) {
    if (pulse[channel] == value) return;
    pulse[channel] = value;
    if (value < 0) {
      std::printf("PWM %lu %u off\n", static_cast<unsigned long>(millisNow()), channel);
    } else {
      std::printf("PWM %lu %u %d\n", static_cast<unsigned long>(millisNow()), channel, value);
    }
    std::fflush(stdout);
  }
};

void writeAll(int fd, const char *text) {
  size_t len = strlen(text);
  while (len > 0) {
    ssize_t n = write(fd, text, len);
    if (n <= 0) return;
    text += n;
    len -= static_cast<size_t>(n);
  }
}

}  // namespace

int main() {
  int master = posix_openpt(O_RDWR | O_NOCTTY);
  if (master < 0 || grantpt(master) != 0 || unlockpt(master) != 0) {
    std::perror("posix_openpt");
    return 1;
  }
  const char *slave_name = ptsname(master);
  if (!slave_name) {
    std::perror("ptsname");
    return 1;
  }
  std::string slave_path(slave_name);
  // Keep a slave descriptor open so the master never sees a hangup while the
  // host reopens the port, and put the line in raw mode (no echo, no CRLF).
  int slave = open(slave_path.c_str(), O_RDWR | O_NOCTTY);
  if (slave < 0) {
    std::perror("open slave");
    return 1;
  }
  termios tio;
  if (tcgetattr(slave, &tio) == 0) {
    cfmakeraw(&tio);
    tcsetattr(slave, TCSANOW, &tio);
  }
  fcntl(master, F_SETFL, fcntl(master, F_GETFL) | O_NONBLOCK);

  TelemetryDriver driver;
  sim::FakeBoard board;
  sim::FakeI2c bus;
  rp::Core core(driver, board, "firmware-bench", "bench", rp::kDefaultWatchdogMs);
  rp::LineBuffer buffer;
  char reply[rp::kReplyMax + 2];
  core.begin(millisNow());
  core.findI2c(bus);
  std::printf("PORT %s\n", slave_path.c_str());
  std::fflush(stdout);

  std::string control;
  bool running = true;
  while (running) {
    pollfd fds[2] = {{master, POLLIN, 0}, {STDIN_FILENO, POLLIN, 0}};
    poll(fds, 2, 1);
    if (fds[0].revents & POLLIN) {
      char bytes[512];
      ssize_t n = read(master, bytes, sizeof(bytes));
      for (ssize_t i = 0; i < n; ++i) {
        switch (buffer.push(bytes[i])) {
          case rp::LineBuffer::Result::Line:
            core.handleLine(buffer.line(), millisNow(), reply, rp::kReplyMax);
            std::strcat(reply, "\r\n");  // Serial.println
            writeAll(master, reply);
            break;
          case rp::LineBuffer::Result::Overflow:
            writeAll(master, "RP1 ERR reason=line_too_long\r\n");
            break;
          case rp::LineBuffer::Result::BadBytes:
            writeAll(master, "RP1 ERR reason=bad_bytes\r\n");
            break;
          case rp::LineBuffer::Result::Pending:
            break;
        }
      }
    }
    if (fds[1].revents & (POLLIN | POLLHUP)) {
      char bytes[256];
      ssize_t n = read(STDIN_FILENO, bytes, sizeof(bytes));
      if (n <= 0) running = false;
      for (ssize_t i = 0; i < n; ++i) {
        if (bytes[i] != '\n') {
          control.push_back(bytes[i]);
          continue;
        }
        if (control == "ESTOP_PIN") {
          core.externalEstop();
        } else if (control == "DRIVER fail" || control == "DRIVER ok") {
          driver.fail = control == "DRIVER fail";
        } else if (control == "QUIT") {
          running = false;
        }
        std::printf("ACK %s\n", control.c_str());
        std::fflush(stdout);
        control.clear();
      }
    }
    core.tick(millisNow());
  }
  core.begin(millisNow());  // release everything on the way out
  close(slave);
  close(master);
  return 0;
}
