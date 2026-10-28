# Smart Classroom

An IoT system that monitors a classroom in real time and controls lighting and ventilation automatically.
Built as my semester project at ISET Bizerte (Feb–May 2026, supervised by Mrs. Wided Maghraoui). I built the whole chain myself: firmware, server, web dashboard and the machine-learning part.

## How it works

```
ESP32 + sensors ──HTTP/JSON──► Flask server ──► SQLite / CSV / Firebase RTDB
      ▲                             │
      └──── light / fan commands ◄──┘──► Web dashboard (admin / teacher / student views)
```

1. The ESP32 reads the sensors every few seconds and sends the values to the Flask API (`POST /api/sensor_data`), and can also write directly to Firebase.
2. The server stores the data, computes the occupancy rate and runs the model to decide whether the light and the fan should be on.
3. The ESP32 polls `GET /api/controls` and switches the actuators.
4. The dashboard refreshes every 3 seconds and shows live values, history and alerts.

## Hardware

| Part | Use | Pin |
|---|---|---|
| ESP32 DevKit | main controller | – |
| BME280 (I2C) | temperature, humidity, pressure | SDA 21, SCL 22 |
| PIR sensor | motion | GPIO 14 |
| LDR | light level | GPIO 34 |
| RC522 RFID reader | who is in the room (badge scan) | SS 5, RST 4 |
| Green LED / relay | lighting actuator (prototype) | GPIO 26 |
| Red LED / relay | fan / ventilation actuator (prototype) | GPIO 27 |

The RFID part was tested in class with the badges of other groups.

## Web application

- Flask backend with role-based access (admin controls, read-only users)
- Pages: dashboard, history, manual / auto mode, lighting, ventilation, users, admin
- Sign-up with e-mail verification code
- Manual controls are locked while global Auto mode is on
- Data saved in SQLite (local), CSV (dataset) and Firebase Realtime Database (cloud)

## Machine learning

`train_model.py` tries to forecast the room temperature 5 minutes ahead from the sensor history (lag, rolling-mean and time features; Gradient Boosting vs Random Forest, keeps the best).

Honest result: the model does **not** forecast well yet (the saved model has a negative R²). Most of the real recordings were short sessions with gaps, so after cleaning only a few hundred usable rows were left. The main lessons for me were about data quality: missing values, a noisy sensor, and sessions that must not be mixed when building lag features. `smart_classroom_dataset.csv` also contains synthetic rows that I generated for demo history; RFID badge IDs are anonymised (`CARD_001`, …).

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your own values (Firebase is optional)
python app.py               # http://localhost:5000
```

Demo accounts are created on first run (`admin@classroom.local` / `admin123`). They are for local testing only; change them before any real use.

Firmware: open `esp32/` in PlatformIO, set your Wi-Fi, server IP and Firebase URL at the top of `src/mainn.cpp`, then build and upload. `src/rfidtest.cpp` is a standalone RFID test.

## Repository layout

```
app.py              Flask backend and API
train_model.py      model training
templates/, static/ dashboard pages and CSS
esp32/              PlatformIO firmware
firebase/           database structure and rules example
docs/               setup notes
```

## Author

Nouha Ayed — Licence in IT (Embedded & Mobile Systems), ISET Bizerte
