# Project Structure

```text
smart_classroom_final/
├── app.py                       # Flask backend, APIs, Firebase sync, dashboard routes
├── requirements.txt             # Python dependencies
├── .env.example                 # Safe environment template
├── train_model.py               # Offline ML training
├── smart_classroom_dataset.csv  # Main CSV dataset for history/ML fallback
├── ml_training_dataset.csv      # ML-ready dataset
├── templates/                   # Preserved dashboard pages
├── static/                      # Preserved CSS
├── esp32/                       # ESP32 PlatformIO project
├── firebase/                    # RTDB structure/rules examples
└── docs/                        # Setup documentation
```

## Kept unchanged as much as possible

- Dashboard layout and CSS
- HTML templates
- Existing Flask route names
- CSV and ML files

## Main changes

- Firebase Realtime Database is now the live database for latest data, history, controls, activity logs, and users.
- SQLite is still available as fallback/local testing.
- ESP32 code is included and configured as a PlatformIO project.


## Final redo notes

- Firebase Realtime Database is the live database.
- Email verification codes are stored under `/email_verifications` and removed after verification.
- Manual ON/OFF controls are disabled in the UI and ignored in the backend when global Auto mode is active.
- Green LED represents the lighting actuator prototype on GPIO 26.
- Red LED represents the fan/ventilation actuator prototype on GPIO 27.
- `smart_classroom_dataset.csv` now contains the original records plus 5000 converted synthetic rows for ML/demo history.
