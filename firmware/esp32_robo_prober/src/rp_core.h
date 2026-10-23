// ROBOPROBE/1 protocol and safety core.
//
// Plain C++ with no Arduino dependency, so the same code runs on the ESP32 and
// in the native host-side simulator used by the test suite
// (test/native/sim_main.cpp). All motion goes through an OutputDriver, every
// pin read through a Board, the I2C bus through an I2cBus.
//
// Safety rules enforced here:
//   * Outputs are released (PWM off, pins back to inputs) at boot, on DISARM,
//     STOP, ESTOP, on a watchdog timeout and on any driver error.
//   * MOVE, START, PROBE and BLINK need ARM with cutoff=confirmed and the
//     session token given at ARM.
//   * Every channel has a pulse band (min/neutral/max). The default band is a
//     conservative +/-100 us around 1500 us; the host can set another band
//     with LIMIT (on a driven channel only if its position is inside the new
//     band). Targets outside the band are rejected, and the written pulse is
//     clamped to the band as a second guard. Every release (DISARM, STOP,
//     ESTOP, watchdog, driver error) restores the default band.
//   * A channel outputs nothing until the host sends START (PCA9685) or PROBE
//     (a GPIO pin) with an explicit start pulse; MOVE then slews toward each
//     target at no more than slew_us_s. The firmware never picks a start pulse.
//   * SURVEY, WATCH and I2C_SCAN only read (I2C_SCAN clocks the two pins
//     open-drain at the weakest drive). PROBE and BLINK drive one pin, only a
//     pin the board profile marks usable, that the firmware itself does not
//     use, and that a fresh survey does not find driven by something else.
//   * A malformed or unknown command is rejected and never moves anything.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace rp {

constexpr const char *kProtocol = "ROBOPROBE/1";
constexpr const char *kFirmwareVersion = "0.3.1";
constexpr int kChannels = 16;                       // PCA9685 outputs: channel 0..15
constexpr int kGpioBase = 100;                      // a probed GPIO N is channel 100+N
constexpr int kGpioCount = 49;                      // GPIO0..48 (pin_profiles.h)
constexpr int kSlots = kChannels + kGpioCount;      // output slots: PCA channels, then GPIOs
constexpr uint16_t kAbsMinUs = 500;
constexpr uint16_t kAbsMaxUs = 2500;
constexpr uint16_t kDefaultNeutralUs = 1500;
constexpr uint16_t kDefaultMinUs = 1400;
constexpr uint16_t kDefaultMaxUs = 1600;
constexpr uint16_t kDefaultSlewUsPerS = 250;
constexpr uint16_t kMaxSlewUsPerS = 2000;
constexpr uint32_t kDefaultWatchdogMs = 1000;
constexpr uint32_t kBlinkHalfMs = 250;              // BLINK: 2 Hz
constexpr uint32_t kWatchIdleMs = 5000;             // WATCH turns itself off without EVENTS polls
constexpr int kPullProofMv = 150;                   // see Core::surveyPin
constexpr size_t kMaxLine = 160;
constexpr size_t kMaxToken = 32;
constexpr size_t kReplyMax = 256;

// Pin flags, the same bits as the generated pin_profiles.h.
constexpr uint8_t kPinUsable = 1;
constexpr uint8_t kPinLast = 2;
constexpr uint8_t kPinInputOnly = 4;
constexpr uint8_t kPinAdc = 8;

enum class Pull : uint8_t { None, Up, Down };
enum class PinClass : uint8_t { Unknown, Floating, PulledUp, PulledDown, DrivenHigh, DrivenLow, Reserved };
const char *pinClassName(PinClass c);

class OutputDriver {
 public:
  virtual ~OutputDriver() {}
  // Each call returns false on a hardware error; the core then releases
  // everything and disarms. Slots 0..15 are PCA9685 channels, slot 16+N is
  // GPIO N.
  virtual bool available() = 0;
  // Why `slot` cannot start output now (e.g. "driver_unavailable",
  // "no_pwm_slot"), or nullptr.
  virtual const char *startProblem(uint8_t slot) {
    (void)slot;
    return nullptr;
  }
  virtual bool writePulseUs(uint8_t slot, uint16_t pulse_us) = 0;
  // Plain digital level, for BLINK on a GPIO slot.
  virtual bool writeLevel(uint8_t slot, bool high) {
    (void)slot;
    (void)high;
    return false;
  }
  // Output off; a GPIO goes back to a plain input.
  virtual bool release(uint8_t slot) = 0;
  virtual bool releaseAll() = 0;
};

// Read side of the pins. Nothing here drives a pin.
class Board {
 public:
  virtual ~Board() {}
  virtual const char *profileId() = 0;  // catalog.py profile, e.g. esp32_s3_chip_reference
  virtual const char *model() = 0;      // e.g. ESP32-S3
  virtual uint8_t pinFlags(int gpio) = 0;
  // Used by the firmware itself (OE, e-stop input): never surveyed or driven.
  virtual bool pinInUse(int gpio) = 0;
  // Make the pin an input with this pull and let it settle.
  virtual void setInput(int gpio, Pull pull) = 0;
  virtual int level(int gpio) = 0;
  // Millivolts with this pull, or -1 without an ADC. Leaves the pin an input with `pull`.
  virtual int readMv(int gpio, Pull pull) = 0;
};

