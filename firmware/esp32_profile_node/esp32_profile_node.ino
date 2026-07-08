#include <Arduino.h>
#include <Preferences.h>
#include <math.h>

#ifndef LED_BUILTIN
#define LED_BUILTIN 2
#endif

constexpr uint32_t PC_BAUD = 115200;
constexpr size_t MAX_LINE_LEN = 2200;
constexpr uint32_t RESULT_INTERVAL_MS = 1000;
constexpr uint32_t PC_RESULT_STALE_MS = 5000;

Preferences prefs;
String inputLine;
String rawProfileJson = "";
String pcResultJson = "";

struct EdgeProfile {
  String sensorId = "sensor_01";
  String locationId = "unknown_position";
  String profileVersion = "none";
  String mode = "vital_detect";
  int windowSeconds = 8;
  float breathDevMin = 0.02f;
  float vitalRatioMin = 0.08f;
  int confirmSeconds = 6;
  int lostGraceSeconds = 3;
  bool movingReject = true;
  String confidencePolicy = "balanced";
};

EdgeProfile profile;
bool hasProfile = false;
bool survivorCandidate = false;
uint32_t firstDetectedMs = 0;
uint32_t lastDetectedMs = 0;
uint32_t lastPcResultMs = 0;
uint32_t lastResultMs = 0;
uint32_t lastLedMs = 0;
bool ledState = false;

int findJsonValueStart(const String &json, const String &key) {
  const String pattern = "\"" + key + "\"";
  int keyIndex = json.indexOf(pattern);
  if (keyIndex < 0) return -1;
  int colon = json.indexOf(':', keyIndex + pattern.length());
  if (colon < 0) return -1;
  int start = colon + 1;
  while (start < (int)json.length() && isspace(json[start])) start++;
  return start;
}

String readJsonString(const String &json, const String &key, const String &fallback) {
  int start = findJsonValueStart(json, key);
  if (start < 0 || start >= (int)json.length() || json[start] != '"') return fallback;
  start++;
  String result;
  while (start < (int)json.length()) {
    char c = json[start++];
    if (c == '\\' && start < (int)json.length()) {
      result += json[start++];
      continue;
    }
    if (c == '"') return result;
    result += c;
  }
  return fallback;
}

