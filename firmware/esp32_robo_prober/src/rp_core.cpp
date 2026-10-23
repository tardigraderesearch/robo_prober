#include "rp_core.h"

#include <stdio.h>
#include <string.h>

namespace rp {
namespace {

constexpr int kMaxFields = 8;
constexpr uint32_t kMaxSlewDtMs = 60000;
constexpr const char *kCaps = "limit,status,hb,token,estop,start";

struct Field {
  const char *key;
  const char *value;
};

struct Command {
  const char *verb;
  Field fields[kMaxFields];
  int count;
};

bool isVerbChar(char c) { return (c >= 'A' && c <= 'Z') || c == '_'; }
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

Core::Core(OutputDriver &driver, const char *device_id, const char *outputs_name, uint32_t watchdog_ms)
    : driver_(driver), device_id_(device_id), outputs_name_(outputs_name), watchdog_ms_(watchdog_ms) {
  resetChannels();
}

void Core::resetChannels() {
  for (int i = 0; i < kChannels; ++i) {
    ChannelState &c = channels_[i];
    c.min_us = kDefaultMinUs;
    c.neutral_us = kDefaultNeutralUs;
    c.max_us = kDefaultMaxUs;
    c.slew_us_s = kDefaultSlewUsPerS;
    c.active = false;
    c.current_us = 0;
    c.target_us = 0;
    c.slew_acc = 0;
    c.last_ms = 0;
  }
}

void Core::begin(uint32_t now_ms) {
  resetChannels();
  armed_ = false;
  estop_ = false;
  fault_ = Fault::None;
  token_[0] = '\0';
  last_feed_ms_ = now_ms;
  driver_.releaseAll();
}

void Core::releaseAll(Fault reason) {
  // Always clear software state, even if the driver reports an error.
  bool ok = driver_.releaseAll();
  armed_ = false;
  token_[0] = '\0';
  for (int i = 0; i < kChannels; ++i) {
    ChannelState &c = channels_[i];
    // A band lives for one armed session: the next session must send its
    // own LIMIT, or it gets the conservative default band.
    c.min_us = kDefaultMinUs;
    c.neutral_us = kDefaultNeutralUs;
    c.max_us = kDefaultMaxUs;
    c.slew_us_s = kDefaultSlewUsPerS;
    c.active = false;
    c.current_us = 0;
    c.target_us = 0;
    c.slew_acc = 0;
  }
  if (reason != Fault::None) fault_ = reason;
  if (!ok) fault_ = Fault::Driver;
}

void Core::driverFault() { releaseAll(Fault::Driver); }

void Core::externalEstop() {
  releaseAll(Fault::Estop);
  estop_ = true;
}

bool Core::writeChannel(int index) {
  ChannelState &c = channels_[index];
  uint16_t pulse = c.current_us;
  if (pulse < c.min_us) pulse = c.min_us;
  if (pulse > c.max_us) pulse = c.max_us;
  return driver_.writePulseUs(static_cast<uint8_t>(index), pulse);
}

void Core::tick(uint32_t now_ms) {
  if (armed_ && static_cast<uint32_t>(now_ms - last_feed_ms_) > watchdog_ms_) {
    releaseAll(Fault::Watchdog);
    return;
  }
  for (int i = 0; i < kChannels; ++i) {
    ChannelState &c = channels_[i];
    if (!c.active) continue;
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
             "RP1 OK proto=%s mode=%s id=%s fw=%s outputs=%s watchdog_ms=%lu caps=%s fault=%s", kProtocol, mode,
             device_id_, kFirmwareVersion, outputs_name_, static_cast<unsigned long>(watchdog_ms_), kCaps,
             faultName(fault_));
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
    if (!parseUint(find(cmd, "channel"), kChannels - 1, channel)) return err(reply, reply_len, "bad_channel");
    if (!parseUint(find(cmd, "min_us"), kAbsMaxUs, min_us) ||
        !parseUint(find(cmd, "neutral_us"), kAbsMaxUs, neutral_us) ||
        !parseUint(find(cmd, "max_us"), kAbsMaxUs, max_us) || min_us < kAbsMinUs || min_us >= max_us ||
        neutral_us < min_us || neutral_us > max_us) {
      return err(reply, reply_len, "bad_limits");
    }
    if (find(cmd, "slew_us_s") && (!parseUint(find(cmd, "slew_us_s"), kMaxSlewUsPerS, slew) || slew == 0)) {
      return err(reply, reply_len, "bad_slew");
    }
    ChannelState &c = channels_[channel];
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
    if (!parseUint(find(cmd, "channel"), kChannels - 1, channel)) return err(reply, reply_len, "bad_channel");
    if (!parseUint(find(cmd, "target_us"), kAbsMaxUs, target) ||
        !parseUint(find(cmd, "seq"), 0xFFFFFFFFUL, seq)) {
      return err(reply, reply_len, "bad_move_values");
    }
    ChannelState &c = channels_[channel];
    if (!c.active) return err(reply, reply_len, "not_started");
    if (target < c.min_us || target > c.max_us) return err(reply, reply_len, "out_of_limits");
    c.target_us = static_cast<uint16_t>(target);
    last_feed_ms_ = now_ms;
    snprintf(reply, reply_len, "RP1 OK seq=%s channel=%s target_us=%s", find(cmd, "seq"), find(cmd, "channel"),
             find(cmd, "target_us"));
    return;
  }

  if (strcmp(verb, "START") == 0) {
    // First PWM on a channel, at a pulse the host chose explicitly. A hobby
    // servo reports no position, so this is where it will go; the firmware
    // never picks a start pulse on its own.
    static const char *const keys[] = {"channel", "pulse_us", "token"};
    if (!fieldsOk(cmd, keys, 3, 3)) return err(reply, reply_len, "bad_fields");
    if (estop_) return err(reply, reply_len, "estop_latched");
    if (!armed_) return err(reply, reply_len, "disarmed");
    if (strcmp(find(cmd, "token"), token_) != 0) return err(reply, reply_len, "bad_token");
    uint32_t channel, pulse;
    if (!parseUint(find(cmd, "channel"), kChannels - 1, channel)) return err(reply, reply_len, "bad_channel");
    if (!parseUint(find(cmd, "pulse_us"), kAbsMaxUs, pulse)) return err(reply, reply_len, "bad_start_values");
    ChannelState &c = channels_[channel];
    if (c.active) return err(reply, reply_len, "already_started");
    if (pulse < c.min_us || pulse > c.max_us) return err(reply, reply_len, "out_of_limits");
    c.active = true;
    c.current_us = static_cast<uint16_t>(pulse);
    c.target_us = static_cast<uint16_t>(pulse);
    c.slew_acc = 0;
    c.last_ms = now_ms;
    if (!writeChannel(static_cast<int>(channel))) {
      driverFault();
      return err(reply, reply_len, "driver_error");
    }
    last_feed_ms_ = now_ms;
    snprintf(reply, reply_len, "RP1 OK channel=%s started_us=%s", find(cmd, "channel"), find(cmd, "pulse_us"));
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
    if (!parseUint(find(cmd, "channel"), kChannels - 1, channel)) return err(reply, reply_len, "bad_channel");
    const ChannelState &c = channels_[channel];
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
