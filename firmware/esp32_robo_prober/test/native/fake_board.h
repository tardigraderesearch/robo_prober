// Emulated pins and I2C bus for the native simulator and bench.
//
// Each pin is either open (nothing attached) or a source: a voltage behind a
// resistance (a pull-up resistor, an LED, a pressed button, another chip's
// output). The ESP32's internal pull is 45 kOhm. The Python device model
// (robo_prober.reference_device) uses the same arithmetic, so both give the
// same replies. Default wiring, on an ESP32-S3 profile:
//   GPIO4  servo signal input (open)        GPIO5  LED to GND (0 mV via 40 kOhm)
//   GPIO6  button with 10 kOhm pull-up      GPIO7  another chip's output, driven low
//   GPIO8/9 I2C SDA/SCL with 4.7 kOhm pull-ups, PCA9685 at 0x40 (+ its 0x70 all-call)
#pragma once

#include <cstdint>
#include <cstring>

#include "pin_profiles.h"
#include "rp_core.h"

namespace sim {

constexpr int kPullOhms = 45000;
constexpr int kRailMv = 3300;
constexpr int kMaxPwm = 8;  // LEDC channels on an ESP32-S3

struct PinSource {
  bool attached;
  int mv;
  int ohms;
};

class FakeBoard : public rp::Board {
 public:
  PinSource source[rp::kGpioCount];
  rp::Pull pull[rp::kGpioCount];

  FakeBoard() {
    for (int g = 0; g < rp::kGpioCount; ++g) {
      source[g] = {false, 0, 0};
      pull[g] = rp::Pull::None;
    }
    set(5, 0, 40000);
    set(6, kRailMv, 10000);
    set(7, 0, 30);
    set(8, kRailMv, 4700);
    set(9, kRailMv, 4700);
  }
  void set(int gpio, int mv, int ohms) { source[gpio] = {true, mv, ohms}; }
  void open(int gpio) { source[gpio] = {false, 0, 0}; }

  int voltage(int gpio, rp::Pull p) const {
    const PinSource &s = source[gpio];
    int rail = p == rp::Pull::Up ? kRailMv : 0;
    if (!s.attached) return p == rp::Pull::None ? 0 : rail;
    if (p == rp::Pull::None || s.ohms == 0) return s.mv;
    return static_cast<int>((static_cast<int64_t>(s.mv) * kPullOhms + static_cast<int64_t>(rail) * s.ohms) /
                            (s.ohms + kPullOhms));
  }

  const char *profileId() override { return "esp32_s3_chip_reference"; }
  const char *model() override { return "ESP32-S3"; }
  uint8_t pinFlags(int gpio) override { return rp::pins::k_esp32s3[gpio]; }
  bool pinInUse(int) override { return false; }
  void setInput(int gpio, rp::Pull p) override { pull[gpio] = p; }
  int level(int gpio) override { return voltage(gpio, pull[gpio]) >= kRailMv / 2 ? 1 : 0; }
  int readMv(int gpio, rp::Pull p) override {
    pull[gpio] = p;
    if (!(pinFlags(gpio) & rp::kPinAdc)) return -1;
    return voltage(gpio, p);
  }
};

class FakeI2c : public rp::I2cBus {
 public:
  int sda = -1;
  int scl = -1;
  int written = 0;  // PCA9685 register writes (all-off)
  int begins = 0;   // bus openings (one per pair scanned, plus the driver's)
  bool touched[rp::kGpioCount] = {false};  // pins the bus was ever opened on

  bool begin(int s, int c) override {
    sda = s;
    scl = c;
    ++begins;
    if (s >= 0 && s < rp::kGpioCount) touched[s] = true;
    if (c >= 0 && c < rp::kGpioCount) touched[c] = true;
    return true;
  }
  int probe(uint8_t address) override {
    if (sda != 8 || scl != 9) return 1;
    return address == 0x40 || address == 0x70 ? 0 : 1;
  }
  bool readReg(uint8_t address, uint8_t reg, uint8_t &value) override {
    if (sda != 8 || scl != 9 || address != 0x40) return false;
    value = reg == 0x01 ? 0x04 : reg == 0x05 ? 0xE0 : reg == 0xFE ? 0x1E : 0x00;
    return true;
  }
  bool writeReg(uint8_t address, uint8_t, uint8_t) override {
    if (sda != 8 || scl != 9 || address != 0x40) return false;
    ++written;
    return true;
  }
  void end() override { sda = scl = -1; }
};

}  // namespace sim