float readJsonFloat(const String &json, const String &key, float fallback) {
  int start = findJsonValueStart(json, key);
  if (start < 0) return fallback;
  int end = start;
  while (end < (int)json.length()) {
    char c = json[end];
    if (!(isdigit(c) || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E')) break;
    end++;
  }
  if (end == start) return fallback;
  return json.substring(start, end).toFloat();
}

int readJsonInt(const String &json, const String &key, int fallback) {
  return (int)readJsonFloat(json, key, fallback);
}

bool readJsonBool(const String &json, const String &key, bool fallback) {
  int start = findJsonValueStart(json, key);
  if (start < 0) return fallback;
  if (json.substring(start, start + 4) == "true") return true;
  if (json.substring(start, start + 5) == "false") return false;
  return fallback;
}

void clampProfile() {
  profile.windowSeconds = constrain(profile.windowSeconds, 5, 12);
  profile.breathDevMin = constrain(profile.breathDevMin, 0.005f, 0.08f);
  profile.vitalRatioMin = constrain(profile.vitalRatioMin, 0.02f, 0.5f);
  profile.confirmSeconds = constrain(profile.confirmSeconds, 3, 15);
  profile.lostGraceSeconds = constrain(profile.lostGraceSeconds, 1, 8);
  if (profile.confidencePolicy != "sensitive" &&
      profile.confidencePolicy != "balanced" &&
      profile.confidencePolicy != "conservative") {
    profile.confidencePolicy = "balanced";
  }
}

void applyProfileJson(const String &json) {
  rawProfileJson = json;
  profile.sensorId = readJsonString(json, "sensor_id", profile.sensorId);
  profile.locationId = readJsonString(json, "location_id", profile.locationId);
  profile.profileVersion = readJsonString(json, "profile_version", profile.profileVersion);
  profile.mode = readJsonString(json, "mode", profile.mode);
  profile.windowSeconds = readJsonInt(json, "window_seconds", profile.windowSeconds);
  profile.breathDevMin = readJsonFloat(json, "breath_dev_min", profile.breathDevMin);
  profile.vitalRatioMin = readJsonFloat(json, "vital_ratio_min", profile.vitalRatioMin);
  profile.confirmSeconds = readJsonInt(json, "confirm_seconds", profile.confirmSeconds);
  profile.lostGraceSeconds = readJsonInt(json, "lost_grace_seconds", profile.lostGraceSeconds);
  profile.movingReject = readJsonBool(json, "moving_reject", profile.movingReject);
  profile.confidencePolicy = readJsonString(json, "confidence_policy", profile.confidencePolicy);
  clampProfile();
  hasProfile = true;

  prefs.putBool("hasProfile", true);
  prefs.putString("rawProfile", rawProfileJson);
}

void loadSavedProfile() {
  hasProfile = prefs.getBool("hasProfile", false);
  rawProfileJson = prefs.getString("rawProfile", "");
  if (hasProfile && rawProfileJson.length() > 0) {
    applyProfileJson(rawProfileJson);
  }
}

void sendAck(const String &kind, const String &extra = "") {
  Serial.print("ACK,");
  Serial.print(kind);
  Serial.print(",sensor_id=");
  Serial.print(profile.sensorId);
  Serial.print(",profile_version=");
  Serial.print(profile.profileVersion);
  Serial.print(",has_profile=");
  Serial.print(hasProfile ? "1" : "0");
  if (extra.length() > 0) {
    Serial.print(",");
    Serial.print(extra);
  }
  Serial.println();
}

float simulatedBreathDeviation() {
  float wave = (sinf(millis() / 2100.0f) + 1.0f) * 0.5f;
  float slow = (sinf(millis() / 7100.0f) + 1.0f) * 0.5f;
  return 0.008f + wave * 0.04f + slow * 0.012f;
}

float simulatedVitalRatio(float breathDev) {
  float ratio = breathDev / max(0.01f, profile.breathDevMin * 3.0f);
  return constrain(ratio * 0.16f, 0.0f, 0.55f);
}

float confidenceFromSignals(float breathDev, float vitalRatio) {
  float breathScore = constrain(breathDev / max(0.001f, profile.breathDevMin * 3.0f), 0.0f, 1.0f);
  float vitalScore = constrain(vitalRatio / max(0.001f, profile.vitalRatioMin * 2.0f), 0.0f, 1.0f);
  float confidence = 0.50f * breathScore + 0.50f * vitalScore;
  if (profile.confidencePolicy == "conservative") confidence *= 0.88f;
  if (profile.confidencePolicy == "sensitive") confidence = min(1.0f, confidence * 1.12f);
  return constrain(confidence, 0.0f, 1.0f);
}

void updateEdgeDecision(float breathDev, float vitalRatio) {
  const uint32_t now = millis();
  const bool instantDetected =
      hasProfile &&
      breathDev >= profile.breathDevMin &&
      vitalRatio >= profile.vitalRatioMin;

  if (instantDetected) {
    if (firstDetectedMs == 0 || (now - lastDetectedMs) > (uint32_t)profile.lostGraceSeconds * 1000UL) {
      firstDetectedMs = now;
    }
    lastDetectedMs = now;
    survivorCandidate = (now - firstDetectedMs) >= (uint32_t)profile.confirmSeconds * 1000UL;
  } else if (lastDetectedMs == 0 || (now - lastDetectedMs) > (uint32_t)profile.lostGraceSeconds * 1000UL) {
    firstDetectedMs = 0;
    survivorCandidate = false;
  }
}

void sendResult() {
  if (pcResultJson.length() > 0 && millis() - lastPcResultMs <= PC_RESULT_STALE_MS) {
    Serial.print("RESULT_JSON ");
    Serial.println(pcResultJson);
    return;
  }

  const float breathDev = simulatedBreathDeviation();
  const float vitalRatio = simulatedVitalRatio(breathDev);
  updateEdgeDecision(breathDev, vitalRatio);
  const float confidence = confidenceFromSignals(breathDev, vitalRatio);

  Serial.print("RESULT_JSON {");
  Serial.print("\"sensor_id\":\""); Serial.print(profile.sensorId); Serial.print("\",");
  Serial.print("\"profile_version\":\""); Serial.print(profile.profileVersion); Serial.print("\",");
  Serial.print("\"has_profile\":"); Serial.print(hasProfile ? "true" : "false"); Serial.print(",");
  Serial.print("\"status\":\""); Serial.print(survivorCandidate ? "SURVIVOR_CANDIDATE" : "CLEAR"); Serial.print("\",");
  Serial.print("\"person_count\":"); Serial.print(survivorCandidate ? 1 : 0); Serial.print(",");
  Serial.print("\"survivor_candidate\":"); Serial.print(survivorCandidate ? "true" : "false"); Serial.print(",");
  Serial.print("\"confidence\":"); Serial.print(confidence, 3); Serial.print(",");
  Serial.print("\"breath_deviation\":"); Serial.print(breathDev, 4); Serial.print(",");
  Serial.print("\"vital_ratio\":"); Serial.print(vitalRatio, 3); Serial.print(",");
  Serial.print("\"source\":\"esp32_profile_simulator\",");
  Serial.print("\"simulated\":true,");
  Serial.print("\"uptime_ms\":"); Serial.print(millis());
  Serial.println("}");
}

void printProfile() {
  Serial.print("PROFILE_CURRENT ");
  if (rawProfileJson.length() > 0) {
    Serial.println(rawProfileJson);
  } else {
    Serial.println("{}");
  }
}

void handleCommand(String line) {
  line.trim();
  if (line.length() == 0) return;

  if (line == "PING") {
    sendAck("PONG", "uptime_ms=" + String(millis()));
    return;
  }

  if (line == "RESULT?") {
    sendResult();
    return;
  }

  if (line.startsWith("PC_RESULT_JSON ")) {
    pcResultJson = line.substring(String("PC_RESULT_JSON ").length());
    lastPcResultMs = millis();
    survivorCandidate = readJsonBool(pcResultJson, "survivor_candidate", false);
    sendAck("PC_RESULT", "pc_result_age_ms=0");
    return;
  }

  if (line == "PROFILE?") {
    printProfile();
    return;
  }

  if (line == "RESET_PROFILE") {
    prefs.clear();
    rawProfileJson = "";
    hasProfile = false;
    profile = EdgeProfile{};
    sendAck("RESET");
    return;
  }

  if (line.startsWith("PROFILE_JSON ")) {
    const String json = line.substring(String("PROFILE_JSON ").length());
    applyProfileJson(json);
    sendAck("PROFILE", "window_seconds=" + String(profile.windowSeconds));
    return;
  }

  Serial.print("ERR,UNKNOWN_COMMAND,line=");
  Serial.println(line.substring(0, 80));
}

void updateLed() {
  const uint32_t now = millis();
  if (!hasProfile) {
    digitalWrite(LED_BUILTIN, (now / 700) % 2 ? HIGH : LOW);
    return;
  }
  if (survivorCandidate) {
    digitalWrite(LED_BUILTIN, HIGH);
    return;
  }
  if (now - lastLedMs >= 250) {
    ledState = !ledState;
    digitalWrite(LED_BUILTIN, ledState ? HIGH : LOW);
    lastLedMs = now;
  }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(PC_BAUD);
  prefs.begin("radarProfile", false);
  delay(300);
  loadSavedProfile();
  Serial.println("BOOT,esp32_profile_node,version=0.1");
  sendAck("READY");
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n') {
      handleCommand(inputLine);
      inputLine = "";
    } else if (c != '\r') {
      if (inputLine.length() < MAX_LINE_LEN) {
        inputLine += c;
      } else {
        inputLine = "";
        Serial.println("ERR,LINE_TOO_LONG");
      }
    }
  }

  if (millis() - lastResultMs >= RESULT_INTERVAL_MS) {
    lastResultMs = millis();
    if (hasProfile) {
      sendResult();
    }
  }

  updateLed();
  delay(2);
}
