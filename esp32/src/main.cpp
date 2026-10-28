#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>

const char* ssid = "YOUR_WIFI_NAME";
const char* password = "YOUR_WIFI_PASSWORD";
const char* sensorApiUrl   = "http://YOUR_PC_IP:5000/api/sensor_data";
const char* controlsApiUrl = "http://YOUR_PC_IP:5000/api/controls";

#define PIR_PIN         14
#define LDR_PIN         34
#define LIGHT_RELAY_PIN 26
#define FAN_RELAY_PIN   27
#define SS_PIN          5
#define RST_PIN         4

MFRC522 rfid(SS_PIN, RST_PIN);
Adafruit_BME280 bme;

bool bmeOk = false;
unsigned long lastSendTime = 0;
const unsigned long sendInterval = 5000;
int currentLightState = 0;
int currentFanState = 0;
String lastRFID = "";
int occupancy = 0;

void connectWiFi() {
  WiFi.begin(ssid, password);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 30) {
    delay(500);
    tries++;
  }
}

void initBME() {
  Wire.begin(21, 22);
  if (bme.begin(0x76) || bme.begin(0x77)) bmeOk = true;
}

String readRFID() {
  if (!rfid.PICC_IsNewCardPresent()) return "";
  if (!rfid.PICC_ReadCardSerial()) return "";
  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  return uid;
}

void applyOutputs(int lightState, int fanState) {
  currentLightState = lightState;
  currentFanState = fanState;
  digitalWrite(LIGHT_RELAY_PIN, lightState ? HIGH : LOW);
  digitalWrite(FAN_RELAY_PIN, fanState ? HIGH : LOW);
}

void getControlsFromFlask() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(controlsApiUrl);
  int httpCode = http.GET();
  if (httpCode > 0) {
    String response = http.getString();
    DynamicJsonDocument doc(512);
    if (deserializeJson(doc, response) == DeserializationError::Ok) {
      applyOutputs(doc["light"] | 0, doc["ventilation"] | 0);
    }
  }
  http.end();
}

void sendSensorDataToFlask() {
  if (WiFi.status() != WL_CONNECTED) return;

  float temperature = 0.0, humidity = 0.0, pressure = 0.0;
  if (bmeOk) {
    temperature = bme.readTemperature();
    humidity = bme.readHumidity();
    pressure = bme.readPressure() / 100.0F;
  }

  int pirValue = digitalRead(PIR_PIN);
  int ldrValue = analogRead(LDR_PIN);
  String scannedRFID = readRFID();
  if (scannedRFID != "") {
    lastRFID = scannedRFID;
    occupancy = 1;
  }
  if (pirValue == HIGH) occupancy = 1;

  DynamicJsonDocument doc(1024);
  doc["temperature"] = temperature;
  doc["humidity"] = humidity;
  doc["pressure"] = pressure;
  doc["pir"] = pirValue;
  doc["ldr"] = ldrValue;
  doc["rfid"] = lastRFID;
  doc["occupancy"] = occupancy;
  doc["esp_timestamp"] = millis();
  doc["light_state"] = currentLightState;
  doc["fan_state"] = currentFanState;

  String jsonData;
  serializeJson(doc, jsonData);

  HTTPClient http;
  http.begin(sensorApiUrl);
  http.addHeader("Content-Type", "application/json");
  http.POST(jsonData);
  http.end();
}

void setup() {
  Serial.begin(115200);
  pinMode(PIR_PIN, INPUT);
  pinMode(LIGHT_RELAY_PIN, OUTPUT);
  pinMode(FAN_RELAY_PIN, OUTPUT);
  digitalWrite(LIGHT_RELAY_PIN, LOW);
  digitalWrite(FAN_RELAY_PIN, LOW);
  SPI.begin();
  rfid.PCD_Init();
  initBME();
  connectWiFi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) connectWiFi();
  if (millis() - lastSendTime >= sendInterval) {
    lastSendTime = millis();
    sendSensorDataToFlask();
    delay(300);
    getControlsFromFlask();
  }
}
