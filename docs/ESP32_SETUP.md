# ESP32 Setup

## Recommended file

```text
esp32/src/mainn.cpp
```

`platformio.ini` builds only `mainn.cpp` so the old test files stay in the folder without causing duplicate `setup()` / `loop()` errors.

## Pins

| Module | Pin |
|---|---|
| PIR | GPIO 14 |
| LDR | GPIO 34 |
| Light relay | GPIO 26 |
| Fan relay | GPIO 27 |
| RC522 SS | GPIO 5 |
| RC522 RST | GPIO 4 |
| BME280 SDA | GPIO 21 |
| BME280 SCL | GPIO 22 |

## Configuration

Edit these in `mainn.cpp`:

```cpp
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* FLASK_BASE_URL = "http://YOUR_PC_IP:5000";
const char* FIREBASE_DB_URL = "YOUR_FIREBASE_DATABASE_URL";
```

## Communication

The firmware supports both:

```cpp
#define USE_FLASK_API       true
#define USE_FIREBASE_DIRECT true
```

You can turn one off while testing.

## Upload

```bash
cd esp32
pio run -t upload
pio device monitor
```


## Final redo notes

- Firebase Realtime Database is the live database.
- Email verification codes are stored under `/email_verifications` and removed after verification.
- Manual ON/OFF controls are disabled in the UI and ignored in the backend when global Auto mode is active.
- Green LED represents the lighting actuator prototype on GPIO 26.
- Red LED represents the fan/ventilation actuator prototype on GPIO 27.
- `smart_classroom_dataset.csv` now contains the original records plus 5000 converted synthetic rows for ML/demo history.
