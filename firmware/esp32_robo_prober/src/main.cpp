#include <Arduino.h>

// ROBOPROBE/1 reference firmware for a generic ESP32 dev board.
//
// This is a PROTOCOL REFERENCE: it implements the ROBOPROBE/1 line protocol
// (HELLO / ARM / MOVE / STOP / DISARM) and acknowledges MOVE commands exactly
// as the host expects, but it does NOT drive any output. Use it to verify the
// host <-> controller path end to end with nothing attached.
//
// To move real servos, add a PCA9685 output layer where marked below, and only
// after you have a bench setup with a physical power cutoff.

bool armed = false;

String fieldValue(const String &line, const String &key) {
  int start = line.indexOf(key + "=");
  if (start < 0) {
    return "";
  }
  start += key.length() + 1;
  int end = line.indexOf(' ', start);
  if (end < 0) {
    end = line.length();
  }
  return line.substring(start, end);
}

void setup() {
  Serial.begin(115200);
}

void loop() {
  if (!Serial.available()) {
    return;
  }
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line == "HELLO") {
    Serial.println("RP1 OK proto=ROBOPROBE/1 mode=disarmed id=esp32-reference");
  } else if (line.startsWith("ARM ") && line.indexOf("cutoff=confirmed") >= 0 && line.indexOf("token=") >= 0) {
    armed = true;
    Serial.println("RP1 OK armed=1");
  } else if (line == "DISARM") {
    armed = false;
    Serial.println("RP1 OK armed=0");
  } else if (line == "STOP") {
    armed = false;
    Serial.println("RP1 OK stopped=1");
  } else if (line.startsWith("MOVE ") && armed) {
    // Output layer goes here: validate channel (0-15) and target_us, then
    // command the PCA9685. STOP/DISARM must also release all outputs.
    String seq = fieldValue(line, "seq");
    String channel = fieldValue(line, "channel");
    String target = fieldValue(line, "target_us");
    if (seq.length() == 0 || channel.length() == 0 || target.length() == 0) {
      Serial.println("RP1 ERR reason=bad_move_fields");
      return;
    }
    Serial.print("RP1 OK seq=");
    Serial.print(seq);
    Serial.print(" channel=");
    Serial.print(channel);
    Serial.print(" target_us=");
    Serial.println(target);
  } else if (line.startsWith("MOVE ")) {
    Serial.println("RP1 ERR reason=disarmed");
  } else {
    Serial.println("RP1 ERR reason=unknown_command");
  }
}
