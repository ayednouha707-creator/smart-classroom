# ESP32 Setup — Smart Classroom

Recommended firmware: `esp32/src/mainn.cpp`.

## Sensors and modules

| Module | Role | Pins |
|---|---|---|
| BME280 | temperature, humidity, pressure | SDA 21, SCL 22 |
| PIR | movement detection | GPIO 14 |
| LDR | light intensity | GPIO 34 |
| RFID RC522 | occupation / presence using cards | SS 5, RST 4, SPI default |
| Light relay | lighting control | GPIO 26 |
| Fan relay | ventilation control | GPIO 27 |

## Before upload

Open `src/mainn.cpp` and change:

```cpp
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* FLASK_BASE_URL = "http://YOUR_PC_IP:5000";
const char* FIREBASE_DB_URL = "YOUR_FIREBASE_DATABASE_URL";
```

For direct Firebase mode, your Realtime Database rules must allow the ESP32 to write/read, or you must provide a token in `FIREBASE_AUTH`.

## Communication modes

```cpp
#define USE_FLASK_API       true
#define USE_FIREBASE_DIRECT true
```

You can set one of them to `false` during tests.

## Upload with PlatformIO

1. Open the `esp32/` folder in VS Code.
2. Install PlatformIO extension.
3. Connect ESP32.
4. Build and upload.
5. Open Serial Monitor at `115200`.

The firmware sends data every ~5 seconds and reads control commands every ~3 seconds.
