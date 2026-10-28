/*
  Smart Classroom - ESP32 Final Recommended Firmware
  --------------------------------------------------
  Sensors:
    BME280  -> temperature, humidity, pressure (I2C SDA=21, SCL=22)
    PIR     -> movement detection
    LDR     -> light intensity (analog)
    RC522   -> RFID card scan for occupation/presence (SPI)
  Actuators:
    LIGHT_RELAY_PIN -> green LED prototype representing classroom light/relay
    FAN_RELAY_PIN   -> red LED prototype representing fan/ventilation relay

  Communication:
    1) ESP32 -> Flask API: POST /api/sensor_data and GET /get_control
    2) ESP32 -> Firebase Realtime Database through REST API
       This direct Firebase mode is optional. For tests, you can keep it false
       and use only Flask. If your RTDB rules require auth, fill FIREBASE_AUTH.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>

// ================= USER CONFIGURATION =================
#define USE_FLASK_API       true
#define USE_FIREBASE_DIRECT true

const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// Flask server on your computer. Example: http://192.168.1.20:5000
const char* FLASK_BASE_URL = "http://YOUR_PC_IP:5000";

// Firebase Realtime Database URL WITHOUT ending slash.
// Example: https://your-project-id-default-rtdb.firebaseio.com
const char* FIREBASE_DB_URL = "YOUR_FIREBASE_DATABASE_URL";

// Optional RTDB database secret/token. Leave empty if your development rules allow writes.
const char* FIREBASE_AUTH = "";

// ================= PIN CONFIGURATION =================
#define PIR_PIN          14
#define LDR_PIN          34
#define LIGHT_RELAY_PIN  26  // Green LED = lighting actuator prototype
#define FAN_RELAY_PIN    27  // Red LED = fan/ventilation actuator prototype
#define LED_BUILTIN_PIN  2

// RFID RC522 pins
#define RFID_SS_PIN      5
#define RFID_RST_PIN     4

// BME280 I2C pins
#define BME_SDA_PIN      21
#define BME_SCL_PIN      22

// ================= TIMING =================
const unsigned long SEND_INTERVAL_MS = 5000;    // send sensor data every ~5 seconds
const unsigned long CONTROL_INTERVAL_MS = 3000; // read commands every ~3 seconds
const unsigned long WIFI_RETRY_MS = 10000;

// ================= OCCUPATION CONFIG =================
const int TOTAL_EXPECTED_CARDS = 20;  // change to real class size
String knownCards[50];
int knownCardCount = 0;

// ================= OBJECTS =================
Adafruit_BME280 bme;
MFRC522 rfid(RFID_SS_PIN, RFID_RST_PIN);

// ================= STATE =================
bool bmeOk = false;
unsigned long lastSend = 0;
unsigned long lastControl = 0;
unsigned long lastWifiRetry = 0;

int currentLightState = 0;
int currentFanState = 0;
String currentMode = "auto";
String lastRfidUid = "";

// Fallback values if BME280 is not connected yet
float simulatedTemp = 24.0;
float simulatedHumidity = 55.0;
float simulatedPressure = 1013.25;

// ================= HELPERS =================
String firebaseUrl(String path) {
  String url = String(FIREBASE_DB_URL);
  if (url.endsWith("/")) url.remove(url.length() - 1);
  if (!path.startsWith("/")) url += "/";
  url += path;
  url += ".json";
  if (String(FIREBASE_AUTH).length() > 0) {
    url += "?auth=";
    url += FIREBASE_AUTH;
  }
  return url;
}

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  if (millis() - lastWifiRetry < WIFI_RETRY_MS) return;
  lastWifiRetry = millis();

  Serial.print("[WiFi] Connecting to ");
  Serial.println(WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 8000) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[WiFi] Connected. IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("[WiFi] Not connected yet.");
  }
}

void initBME280() {
  Wire.begin(BME_SDA_PIN, BME_SCL_PIN);
  delay(100);

  if (bme.begin(0x76) || bme.begin(0x77)) {
    bmeOk = true;
    Serial.println("[BME280] Found.");
  } else {
    bmeOk = false;
    Serial.println("[BME280] Not found. Using simulated values until sensor is connected.");
  }
}

float readTemperature() {
  if (bmeOk) return bme.readTemperature();
  simulatedTemp += random(-2, 3) * 0.1;
  return simulatedTemp;
}

float readHumidity() {
  if (bmeOk) return bme.readHumidity();
  simulatedHumidity += random(-2, 3) * 0.2;
  return simulatedHumidity;
}

float readPressure() {
  if (bmeOk) return bme.readPressure() / 100.0F;
  simulatedPressure += random(-2, 3) * 0.1;
  return simulatedPressure;
}

String uidToString(MFRC522::Uid *uid) {
  String result = "";
  for (byte i = 0; i < uid->size; i++) {
    if (uid->uidByte[i] < 0x10) result += "0";
    result += String(uid->uidByte[i], HEX);
  }
  result.toUpperCase();
  return result;
}

bool cardAlreadyKnown(String uid) {
  for (int i = 0; i < knownCardCount; i++) {
    if (knownCards[i] == uid) return true;
  }
  return false;
}

void updateRFID() {
  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) return;

  String uid = uidToString(&rfid.uid);
  lastRfidUid = uid;

  if (!cardAlreadyKnown(uid) && knownCardCount < 50) {
    knownCards[knownCardCount++] = uid;
    Serial.print("[RFID] New card counted: ");
    Serial.println(uid);
  } else {
    Serial.print("[RFID] Card seen: ");
    Serial.println(uid);
  }

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
}

int occupationRate() {
  if (TOTAL_EXPECTED_CARDS <= 0) return 0;
  int rate = (knownCardCount * 100) / TOTAL_EXPECTED_CARDS;
  if (rate > 100) rate = 100;
  return rate;
}

void applyRelayStates() {
  digitalWrite(LIGHT_RELAY_PIN, currentLightState ? HIGH : LOW);
  digitalWrite(FAN_RELAY_PIN, currentFanState ? HIGH : LOW);
  digitalWrite(LED_BUILTIN_PIN, (currentLightState || currentFanState) ? HIGH : LOW);
}

DynamicJsonDocument buildSensorJson() {
  float temperature = readTemperature();
  float humidity = readHumidity();
  float pressure = readPressure();
  int pir = digitalRead(PIR_PIN);
  int ldr = analogRead(LDR_PIN);

  DynamicJsonDocument doc(768);
  doc["temperature"] = temperature;
  doc["humidity"] = humidity;
  doc["pressure"] = pressure;
  doc["pir"] = pir;
  doc["motion"] = pir == HIGH;
  doc["ldr"] = ldr;
  doc["light_level"] = ldr;
  doc["rfid"] = lastRfidUid;
  doc["rfid_present"] = lastRfidUid.length() > 0;
  doc["occupancy"] = knownCardCount;
  doc["occupation_count"] = knownCardCount;
  doc["taux_occupation"] = occupationRate();
  doc["occupation_rate"] = occupationRate();
  doc["fan_state"] = currentFanState ? "ON" : "OFF";
  doc["light_state"] = currentLightState ? "ON" : "OFF";
  doc["source"] = "esp32";
  doc["esp_timestamp"] = millis();
  return doc;
}

bool httpJsonRequest(const char* method, const String& url, const String& body, String& response) {
  if (WiFi.status() != WL_CONNECTED) return false;

  HTTPClient http;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");

  int code = -1;
  if (String(method) == "GET") {
    code = http.GET();
  } else if (String(method) == "POST") {
    code = http.POST(body);
  } else if (String(method) == "PUT") {
    code = http.PUT(body);
  } else if (String(method) == "PATCH") {
    code = http.PATCH(body);
  }

  response = http.getString();
  http.end();

  Serial.print("[HTTP] ");
  Serial.print(method);
  Serial.print(" ");
  Serial.print(url);
  Serial.print(" -> ");
  Serial.println(code);

  return code >= 200 && code < 300;
}

void sendSensorDataToFlask(const String& payload) {
  if (!USE_FLASK_API) return;
  String response;
  String url = String(FLASK_BASE_URL) + "/api/sensor_data";
  if (httpJsonRequest("POST", url, payload, response)) {
    DynamicJsonDocument res(512);
    if (deserializeJson(res, response) == DeserializationError::Ok) {
      if (res.containsKey("light")) currentLightState = res["light"];
      if (res.containsKey("ventilation")) currentFanState = res["ventilation"];
      applyRelayStates();
    }
  }
}

void sendSensorDataToFirebase(const String& payload) {
  if (!USE_FIREBASE_DIRECT) return;

  String response;
  httpJsonRequest("PUT", firebaseUrl("/latest_data"), payload, response);
  httpJsonRequest("POST", firebaseUrl("/sensor_history"), payload, response);
  httpJsonRequest("PUT", firebaseUrl("/esp32_status"), "{\"connected\":true,\"source\":\"esp32\"}", response);
}

void readControlFromFlask() {
  if (!USE_FLASK_API) return;

  String response;
  String url = String(FLASK_BASE_URL) + "/get_control";
  if (httpJsonRequest("GET", url, "", response)) {
    DynamicJsonDocument doc(512);
    if (deserializeJson(doc, response) == DeserializationError::Ok) {
      if (doc.containsKey("light")) currentLightState = doc["light"];
      if (doc.containsKey("ventilation")) currentFanState = doc["ventilation"];
      if (doc.containsKey("mode")) currentMode = String((const char*)doc["mode"]);
      applyRelayStates();
    }
  }
}

void readControlFromFirebase() {
  if (!USE_FIREBASE_DIRECT) return;

  String response;
  if (httpJsonRequest("GET", firebaseUrl("/control_state"), "", response)) {
    DynamicJsonDocument doc(768);
    if (deserializeJson(doc, response) == DeserializationError::Ok) {
      if (doc.containsKey("light_state")) currentLightState = String((const char*)doc["light_state"]) == "ON";
      if (doc.containsKey("fan")) currentFanState = String((const char*)doc["fan"]) == "ON";
      else if (doc.containsKey("ventilation_state")) currentFanState = String((const char*)doc["ventilation_state"]) == "ON";
      if (doc.containsKey("mode")) currentMode = String((const char*)doc["mode"]);
      applyRelayStates();
    }
  }
}

// ================= SETUP / LOOP =================
void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(PIR_PIN, INPUT);
  pinMode(LDR_PIN, INPUT);
  pinMode(LIGHT_RELAY_PIN, OUTPUT);
  pinMode(FAN_RELAY_PIN, OUTPUT);
  pinMode(LED_BUILTIN_PIN, OUTPUT);

  applyRelayStates();

  SPI.begin();
  rfid.PCD_Init();

  initBME280();
  ensureWiFi();

  Serial.println("[SYSTEM] Smart Classroom ESP32 firmware started.");
}

void loop() {
  ensureWiFi();
  updateRFID();

  unsigned long now = millis();

  if (now - lastControl >= CONTROL_INTERVAL_MS) {
    lastControl = now;
    readControlFromFirebase();
    readControlFromFlask();
  }

  if (now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;

    DynamicJsonDocument doc = buildSensorJson();
    String payload;
    serializeJson(doc, payload);

    Serial.print("[DATA] ");
    Serial.println(payload);

    sendSensorDataToFirebase(payload);
    sendSensorDataToFlask(payload);

    // Do not keep resending the same RFID as "new"; occupation count remains stored.
    lastRfidUid = "";
  }
}
