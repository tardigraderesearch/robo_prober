#include "rp_core.h"

#include <stdio.h>
#include <string.h>

namespace rp {
namespace {

constexpr int kMaxFields = 8;
constexpr uint32_t kMaxSlewDtMs = 60000;
constexpr const char *kCaps = "limit,status,hb,token,estop,start,survey,watch,i2c,probe,blink";
constexpr int kMaxPairs = 64;

struct Field {
  const char *key;
  const char *value;
};

struct Command {
  const char *verb;
  Field fields[kMaxFields];
  int count;
};

bool isVerbChar(char c) { return (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_'; }
bool isKeyChar(char c) { return (c >= 'a' && c <= 'z') || c == '_'; }
bool isTokenChar(char c) {
  return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' ||
         c == '-' || c == '.';
}

// Splits `work` in place. Grammar: VERB( key=value)*, single spaces only,
// keys lowercase, values non-empty without '=' and no duplicate keys.
// Returns an error reason, or nullptr on success.
const char *parse(char *work, Command &cmd) {
  // Trim surrounding spaces; inner spacing must be exact.
  size_t len = strlen(work);
  while (len > 0 && work[len - 1] == ' ') work[--len] = '\0';
  char *p = work;
  while (*p == ' ') ++p;
  if (*p == '\0') return "empty_command";

  cmd.verb = p;
  cmd.count = 0;
  while (*p && *p != ' ') {
    if (!isVerbChar(*p)) return "unknown_command";
    ++p;
  }
  while (*p) {
    // *p == ' '
    *p++ = '\0';
    if (*p == ' ' || *p == '\0') return "bad_syntax";
    if (cmd.count >= kMaxFields) return "too_many_fields";
    Field &field = cmd.fields[cmd.count];
    field.key = p;
    while (*p && *p != '=' && *p != ' ') {
      if (!isKeyChar(*p)) return "bad_syntax";
      ++p;
    }
    if (*p != '=' || p == field.key) return "bad_syntax";
    *p++ = '\0';
    field.value = p;
    while (*p && *p != ' ') {
      if (*p == '=') return "bad_syntax";
      ++p;
    }
    if (p == field.value) return "bad_syntax";
    for (int i = 0; i < cmd.count; ++i) {
      if (strcmp(cmd.fields[i].key, field.key) == 0) return "duplicate_field";
    }
    ++cmd.count;
  }
  return nullptr;
}

const char *find(const Command &cmd, const char *key) {
  for (int i = 0; i < cmd.count; ++i) {
    if (strcmp(cmd.fields[i].key, key) == 0) return cmd.fields[i].value;
  }
  return nullptr;
}

// True when every field key is in `allowed` and every `required` key exists.
bool fieldsOk(const Command &cmd, const char *const *allowed, int allowed_count, int required_count) {
  for (int i = 0; i < cmd.count; ++i) {
    bool known = false;
    for (int j = 0; j < allowed_count; ++j) {
      if (strcmp(cmd.fields[i].key, allowed[j]) == 0) known = true;
    }
    if (!known) return false;
  }
  for (int j = 0; j < required_count; ++j) {
    if (!find(cmd, allowed[j])) return false;
  }
  return true;
}

// Strict unsigned decimal: digits only, at most 10 of them, value <= max.
bool parseUint(const char *text, uint32_t max, uint32_t &out) {
  if (!text || !*text) return false;
  uint64_t value = 0;
  size_t digits = 0;
  for (const char *p = text; *p; ++p) {
    if (*p < '0' || *p > '9') return false;
    if (++digits > 10) return false;
    value = value * 10 + static_cast<uint64_t>(*p - '0');
  }
  if (value > max) return false;
  out = static_cast<uint32_t>(value);
  return true;
}

bool validToken(const char *text) {
  if (!text) return false;
  size_t len = strlen(text);
  if (len == 0 || len > kMaxToken) return false;
  for (size_t i = 0; i < len; ++i) {
    if (!isTokenChar(text[i])) return false;
  }
  return true;
}

void err(char *reply, size_t reply_len, const char *reason) {
  snprintf(reply, reply_len, "RP1 ERR reason=%s", reason);
}

// Channel 0..15 -> slot 0..15 (PCA9685); channel 100+N -> slot 16+N (GPIO N).
bool parseChannel(const char *text, uint32_t &channel, int &slot) {
  if (!parseUint(text, kGpioBase + kGpioCount - 1, channel)) return false;
  if (channel < static_cast<uint32_t>(kChannels)) {
    slot = static_cast<int>(channel);
  } else if (channel >= static_cast<uint32_t>(kGpioBase)) {
    slot = kChannels + static_cast<int>(channel) - kGpioBase;
  } else {
    return false;
  }
  return true;
}

bool parsePin(const char *text, int &gpio) {
  uint32_t value;
  if (!parseUint(text, kGpioCount - 1, value)) return false;
  gpio = static_cast<int>(value);
  return true;
}

// Append to a comma-separated list; false (list unchanged) when it would not fit.
bool appendItem(char *buf, size_t len, const char *item) {
  bool none = strcmp(buf, "none") == 0;
  size_t used = none ? 0 : strlen(buf);
  size_t need = used + (used ? 1 : 0) + strlen(item) + 1;
  if (need > len) return false;
  if (none) buf[0] = '\0';
  if (used) strcat(buf, ",");
  strcat(buf, item);
  return true;
}

}  // namespace

const char *faultName(Fault fault) {
  switch (fault) {
    case Fault::None: return "none";
    case Fault::Watchdog: return "watchdog";
    case Fault::Estop: return "estop";
    case Fault::Stop: return "stop";
    case Fault::Driver: return "driver";
  }
  return "unknown";
}

const char *pinClassName(PinClass c) {
  switch (c) {
    case PinClass::Floating: return "floating";
    case PinClass::PulledUp: return "pulled_up";
    case PinClass::PulledDown: return "pulled_down";
    case PinClass::DrivenHigh: return "driven_high";
    case PinClass::DrivenLow: return "driven_low";
    case PinClass::Reserved: return "reserved";
    case PinClass::Unknown: break;
  }
  return "unknown";
}

Core::Core(OutputDriver &driver, Board &board, const char *device_id, const char *outputs_name,
           uint32_t watchdog_ms)
    : driver_(driver), board_(board), device_id_(device_id), outputs_name_(outputs_name), watchdog_ms_(watchdog_ms) {
  for (int i = 0; i < kSlots; ++i) resetChannel(channels_[i]);
  for (int g = 0; g < kGpioCount; ++g) classes_[g] = PinClass::Unknown;
}

void Core::resetChannel(ChannelState &c) {
  // A band lives for one armed session: the next session must send its own
  // LIMIT, or it gets the conservative default band.
  c.min_us = kDefaultMinUs;
  c.neutral_us = kDefaultNeutralUs;
  c.max_us = kDefaultMaxUs;
  c.slew_us_s = kDefaultSlewUsPerS;
  c.active = false;
  c.current_us = 0;
  c.target_us = 0;
  c.slew_acc = 0;
  c.last_ms = 0;
  c.blinking = false;
  c.blink_left = 0;
  c.blink_high = false;
  c.blink_next_ms = 0;
}

void Core::begin(uint32_t now_ms) {
  for (int i = 0; i < kSlots; ++i) resetChannel(channels_[i]);
  armed_ = false;
  estop_ = false;
  fault_ = Fault::None;
  token_[0] = '\0';
  last_feed_ms_ = now_ms;
  driver_.releaseAll();
  watching_ = false;
  event_count_ = 0;
  // Read-only survey of every usable pin; boot strapping / JTAG pins last.
  for (int pass = 0; pass < 2; ++pass) {
    for (int g = 0; g < kGpioCount; ++g) {
      bool last = (board_.pinFlags(g) & kPinLast) != 0;
      if (last != (pass == 1)) continue;
      int mv;
      surveyPin(g, mv);
    }
  }
}

// Read-only. With the internal pull-up and then the pull-down: a pin that
// follows both is floating (nothing, a servo signal input, ...). A pin that
// keeps its level against the pull is held by something. It counts as only
// "pulled" (a resistor, so driving it later is current-limited) when the ADC
// proves it: the opposing pull moves the voltage by at least kPullProofMv. A
// real output does not move; without an ADC there is no proof, so it is
// "driven" and never driven by PROBE or BLINK.
PinClass Core::surveyPin(int gpio, int &mv) {
  mv = -1;
  uint8_t flags = board_.pinFlags(gpio);
  PinClass c;
  if (!(flags & kPinUsable) || board_.pinInUse(gpio) || pinBus(gpio)) {
    c = PinClass::Reserved;
  } else {
    board_.setInput(gpio, Pull::Up);
    int up = board_.level(gpio);
    board_.setInput(gpio, Pull::Down);
    int down = board_.level(gpio);
    if (up && !down) {
      c = PinClass::Floating;
    } else if (up == down) {
      bool high = up != 0;
      int plain = board_.readMv(gpio, Pull::None);
      int against = board_.readMv(gpio, high ? Pull::Down : Pull::Up);
      int shift = high ? plain - against : against - plain;
      bool resistive = plain >= 0 && against >= 0 && shift >= kPullProofMv;
      c = high ? (resistive ? PinClass::PulledUp : PinClass::DrivenHigh)
               : (resistive ? PinClass::PulledDown : PinClass::DrivenLow);
    } else {
      c = PinClass::DrivenHigh;  // goes against both pulls: something is switching it
    }
    board_.setInput(gpio, Pull::None);
    if (flags & kPinAdc) mv = board_.readMv(gpio, Pull::None);
  }
  classes_[gpio] = c;
  return c;
}

int Core::scanPair(int sda, int scl, char *devices, size_t devices_len, char *pcas, size_t pcas_len,
                   uint8_t &first_pca) {
  strcpy(devices, "none");
  strcpy(pcas, "none");
  first_pca = 0;
  if (!bus_) return 0;
  bus_->end();
  if (!bus_->begin(sda, scl)) {
    bus_->end();
    return 0;
  }
  uint8_t acked[0x78];
  int count = 0;
  for (uint8_t address = 0x08; address <= 0x77; ++address) {
    int result = bus_->probe(address);
    if (result == 2) break;  // stuck or dead bus: give up on this pair
    if (result == 0) acked[count++] = address;
  }
  char item[8];
  for (int i = 0; i < count; ++i) {
    uint8_t a = acked[i];
    snprintf(item, sizeof(item), "0x%02x", a);
    appendItem(devices, devices_len, item);
    if (a < 0x40 || a == 0x70) continue;  // PCA9685 addresses are 0x40-0x7F; 0x70 is its all-call
    uint8_t mode2, allcall, prescale;
    if (bus_->readReg(a, 0x01, mode2) && (mode2 & 0xE0) == 0 && bus_->readReg(a, 0x05, allcall) &&
        (allcall & 0x01) == 0 && bus_->readReg(a, 0xFE, prescale) && prescale >= 3) {
      bus_->writeReg(a, 0xFD, 0x10);  // ALL_LED_OFF_H full off: a found PCA9685 outputs nothing
      appendItem(pcas, pcas_len, item);
      if (!first_pca) first_pca = a;
    }
  }
  bus_->end();
  if (pca_address_) bus_->begin(i2c_sda_, i2c_scl_);  // back to the driver's bus
  return count;
}

void Core::findI2c(I2cBus &bus, int sda, int scl, bool fixed) {
  bus_ = &bus;
  i2c_sda_ = i2c_scl_ = -1;
  pca_address_ = 0;
  strcpy(i2c_text_, "none");
  strcpy(pca_text_, "none");
  int pairs[kMaxPairs][2];
  int count = 0;
  if (fixed) {
    pairs[count][0] = sda;
    pairs[count][1] = scl;
    ++count;
  } else {
    // I2C lines have pull-ups: only pins held high are candidates. Strapping
    // / JTAG pins pair up last.
    int high[kGpioCount];
    int n = 0;
    for (int pass = 0; pass < 2; ++pass) {
      for (int g = 0; g < kGpioCount; ++g) {
        bool last = (board_.pinFlags(g) & kPinLast) != 0;
        if (last != (pass == 1) || (board_.pinFlags(g) & kPinInputOnly)) continue;
        if (classes_[g] == PinClass::PulledUp || classes_[g] == PinClass::DrivenHigh) high[n++] = g;
      }
    }
    for (int i = 0; i < n && count < kMaxPairs; ++i) {
      for (int j = 0; j < n && count < kMaxPairs; ++j) {
        if (i == j) continue;
        pairs[count][0] = high[i];
        pairs[count][1] = high[j];
        ++count;
      }
    }
  }
  char devices[96];
  char pcas[48];
  for (int i = 0; i < count && !pca_address_; ++i) {
    uint8_t first;
    scanPair(pairs[i][0], pairs[i][1], devices, sizeof(devices), pcas, sizeof(pcas), first);
    if (!first) continue;
    i2c_sda_ = pairs[i][0];
    i2c_scl_ = pairs[i][1];
    pca_address_ = first;
    snprintf(i2c_text_, sizeof(i2c_text_), "%d,%d", i2c_sda_, i2c_scl_);
    strcpy(pca_text_, pcas);
    classes_[i2c_sda_] = PinClass::Reserved;
    classes_[i2c_scl_] = PinClass::Reserved;
    bus_->begin(i2c_sda_, i2c_scl_);
  }
}

void Core::releaseAll(Fault reason) {
  // Always clear software state, even if the driver reports an error.
  bool ok = driver_.releaseAll();
  armed_ = false;
  token_[0] = '\0';
  for (int i = 0; i < kSlots; ++i) resetChannel(channels_[i]);
  if (reason != Fault::None) fault_ = reason;
  if (!ok) fault_ = Fault::Driver;
}

void Core::driverFault() { releaseAll(Fault::Driver); }

void Core::externalEstop() {
  releaseAll(Fault::Estop);
  estop_ = true;
}

bool Core::writeChannel(int slot) {
  ChannelState &c = channels_[slot];
  uint16_t pulse = c.current_us;
  if (pulse < c.min_us) pulse = c.min_us;
  if (pulse > c.max_us) pulse = c.max_us;
  return driver_.writePulseUs(static_cast<uint8_t>(slot), pulse);
}

void Core::watchOff() {
  if (!watching_) return;
  for (int g = 0; g < kGpioCount; ++g) {
    if (watched_[g] && !channels_[kChannels + g].active) board_.setInput(g, Pull::None);
    watched_[g] = false;
  }
  watching_ = false;
  event_count_ = 0;
}

// Why PROBE/BLINK may not drive this pin, or nullptr. Surveys it again first.
const char *Core::drivablePin(int gpio) {
  uint8_t flags = board_.pinFlags(gpio);
  if (!(flags & kPinUsable) || (flags & kPinInputOnly)) return "pin_reserved";
  if (board_.pinInUse(gpio) || pinBus(gpio) || channels_[kChannels + gpio].active) return "pin_claimed";
  watched_[gpio] = false;  // the probe takes the pin over from WATCH
  int mv;
  PinClass c = surveyPin(gpio, mv);
  if (c == PinClass::DrivenHigh || c == PinClass::DrivenLow) return "pin_driven";
  if (c == PinClass::Reserved) return "pin_reserved";
  return nullptr;
}

void Core::tick(uint32_t now_ms) {
  if (armed_ && static_cast<uint32_t>(now_ms - last_feed_ms_) > watchdog_ms_) {
    releaseAll(Fault::Watchdog);
    return;
  }
  for (int i = 0; i < kSlots; ++i) {
    ChannelState &c = channels_[i];
    if (!c.active) continue;
    if (c.blinking) {
      if (static_cast<int32_t>(now_ms - c.blink_next_ms) < 0) continue;
      bool ok;
      if (c.blink_left == 0) {
        ok = driver_.release(static_cast<uint8_t>(i));
        resetChannel(c);
      } else {
        c.blink_high = !c.blink_high;
        --c.blink_left;
        c.blink_next_ms += kBlinkHalfMs;
        ok = driver_.writeLevel(static_cast<uint8_t>(i), c.blink_high);
      }
      if (!ok) {
        driverFault();
        return;
      }
      continue;
    }
    uint32_t dt = static_cast<uint32_t>(now_ms - c.last_ms);
    c.last_ms = now_ms;
    if (c.current_us == c.target_us) {
      c.slew_acc = 0;
      continue;
    }
    if (dt > kMaxSlewDtMs) dt = kMaxSlewDtMs;
    c.slew_acc += static_cast<uint32_t>(c.slew_us_s) * dt;
    uint32_t step = c.slew_acc / 1000;
    if (step == 0) continue;
    c.slew_acc %= 1000;
    uint32_t distance = c.target_us > c.current_us ? c.target_us - c.current_us : c.current_us - c.target_us;
    if (step >= distance) {
      c.current_us = c.target_us;
      c.slew_acc = 0;
    } else if (c.target_us > c.current_us) {
      c.current_us = static_cast<uint16_t>(c.current_us + step);
    } else {
      c.current_us = static_cast<uint16_t>(c.current_us - step);
    }
    if (!writeChannel(i)) {
      driverFault();
      return;
    }
  }
  if (watching_) {
    if (static_cast<uint32_t>(now_ms - watch_poll_ms_) > kWatchIdleMs) {
      watchOff();  // the host went away: no pull-ups left on
      return;
    }
    for (int g = 0; g < kGpioCount; ++g) {
      if (!watched_[g]) continue;
      uint8_t level = board_.level(g) ? 1 : 0;
      if (level == watch_level_[g]) continue;
      watch_level_[g] = level;
      if (event_count_ < kMaxEvents) {
        event_pin_[event_count_] = static_cast<uint8_t>(g);
        event_level_[event_count_] = level;
        ++event_count_;
      }
    }
  }
}

void Core::handleLine(const char *line, uint32_t now_ms, char *reply, size_t reply_len) {
  tick(now_ms);

  char work[kMaxLine + 1];
  if (strlen(line) > kMaxLine) {
    err(reply, reply_len, "line_too_long");
    return;
  }
  strcpy(work, line);
  Command cmd;
  const char *problem = parse(work, cmd);
  if (problem) {
    err(reply, reply_len, problem);
    return;
  }
  const char *verb = cmd.verb;

  if (strcmp(verb, "HELLO") == 0) {
    if (cmd.count != 0) return err(reply, reply_len, "bad_fields");
    const char *mode = estop_ ? "estop" : (armed_ ? "armed" : "disarmed");
    snprintf(reply, reply_len,
             "RP1 OK proto=%s mode=%s id=%s fw=%s outputs=%s i2c=%s pca9685=%s watchdog_ms=%lu caps=%s fault=%s",
             kProtocol, mode, device_id_, kFirmwareVersion, outputs_name_, i2c_text_, pca_text_,
             static_cast<unsigned long>(watchdog_ms_), kCaps, faultName(fault_));
    return;
  }

  if (strcmp(verb, "BOARD") == 0) {
    if (cmd.count != 0) return err(reply, reply_len, "bad_fields");
    char pins[160] = "none";
    char item[4];
    for (int g = 0; g < kGpioCount; ++g) {
      if (!(board_.pinFlags(g) & kPinUsable) || board_.pinInUse(g) || pinBus(g)) continue;
      snprintf(item, sizeof(item), "%d", g);
      appendItem(pins, sizeof(pins), item);
    }
    snprintf(reply, reply_len, "RP1 OK board=%s chip=%s pins=%s", board_.profileId(), board_.model(), pins);
    return;
  }

  if (strcmp(verb, "SURVEY") == 0) {
    // Read-only: pulls and reads one pin, drives nothing.
    static const char *const keys[] = {"pin"};
    if (!fieldsOk(cmd, keys, 1, 1)) return err(reply, reply_len, "bad_fields");
    int gpio;
    if (!parsePin(find(cmd, "pin"), gpio)) return err(reply, reply_len, "bad_pin");
    if (channels_[kChannels + gpio].active) return err(reply, reply_len, "pin_claimed");
    int mv;
    PinClass c = surveyPin(gpio, mv);
    if (watched_[gpio]) {
      board_.setInput(gpio, c == PinClass::Floating ? Pull::Up : Pull::None);
      watch_level_[gpio] = board_.level(gpio) ? 1 : 0;
    }
    char adc[12] = "-";
    if (mv >= 0) snprintf(adc, sizeof(adc), "%d", mv);
    snprintf(reply, reply_len, "RP1 OK pin=%d class=%s adc=%s", gpio, pinClassName(c), adc);
    return;
  }

  if (strcmp(verb, "WATCH") == 0) {
    // Read-only: floating pins get the internal pull-up so a button to GND
    // shows up; level changes queue up for EVENTS.
    static const char *const keys[] = {"state"};
    if (!fieldsOk(cmd, keys, 1, 1)) return err(reply, reply_len, "bad_fields");
    const char *state = find(cmd, "state");
    if (strcmp(state, "off") == 0) {
      watchOff();
      snprintf(reply, reply_len, "RP1 OK watch=0");
      return;
    }
    if (strcmp(state, "on") != 0) return err(reply, reply_len, "bad_fields");
    if (!watching_) {
      for (int g = 0; g < kGpioCount; ++g) {
        PinClass c = classes_[g];
        watched_[g] = c != PinClass::Reserved && c != PinClass::Unknown && !channels_[kChannels + g].active;
        if (!watched_[g]) continue;
        board_.setInput(g, c == PinClass::Floating ? Pull::Up : Pull::None);
        watch_level_[g] = board_.level(g) ? 1 : 0;
      }
      event_count_ = 0;
      watching_ = true;
    }
    watch_poll_ms_ = now_ms;
    snprintf(reply, reply_len, "RP1 OK watch=1");
    return;
  }

  if (strcmp(verb, "EVENTS") == 0) {
    if (cmd.count != 0) return err(reply, reply_len, "bad_fields");
    watch_poll_ms_ = now_ms;
    char events[200] = "none";
    char item[8];
    int taken = 0;
    while (taken < event_count_) {
      snprintf(item, sizeof(item), "%u:%u", event_pin_[taken], event_level_[taken]);
      if (!appendItem(events, sizeof(events), item)) break;
      ++taken;
    }
    for (int i = taken; i < event_count_; ++i) {
      event_pin_[i - taken] = event_pin_[i];
      event_level_[i - taken] = event_level_[i];
    }
    event_count_ -= taken;
    snprintf(reply, reply_len, "RP1 OK events=%s", events);
    return;
  }

  if (strcmp(verb, "I2C_SCAN") == 0) {
    static const char *const keys[] = {"sda", "scl"};
    if (!fieldsOk(cmd, keys, 2, 2)) return err(reply, reply_len, "bad_fields");
    int sda, scl;
    if (!parsePin(find(cmd, "sda"), sda) || !parsePin(find(cmd, "scl"), scl) || sda == scl) {
      return err(reply, reply_len, "bad_pin");
    }
    bool driver_bus = (sda == i2c_sda_ && scl == i2c_scl_) || (sda == i2c_scl_ && scl == i2c_sda_);
    int pins[2] = {sda, scl};
    for (int pin : pins) {
      uint8_t flags = board_.pinFlags(pin);
      if (!(flags & kPinUsable) || (flags & kPinInputOnly)) return err(reply, reply_len, "pin_reserved");
      if (board_.pinInUse(pin) || channels_[kChannels + pin].active || (pinBus(pin) && !driver_bus)) {
        return err(reply, reply_len, "pin_claimed");
      }
    }
    char devices[96];
    char pcas[48];
    uint8_t first;
    scanPair(sda, scl, devices, sizeof(devices), pcas, sizeof(pcas), first);
    for (int pin : pins) {  // the scan left both pins plain inputs
      if (watched_[pin]) board_.setInput(pin, classes_[pin] == PinClass::Floating ? Pull::Up : Pull::None);
    }
    snprintf(reply, reply_len, "RP1 OK devices=%s pca9685=%s", devices, pcas);
    return;
  }

  if (strcmp(verb, "ARM") == 0) {
    static const char *const keys[] = {"token", "cutoff"};
    if (!fieldsOk(cmd, keys, 2, 2)) return err(reply, reply_len, "missing_arm_fields");
    if (estop_) return err(reply, reply_len, "estop_latched");
    if (armed_) return err(reply, reply_len, "already_armed");
    if (strcmp(find(cmd, "cutoff"), "confirmed") != 0) return err(reply, reply_len, "cutoff_unconfirmed");
    const char *token = find(cmd, "token");
    if (!validToken(token)) return err(reply, reply_len, "bad_token");
    if (!driver_.available()) return err(reply, reply_len, "driver_unavailable");
    strcpy(token_, token);
    armed_ = true;
    fault_ = Fault::None;
    last_feed_ms_ = now_ms;
    snprintf(reply, reply_len, "RP1 OK armed=1");
    return;
  }

  if (strcmp(verb, "DISARM") == 0) {
    if (cmd.count != 0) return err(reply, reply_len, "bad_fields");
    releaseAll(Fault::None);
    snprintf(reply, reply_len, "RP1 OK armed=0");
    return;
  }

  if (strcmp(verb, "STOP") == 0) {
    if (cmd.count != 0) return err(reply, reply_len, "bad_fields");
    releaseAll(Fault::Stop);
    snprintf(reply, reply_len, "RP1 OK stopped=1");
    return;
  }

  if (strcmp(verb, "ESTOP") == 0) {
    if (cmd.count != 0) return err(reply, reply_len, "bad_fields");
    externalEstop();
    snprintf(reply, reply_len, "RP1 OK estop=1");
    return;
  }

  if (strcmp(verb, "ESTOP_CLEAR") == 0) {
    static const char *const keys[] = {"cutoff"};
    if (!fieldsOk(cmd, keys, 1, 1)) return err(reply, reply_len, "bad_fields");
    if (strcmp(find(cmd, "cutoff"), "confirmed") != 0) return err(reply, reply_len, "cutoff_unconfirmed");
    estop_ = false;
    snprintf(reply, reply_len, "RP1 OK estop=0");
    return;
  }

  if (strcmp(verb, "LIMIT") == 0) {
    static const char *const keys[] = {"channel", "min_us", "neutral_us", "max_us", "slew_us_s"};
    if (!fieldsOk(cmd, keys, 5, 4)) return err(reply, reply_len, "bad_fields");
    uint32_t channel, min_us, neutral_us, max_us, slew = kDefaultSlewUsPerS;
    int slot;
    if (!parseChannel(find(cmd, "channel"), channel, slot)) return err(reply, reply_len, "bad_channel");
    if (!parseUint(find(cmd, "min_us"), kAbsMaxUs, min_us) ||
        !parseUint(find(cmd, "neutral_us"), kAbsMaxUs, neutral_us) ||
        !parseUint(find(cmd, "max_us"), kAbsMaxUs, max_us) || min_us < kAbsMinUs || min_us >= max_us ||
        neutral_us < min_us || neutral_us > max_us) {
      return err(reply, reply_len, "bad_limits");
    }
    if (find(cmd, "slew_us_s") && (!parseUint(find(cmd, "slew_us_s"), kMaxSlewUsPerS, slew) || slew == 0)) {
      return err(reply, reply_len, "bad_slew");
    }
    ChannelState &c = channels_[slot];
    // A driven channel may get a new band only if it is already inside it, so
    // a band change can never make the output jump.
    if (c.active && (c.current_us < min_us || c.current_us > max_us || c.target_us < min_us ||
                     c.target_us > max_us)) {
      return err(reply, reply_len, "position_outside_limits");
    }
    c.min_us = static_cast<uint16_t>(min_us);
    c.neutral_us = static_cast<uint16_t>(neutral_us);
    c.max_us = static_cast<uint16_t>(max_us);
    c.slew_us_s = static_cast<uint16_t>(slew);
    snprintf(reply, reply_len, "RP1 OK channel=%lu min_us=%lu neutral_us=%lu max_us=%lu slew_us_s=%lu",
             static_cast<unsigned long>(channel), static_cast<unsigned long>(min_us),
             static_cast<unsigned long>(neutral_us), static_cast<unsigned long>(max_us),
             static_cast<unsigned long>(slew));
    return;
  }

  if (strcmp(verb, "MOVE") == 0) {
    static const char *const keys[] = {"channel", "target_us", "seq", "token"};
    if (!fieldsOk(cmd, keys, 4, 3)) return err(reply, reply_len, "bad_move_fields");
    if (estop_) return err(reply, reply_len, "estop_latched");
    if (!armed_) return err(reply, reply_len, "disarmed");
    const char *token = find(cmd, "token");
    if (!token) return err(reply, reply_len, "token_required");
    if (strcmp(token, token_) != 0) return err(reply, reply_len, "bad_token");
    uint32_t channel, target, seq;
    int slot;
    if (!parseChannel(find(cmd, "channel"), channel, slot)) return err(reply, reply_len, "bad_channel");
    if (!parseUint(find(cmd, "target_us"), kAbsMaxUs, target) ||
        !parseUint(find(cmd, "seq"), 0xFFFFFFFFUL, seq)) {
      return err(reply, reply_len, "bad_move_values");
    }
    ChannelState &c = channels_[slot];
    if (!c.active) return err(reply, reply_len, "not_started");
    if (c.blinking) return err(reply, reply_len, "pin_claimed");
    if (target < c.min_us || target > c.max_us) return err(reply, reply_len, "out_of_limits");
    c.target_us = static_cast<uint16_t>(target);
    last_feed_ms_ = now_ms;
    snprintf(reply, reply_len, "RP1 OK seq=%s channel=%s target_us=%s", find(cmd, "seq"), find(cmd, "channel"),
             find(cmd, "target_us"));
    return;
  }

  if (strcmp(verb, "START") == 0) {
    // First PWM on a PCA9685 channel, at a pulse the host chose explicitly. A
    // hobby servo reports no position, so this is where it will go; the
    // firmware never picks a start pulse on its own. (GPIO pins: PROBE.)
    static const char *const keys[] = {"channel", "pulse_us", "token"};
    if (!fieldsOk(cmd, keys, 3, 3)) return err(reply, reply_len, "bad_fields");
    if (estop_) return err(reply, reply_len, "estop_latched");
    if (!armed_) return err(reply, reply_len, "disarmed");
    if (strcmp(find(cmd, "token"), token_) != 0) return err(reply, reply_len, "bad_token");
    uint32_t channel, pulse;
    int slot;
    if (!parseChannel(find(cmd, "channel"), channel, slot) || slot >= kChannels) {
      return err(reply, reply_len, "bad_channel");
    }
    if (!parseUint(find(cmd, "pulse_us"), kAbsMaxUs, pulse)) return err(reply, reply_len, "bad_start_values");
    ChannelState &c = channels_[slot];
    if (c.active) return err(reply, reply_len, "already_started");
    if (pulse < c.min_us || pulse > c.max_us) return err(reply, reply_len, "out_of_limits");
    const char *why = driver_.startProblem(static_cast<uint8_t>(slot));
    if (why) return err(reply, reply_len, why);
    c.active = true;
    c.current_us = static_cast<uint16_t>(pulse);
    c.target_us = static_cast<uint16_t>(pulse);
    c.slew_acc = 0;
    c.last_ms = now_ms;
    if (!writeChannel(slot)) {
      driverFault();
      return err(reply, reply_len, "driver_error");
    }
    last_feed_ms_ = now_ms;
    snprintf(reply, reply_len, "RP1 OK channel=%s started_us=%s", find(cmd, "channel"), find(cmd, "pulse_us"));
    return;
  }

  if (strcmp(verb, "PROBE") == 0 || strcmp(verb, "BLINK") == 0) {
    // One GPIO, operator-triggered: servo PWM at an explicit start pulse
    // (channel 100+pin, then LIMIT/MOVE/STATUS/RELEASE), or a 2 Hz on/off
    // blink that releases the pin when done.
    bool probe = verb[0] == 'P';
    static const char *const probe_keys[] = {"pin", "mode", "start_us", "token"};
    static const char *const blink_keys[] = {"pin", "count", "token"};
    if (probe ? !fieldsOk(cmd, probe_keys, 4, 4) : !fieldsOk(cmd, blink_keys, 3, 3)) {
      return err(reply, reply_len, "bad_fields");
    }
    if (estop_) return err(reply, reply_len, "estop_latched");
    if (!armed_) return err(reply, reply_len, "not_armed");
    if (strcmp(find(cmd, "token"), token_) != 0) return err(reply, reply_len, "bad_token");
    int gpio;
    if (!parsePin(find(cmd, "pin"), gpio)) return err(reply, reply_len, "bad_pin");
    int slot = kChannels + gpio;
    ChannelState &c = channels_[slot];
    uint32_t value;
    if (probe) {
      if (strcmp(find(cmd, "mode"), "servo") != 0) return err(reply, reply_len, "bad_fields");
      if (!parseUint(find(cmd, "start_us"), kAbsMaxUs, value)) return err(reply, reply_len, "bad_start_values");
      if (!c.active && (value < c.min_us || value > c.max_us)) return err(reply, reply_len, "out_of_limits");
    } else if (!parseUint(find(cmd, "count"), 10, value) || value == 0) {
      return err(reply, reply_len, "bad_fields");
    }
    const char *why = drivablePin(gpio);
    if (!why && probe) why = driver_.startProblem(static_cast<uint8_t>(slot));
    if (why) return err(reply, reply_len, why);
    c.active = true;
    bool ok;
    if (probe) {
      c.current_us = static_cast<uint16_t>(value);
      c.target_us = static_cast<uint16_t>(value);
      c.slew_acc = 0;
      c.last_ms = now_ms;
      ok = writeChannel(slot);
    } else {
      c.blinking = true;
      c.blink_high = true;
      c.blink_left = static_cast<uint8_t>(value * 2 - 1);
      c.blink_next_ms = now_ms + kBlinkHalfMs;
      ok = driver_.writeLevel(static_cast<uint8_t>(slot), true);
    }
    if (!ok) {
      driverFault();
      return err(reply, reply_len, "driver_error");
    }
    last_feed_ms_ = now_ms;
    if (probe) {
      snprintf(reply, reply_len, "RP1 OK pin=%d channel=%d", gpio, kGpioBase + gpio);
    } else {
      snprintf(reply, reply_len, "RP1 OK pin=%d blinked=%lu", gpio, static_cast<unsigned long>(value));
    }
    return;
  }

  if (strcmp(verb, "RELEASE") == 0) {
    static const char *const keys[] = {"pin", "token"};
    if (!fieldsOk(cmd, keys, 2, 2)) return err(reply, reply_len, "bad_fields");
    if (!armed_) return err(reply, reply_len, "not_armed");
    if (strcmp(find(cmd, "token"), token_) != 0) return err(reply, reply_len, "bad_token");
    int gpio;
    if (!parsePin(find(cmd, "pin"), gpio)) return err(reply, reply_len, "bad_pin");
    ChannelState &c = channels_[kChannels + gpio];
    if (c.active) {
      bool ok = driver_.release(static_cast<uint8_t>(kChannels + gpio));
      resetChannel(c);
      if (!ok) {
        driverFault();
        return err(reply, reply_len, "driver_error");
      }
    }
    last_feed_ms_ = now_ms;
    snprintf(reply, reply_len, "RP1 OK pin=%d released=1", gpio);
    return;
  }

  if (strcmp(verb, "HB") == 0) {
    static const char *const keys[] = {"token"};
    if (!fieldsOk(cmd, keys, 1, 1)) return err(reply, reply_len, "bad_fields");
    if (estop_) return err(reply, reply_len, "estop_latched");
    if (!armed_) return err(reply, reply_len, "disarmed");
    if (strcmp(find(cmd, "token"), token_) != 0) return err(reply, reply_len, "bad_token");
    last_feed_ms_ = now_ms;
    snprintf(reply, reply_len, "RP1 OK hb=1");
    return;
  }

  if (strcmp(verb, "STATUS") == 0) {
    static const char *const keys[] = {"channel"};
    if (!fieldsOk(cmd, keys, 1, 1)) return err(reply, reply_len, "bad_fields");
    uint32_t channel;
    int slot;
    if (!parseChannel(find(cmd, "channel"), channel, slot)) return err(reply, reply_len, "bad_channel");
    const ChannelState &c = channels_[slot];
    snprintf(reply, reply_len,
             "RP1 OK channel=%lu active=%d current_us=%u target_us=%u min_us=%u neutral_us=%u max_us=%u "
             "slew_us_s=%u armed=%d",
             static_cast<unsigned long>(channel), c.active ? 1 : 0, c.current_us, c.target_us, c.min_us,
             c.neutral_us, c.max_us, c.slew_us_s, armed_ ? 1 : 0);
    return;
  }

  err(reply, reply_len, "unknown_command");
}

LineBuffer::Result LineBuffer::push(char c) {
  if (c == '\n') {
    Result result = overflow_ ? Result::Overflow : (bad_ ? Result::BadBytes : Result::Line);
    buf_[len_] = '\0';
    len_ = 0;
    overflow_ = false;
    bad_ = false;
    return result;
  }
  if (c == '\r') return Result::Pending;
  if (c < 0x20 || c > 0x7e) {
    bad_ = true;
    return Result::Pending;
  }
  if (len_ >= kMaxLine) {
    overflow_ = true;
    return Result::Pending;
  }
  buf_[len_++] = c;
  return Result::Pending;
}

}  // namespace rp