class I2cBus {
 public:
  virtual ~I2cBus() {}
  // Open-drain, weakest drive strength, so a pin someone else drives high
  // sees at most a few mA.
  virtual bool begin(int sda, int scl) = 0;
  virtual int probe(uint8_t address) = 0;  // 0 = ACK, 1 = no ACK, 2 = bus error
  virtual bool readReg(uint8_t address, uint8_t reg, uint8_t &value) = 0;
  virtual bool writeReg(uint8_t address, uint8_t reg, uint8_t value) = 0;
  virtual void end() = 0;  // both pins back to plain inputs
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
  bool blinking;      // BLINK in progress (active is also set)
  uint8_t blink_left; // level changes still to make
  bool blink_high;
  uint32_t blink_next_ms;
};

enum class Fault : uint8_t { None, Watchdog, Estop, Stop, Driver };

const char *faultName(Fault fault);

class Core {
 public:
  Core(OutputDriver &driver, Board &board, const char *device_id, const char *outputs_name,
       uint32_t watchdog_ms = kDefaultWatchdogMs);

  // Release every output, reset all state and survey every usable pin. Call once at boot.
  void begin(uint32_t now_ms);
  // Boot-time search for a PCA9685: scans pairs of pins the survey found held
  // high (I2C needs pull-ups), both orders, addresses 0x08-0x77. With
  // fixed=true only the given pair is scanned (build flags). Every PCA9685
  // found is switched fully off. Afterwards i2cSda()/i2cScl()/pcaAddress() say
  // where the driver lives (pcaAddress() == 0: none found). Keeps `bus` for
  // I2C_SCAN.
  void findI2c(I2cBus &bus, int sda = -1, int scl = -1, bool fixed = false);
  // Boot-time check of a remembered pair (the firmware caches the pins it found
  // in NVS): only pins the profile allows, that are not input-only and that the
  // survey found held high (I2C pull-ups) are probed, so a stale cache never
  // drives a pin something else now holds low. True when a PCA9685 answers
  // there; otherwise nothing is set and the caller falls back to findI2c(bus).
  bool verifyI2c(I2cBus &bus, int sda, int scl);
  // Without findI2c (protocol-only build), I2C_SCAN answers no devices.
  int i2cSda() const { return i2c_sda_; }
  int i2cScl() const { return i2c_scl_; }
  uint8_t pcaAddress() const { return pca_address_; }

  // Handle one complete command line (without the newline). Always writes
  // exactly one reply line (without the newline) into `reply`.
  void handleLine(const char *line, uint32_t now_ms, char *reply, size_t reply_len);
  // Advance slew and blinks, watch pins and check the watchdog. Call every loop iteration.
  void tick(uint32_t now_ms);
  // Latching emergency stop from a hardware input.
  void externalEstop();

  bool armed() const { return armed_; }
  bool estopLatched() const { return estop_; }
  Fault lastFault() const { return fault_; }
  uint32_t watchdogMs() const { return watchdog_ms_; }
  const ChannelState &channel(int slot) const { return channels_[slot]; }
  PinClass pinClass(int gpio) const { return classes_[gpio]; }

 private:
  void resetChannel(ChannelState &c);
  void releaseAll(Fault reason);
  bool writeChannel(int slot);
  void driverFault();
  bool pinBus(int gpio) const { return gpio == i2c_sda_ || gpio == i2c_scl_; }
  PinClass surveyPin(int gpio, int &mv);
  int scanPair(int sda, int scl, char *devices, size_t devices_len, char *pcas, size_t pcas_len,
               uint8_t &first_pca);
  void watchOff();
  const char *drivablePin(int gpio);

  OutputDriver &driver_;
  Board &board_;
  I2cBus *bus_ = nullptr;
  const char *device_id_;
  const char *outputs_name_;
  uint32_t watchdog_ms_;
  bool armed_ = false;
  bool estop_ = false;
  Fault fault_ = Fault::None;
  char token_[kMaxToken + 1] = {0};
  uint32_t last_feed_ms_ = 0;
  ChannelState channels_[kSlots];
  PinClass classes_[kGpioCount];
  int i2c_sda_ = -1;
  int i2c_scl_ = -1;
  uint8_t pca_address_ = 0;
  char i2c_text_[12] = "none";
  char pca_text_[48] = "none";
  // WATCH
  static constexpr int kMaxEvents = 32;
  bool watching_ = false;
  bool watched_[kGpioCount] = {false};
  uint8_t watch_level_[kGpioCount] = {0};
  uint32_t watch_poll_ms_ = 0;
  uint8_t event_pin_[kMaxEvents] = {0};
  uint8_t event_level_[kMaxEvents] = {0};
  int event_count_ = 0;
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
