#include <Arduino.h>
#include <Preferences.h>
#include <Wire.h>
#include <driver/gpio.h>
#include <esp_log.h>
#include <soc/soc_caps.h>

// ROBOPROBE/1 firmware for the ESP32 family (ESP32, -S2, -S3, -C3, -C6).
// Servos on a PCA9685 over I2C, or straight on GPIO pins (LEDC PWM).
//
// The protocol and every safety rule live in rp_core.cpp (shared with the
// native test simulator). This file only wires the core to the serial port,
// the pins, the I2C bus, the PCA9685 and an optional hardware e-stop input.
// Which pins exist and which must never be touched comes from pin_profiles.h,
// generated from the board catalog (src/robo_prober/catalog.py).
//
// Build-time options (PlatformIO build_flags, or #define at the top of this file):
//   RP_OUTPUTS_NONE          protocol-only build, drives nothing
//   RP_I2C_SDA, RP_I2C_SCL   PCA9685 I2C pins (default: found at boot, see Core::findI2c;
//                            the pins found are cached in NVS and checked first next boot)
//   RP_PCA9685_OE_PIN        GPIO wired to the PCA9685 OE pin, -1 = not wired
//   RP_PCA9685_OSC_HZ        oscillator calibration (default 25 MHz)
//   RP_ESTOP_PIN             GPIO for a normally-closed e-stop switch to GND,
//                            -1 = none. Pin high (switch open or wire cut)
//                            latches ESTOP.
//   RP_WATCHDOG_MS           release outputs if the host goes quiet this long

#include "pca9685_driver.h"
#include "pin_profiles.h"
#include "rp_core.h"

#ifndef RP_PCA9685_OE_PIN
#define RP_PCA9685_OE_PIN -1
#endif
#ifndef RP_PCA9685_OSC_HZ
#define RP_PCA9685_OSC_HZ 25000000UL
#endif
#ifndef RP_ESTOP_PIN
#define RP_ESTOP_PIN -1
#endif
#ifndef RP_WATCHDOG_MS
#define RP_WATCHDOG_MS 1000
#endif

#if CONFIG_IDF_TARGET_ESP32S3
#define RP_CHIP esp32s3
#elif CONFIG_IDF_TARGET_ESP32S2
#define RP_CHIP esp32s2
#elif CONFIG_IDF_TARGET_ESP32C3
#define RP_CHIP esp32c3
#elif CONFIG_IDF_TARGET_ESP32C6
#define RP_CHIP esp32c6
#elif CONFIG_IDF_TARGET_ESP32
#define RP_CHIP esp32
#else
#error "no pin profile for this chip: add it to catalog.py FIRMWARE_CHIPS and regenerate pin_profiles.h"
#endif
#define RP_CAT2(a, b) a##b
#define RP_CAT(a, b) RP_CAT2(a, b)
#define RP_TABLE(suffix) RP_CAT(rp::pins::k_, RP_CAT(RP_CHIP, suffix))

