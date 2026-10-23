#include "pca9685_driver.h"

namespace {

constexpr uint8_t kMode1 = 0x00;
constexpr uint8_t kMode2 = 0x01;
constexpr uint8_t kLed0OnL = 0x06;
constexpr uint8_t kAllLedOffH = 0xFD;
constexpr uint8_t kPrescale = 0xFE;

constexpr uint8_t kMode1Restart = 0x80;
constexpr uint8_t kMode1AutoIncrement = 0x20;
constexpr uint8_t kMode1Sleep = 0x10;
constexpr uint8_t kMode2Totem = 0x04;
constexpr uint8_t kFullOffBit = 0x10;  // bit 4 of LEDn_OFF_H

}  // namespace

Pca9685Driver::Pca9685Driver(int oe_pin, uint32_t oscillator_hz, uint16_t frequency_hz)
    : oe_pin_(oe_pin), oscillator_hz_(oscillator_hz), frequency_hz_(frequency_hz) {}

void Pca9685Driver::setOutputEnable(bool enabled) {
  if (oe_pin_ >= 0) digitalWrite(oe_pin_, enabled ? LOW : HIGH);  // OE is active low
}

bool Pca9685Driver::write8(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(address_);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool Pca9685Driver::read8(uint8_t reg, uint8_t &value) {
  Wire.beginTransmission(address_);
  Wire.write(reg);
  if (Wire.endTransmission() != 0) return false;
  if (Wire.requestFrom(static_cast<int>(address_), 1) != 1) return false;
  value = static_cast<uint8_t>(Wire.read());
  return true;
}

bool Pca9685Driver::writeLed(uint8_t channel, uint16_t on, uint16_t off) {
  Wire.beginTransmission(address_);
  Wire.write(static_cast<uint8_t>(kLed0OnL + 4 * channel));
  Wire.write(static_cast<uint8_t>(on & 0xFF));
  Wire.write(static_cast<uint8_t>(on >> 8));
  Wire.write(static_cast<uint8_t>(off & 0xFF));
  Wire.write(static_cast<uint8_t>(off >> 8));
  return Wire.endTransmission() == 0;
}

bool Pca9685Driver::begin(uint8_t address) {
  address_ = address;
  if (oe_pin_ >= 0) {
    pinMode(oe_pin_, OUTPUT);
    setOutputEnable(false);
  }
  // prescale = round(osc / (4096 * freq)) - 1
  uint32_t prescale = (oscillator_hz_ + 2048UL * frequency_hz_) / (4096UL * frequency_hz_) - 1;
  if (prescale < 3) prescale = 3;
  if (prescale > 255) prescale = 255;
  prescale_ = static_cast<uint8_t>(prescale);

  uint8_t mode1 = 0;
  ok_ = read8(kMode1, mode1);
  // Sleep (oscillator off) to set the prescaler, everything off first.
  ok_ = ok_ && write8(kAllLedOffH, kFullOffBit);
  ok_ = ok_ && write8(kMode1, static_cast<uint8_t>((mode1 & ~kMode1Restart) | kMode1Sleep));
  ok_ = ok_ && write8(kPrescale, prescale_);
  ok_ = ok_ && write8(kMode2, kMode2Totem);
  ok_ = ok_ && write8(kMode1, kMode1AutoIncrement);
  if (ok_) {
    delay(1);  // oscillator start-up (500 us max)
    ok_ = write8(kMode1, static_cast<uint8_t>(kMode1AutoIncrement | kMode1Restart));
  }
  ok_ = ok_ && releaseAll();
  return ok_;
}

bool Pca9685Driver::writePulseUs(uint8_t channel, uint16_t pulse_us) {
  if (!ok_ || channel >= rp::kChannels) return false;
  // ticks = pulse_us * osc / ((prescale + 1) * 1e6), rounded.
  uint64_t numerator = static_cast<uint64_t>(pulse_us) * oscillator_hz_;
  uint64_t denominator = static_cast<uint64_t>(prescale_ + 1) * 1000000ULL;
  uint32_t ticks = static_cast<uint32_t>((numerator + denominator / 2) / denominator);
  if (ticks == 0 || ticks > 4095) return false;
  if (!writeLed(channel, 0, static_cast<uint16_t>(ticks))) {
    ok_ = false;
    return false;
  }
  setOutputEnable(true);
  return true;
}

bool Pca9685Driver::release(uint8_t channel) {
  if (channel >= rp::kChannels) return false;
  return writeLed(channel, 0, static_cast<uint16_t>(kFullOffBit) << 8);
}

bool Pca9685Driver::releaseAll() {
  setOutputEnable(false);  // works even if I2C is dead
  bool written = write8(kAllLedOffH, kFullOffBit);
  if (!written) ok_ = false;
  return written;
}
