// Minimal PCA9685 output driver over I2C (Wire). No external library.
//
// Outputs are fully off after begin(). The optional OE (output enable) pin is
// held high (outputs disabled) at boot and whenever every channel is released.
#pragma once

#include <Arduino.h>
#include <Wire.h>

#include "rp_core.h"

class Pca9685Driver : public rp::OutputDriver {
 public:
  Pca9685Driver(int oe_pin, uint32_t oscillator_hz, uint16_t frequency_hz);

  // Disables OE first, then configures the chip at `address` on the already
  // started Wire bus, all outputs off. Returns false if it does not answer.
  bool begin(uint8_t address);

  bool available() override { return ok_; }
  bool writePulseUs(uint8_t channel, uint16_t pulse_us) override;
  bool release(uint8_t channel) override;
  bool releaseAll() override;

 private:
  bool write8(uint8_t reg, uint8_t value);
  bool read8(uint8_t reg, uint8_t &value);
  bool writeLed(uint8_t channel, uint16_t on, uint16_t off);
  void setOutputEnable(bool enabled);

  uint8_t address_ = 0;
  int oe_pin_;
  uint32_t oscillator_hz_;
  uint16_t frequency_hz_;
  uint8_t prescale_ = 0;
  bool ok_ = false;
};

// Used for the protocol-only build (RP_OUTPUTS_NONE): nothing is driven, and
// ARM still works so the host path can be tested with nothing attached.
class NullDriver : public rp::OutputDriver {
 public:
  bool available() override { return true; }
  bool writePulseUs(uint8_t, uint16_t) override { return true; }
  bool writeLevel(uint8_t, bool) override { return true; }
  bool release(uint8_t) override { return true; }
  bool releaseAll() override { return true; }
};