namespace {

constexpr uint32_t kServoHz = 50;
constexpr uint8_t kServoBits = 14;
constexpr uint32_t kI2cHz = 100000;

class Esp32Board : public rp::Board {
 public:
  const char *profileId() override { return RP_TABLE(_profile); }
  const char *model() override { return RP_TABLE(_model); }
  uint8_t pinFlags(int gpio) override {
    if (gpio < 0 || gpio >= rp::kGpioCount) return 0;
#if CONFIG_IDF_TARGET_ESP32
    if ((gpio == 16 || gpio == 17) && psramFound()) return 0;  // WROVER: PSRAM
#endif
    return RP_TABLE()[gpio];
  }
  bool pinInUse(int gpio) override { return gpio == RP_PCA9685_OE_PIN || gpio == RP_ESTOP_PIN; }
  void setInput(int gpio, rp::Pull pull) override {
    if (pinFlags(gpio) & rp::kPinInputOnly) pull = rp::Pull::None;  // no pull resistors there
    pinMode(gpio, pull == rp::Pull::Up ? INPUT_PULLUP : pull == rp::Pull::Down ? INPUT_PULLDOWN : INPUT);
    delayMicroseconds(100);
  }
  int level(int gpio) override { return digitalRead(gpio) == HIGH ? 1 : 0; }
  int readMv(int gpio, rp::Pull pull) override {
    if (!(pinFlags(gpio) & rp::kPinAdc)) {
      setInput(gpio, pull);
      return -1;
    }
    analogReadMilliVolts(gpio);  // attaches the ADC, which switches the pulls off
    gpio_num_t pin = static_cast<gpio_num_t>(gpio);
    if (pull == rp::Pull::Up && !(pinFlags(gpio) & rp::kPinInputOnly)) gpio_pullup_en(pin);
    if (pull == rp::Pull::Down && !(pinFlags(gpio) & rp::kPinInputOnly)) gpio_pulldown_en(pin);
    delayMicroseconds(200);
    int mv = static_cast<int>(analogReadMilliVolts(gpio));
    setInput(gpio, pull);  // back to a digital input
    return mv;
  }
};

class Esp32I2c : public rp::I2cBus {
 public:
  bool begin(int sda, int scl) override {
    if (!Wire.begin(sda, scl, kI2cHz)) return false;
    Wire.setTimeOut(5);
    gpio_set_drive_capability(static_cast<gpio_num_t>(sda), GPIO_DRIVE_CAP_0);
    gpio_set_drive_capability(static_cast<gpio_num_t>(scl), GPIO_DRIVE_CAP_0);
    sda_ = sda;
    scl_ = scl;
    return true;
  }
  int probe(uint8_t address) override {
    Wire.beginTransmission(address);
    uint8_t result = Wire.endTransmission();
    if (result == 0) return 0;
    return result == 2 || result == 3 ? 1 : 2;  // no ACK; anything else is a bus problem
  }
  bool readReg(uint8_t address, uint8_t reg, uint8_t &value) override {
    Wire.beginTransmission(address);
    Wire.write(reg);
    if (Wire.endTransmission() != 0) return false;
    if (Wire.requestFrom(static_cast<int>(address), 1) != 1) return false;
    value = static_cast<uint8_t>(Wire.read());
    return true;
  }
  bool writeReg(uint8_t address, uint8_t reg, uint8_t value) override {
    Wire.beginTransmission(address);
    Wire.write(reg);
    Wire.write(value);
    return Wire.endTransmission() == 0;
  }
  void end() override {
    Wire.end();
    if (sda_ >= 0) pinMode(sda_, INPUT);
    if (scl_ >= 0) pinMode(scl_, INPUT);
    sda_ = scl_ = -1;
  }

 private:
  int sda_ = -1;
  int scl_ = -1;
};

#ifdef RP_OUTPUTS_NONE
using Outputs = NullDriver;
#else
// Slots 0..15: PCA9685 channels. Slot 16+N: GPIO N, as 50 Hz LEDC servo PWM
// or a plain digital level (BLINK), always at the weakest drive strength.
class Outputs : public rp::OutputDriver {
 public:
  Pca9685Driver pca{RP_PCA9685_OE_PIN, RP_PCA9685_OSC_HZ, 50};
  bool pca_ok = false;

  bool available() override { return true; }
  const char *startProblem(uint8_t slot) override {
    if (slot < rp::kChannels) return pca_ok && pca.available() ? nullptr : "driver_unavailable";
    int gpio = slot - rp::kChannels;
    if (pwm_[gpio]) return nullptr;
    return pwm_count_ >= SOC_LEDC_CHANNEL_NUM ? "no_pwm_slot" : nullptr;
  }
  bool writePulseUs(uint8_t slot, uint16_t pulse_us) override {
    if (slot < rp::kChannels) return pca_ok && pca.writePulseUs(slot, pulse_us);
    int gpio = slot - rp::kChannels;
    if (digital_[gpio]) return false;
    if (!pwm_[gpio]) {
      if (!ledcAttach(gpio, kServoHz, kServoBits)) return false;
      gpio_set_drive_capability(static_cast<gpio_num_t>(gpio), GPIO_DRIVE_CAP_0);
      pwm_[gpio] = true;
      ++pwm_count_;
    }
    uint32_t duty = (static_cast<uint32_t>(pulse_us) << kServoBits) / (1000000UL / kServoHz);
    return ledcWrite(gpio, duty);
  }
  bool writeLevel(uint8_t slot, bool high) override {
    if (slot < rp::kChannels) return false;
    int gpio = slot - rp::kChannels;
    if (pwm_[gpio]) return false;
    if (!digital_[gpio]) {
      digitalWrite(gpio, high ? HIGH : LOW);
      pinMode(gpio, OUTPUT);
      gpio_set_drive_capability(static_cast<gpio_num_t>(gpio), GPIO_DRIVE_CAP_0);
      digital_[gpio] = true;
    }
    digitalWrite(gpio, high ? HIGH : LOW);
    return true;
  }
  bool release(uint8_t slot) override {
    if (slot < rp::kChannels) return !pca_ok || pca.release(slot);
    int gpio = slot - rp::kChannels;
    if (pwm_[gpio]) {
      ledcDetach(gpio);
      pwm_[gpio] = false;
      --pwm_count_;
    }
    pinMode(gpio, INPUT);
    digital_[gpio] = false;
    return true;
  }
  bool releaseAll() override {
    bool ok = !pca_ok || pca.releaseAll();
    for (int gpio = 0; gpio < rp::kGpioCount; ++gpio) {
      if (pwm_[gpio] || digital_[gpio]) release(static_cast<uint8_t>(rp::kChannels + gpio));
    }
    return ok;
  }

