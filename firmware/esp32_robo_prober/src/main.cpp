#include <Arduino.h>
#include <Wire.h>

// ROBOPROBE/1 firmware for a generic ESP32 / ESP32-S3 dev board driving a
// PCA9685 16-channel servo board over I2C.
//
// The protocol and every safety rule live in rp_core.cpp (shared with the
// native test simulator). This file only wires the core to the serial port,
// the PCA9685 and an optional hardware e-stop input.
//
// Build-time options (PlatformIO build_flags, or #define at the top of this file):
//   RP_OUTPUTS_NONE          protocol-only build, drives nothing (v0.1 behaviour)
//   RP_I2C_SDA, RP_I2C_SCL   I2C pins (default: the board's Wire pins)
//   RP_PCA9685_ADDR          I2C address (default 0x40)
//   RP_PCA9685_OE_PIN        GPIO wired to the PCA9685 OE pin, -1 = not wired
//   RP_PCA9685_OSC_HZ        oscillator calibration (default 25 MHz)
//   RP_ESTOP_PIN             GPIO for a normally-closed e-stop switch to GND,
//                            -1 = none. Pin high (switch open or wire cut)
//                            latches ESTOP.
//   RP_WATCHDOG_MS           release outputs if the host goes quiet this long

#include "pca9685_driver.h"
#include "rp_core.h"

#ifndef RP_PCA9685_ADDR
#define RP_PCA9685_ADDR 0x40
#endif
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

namespace {

#ifdef RP_OUTPUTS_NONE
NullDriver driver;
rp::Core core(driver, "esp32-robo-prober", "none", RP_WATCHDOG_MS);
#else
Pca9685Driver driver(RP_PCA9685_ADDR, RP_PCA9685_OE_PIN, RP_PCA9685_OSC_HZ, 50);
rp::Core core(driver, "esp32-robo-prober", "pca9685", RP_WATCHDOG_MS);
#endif

rp::LineBuffer line_buffer;
char reply[rp::kReplyMax];

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
  Serial.begin(115200);
#ifndef RP_OUTPUTS_NONE
#if defined(RP_I2C_SDA) && defined(RP_I2C_SCL)
  Wire.begin(RP_I2C_SDA, RP_I2C_SCL);
#else
  Wire.begin();
#endif
  driver.begin();  // a missing chip leaves the driver unavailable; ARM is then refused
#endif
  core.begin(millis());
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