 private:
  bool pwm_[rp::kGpioCount] = {false};
  bool digital_[rp::kGpioCount] = {false};
  int pwm_count_ = 0;
};
#endif

Outputs outputs;
Esp32Board board;
Esp32I2c i2c;
#ifdef RP_OUTPUTS_NONE
rp::Core core(outputs, board, "esp32-robo-prober", "none", RP_WATCHDOG_MS);
#else
rp::Core core(outputs, board, "esp32-robo-prober", "gpio+pca9685", RP_WATCHDOG_MS);
#endif

rp::LineBuffer line_buffer;
char reply[rp::kReplyMax];

// The PCA9685 pins found at boot are remembered in NVS (Preferences) so the
// next boot checks that one pair first instead of scanning every pulled-up pair.
// The cache is only a hint: Core::verifyI2c re-checks it against this boot's
// survey and a real PCA9685 reply, and anything else falls back to the scan.
#if !defined(RP_OUTPUTS_NONE) && !(defined(RP_I2C_SDA) && defined(RP_I2C_SCL))
constexpr const char *kNvsNamespace = "robo_prober";

void findPca() {
  Preferences prefs;
  bool nvs = prefs.begin(kNvsNamespace, false);
  int sda = nvs ? prefs.getInt("i2c_sda", -1) : -1;
  int scl = nvs ? prefs.getInt("i2c_scl", -1) : -1;
  bool same_board = nvs && prefs.getString("profile", "") == RP_TABLE(_profile);
  bool cached = same_board && core.verifyI2c(i2c, sda, scl);
  if (!cached) core.findI2c(i2c);
  if (nvs && core.pcaAddress() && !cached) {
    prefs.putInt("i2c_sda", core.i2cSda());
    prefs.putInt("i2c_scl", core.i2cScl());
    prefs.putString("profile", RP_TABLE(_profile));
  }
  if (nvs) prefs.end();
}
#endif

bool estopInputTripped() {
#if RP_ESTOP_PIN >= 0
  return digitalRead(RP_ESTOP_PIN) == HIGH;
#else
  return false;
#endif
}

}  // namespace

void setup() {
#if RP_PCA9685_OE_PIN >= 0
  // Disable PCA9685 outputs before anything else runs.
  pinMode(RP_PCA9685_OE_PIN, OUTPUT);
  digitalWrite(RP_PCA9685_OE_PIN, HIGH);
#endif
#if RP_ESTOP_PIN >= 0
  pinMode(RP_ESTOP_PIN, INPUT_PULLUP);
#endif
  esp_log_level_set("*", ESP_LOG_NONE);  // driver logs must never mix into protocol lines
  Serial.begin(115200);
  core.begin(millis());  // releases everything, surveys the pins (read-only)
#ifndef RP_OUTPUTS_NONE
#if defined(RP_I2C_SDA) && defined(RP_I2C_SCL)
  core.findI2c(i2c, RP_I2C_SDA, RP_I2C_SCL, true);
#else
  findPca();
#endif
  // No PCA9685: START on channels 0-15 is refused (driver_unavailable); GPIO servos still work.
  if (core.pcaAddress()) outputs.pca_ok = outputs.pca.begin(core.pcaAddress());
#endif
}

void loop() {
  while (Serial.available() > 0) {
    int byte_in = Serial.read();
    if (byte_in < 0) break;
    switch (line_buffer.push(static_cast<char>(byte_in))) {
      case rp::LineBuffer::Result::Line:
        core.handleLine(line_buffer.line(), millis(), reply, sizeof(reply));
        Serial.println(reply);
        break;
      case rp::LineBuffer::Result::Overflow:
        Serial.println("RP1 ERR reason=line_too_long");
        break;
      case rp::LineBuffer::Result::BadBytes:
        Serial.println("RP1 ERR reason=bad_bytes");
        break;
      case rp::LineBuffer::Result::Pending:
        break;
    }
  }
  if (estopInputTripped() && !core.estopLatched()) {
    core.externalEstop();
  }
  core.tick(millis());
}
