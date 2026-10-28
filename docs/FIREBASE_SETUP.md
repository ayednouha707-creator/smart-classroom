# Firebase Setup

## 1. Firebase Authentication

In Firebase Console:

1. Go to Authentication.
2. Enable Email/Password provider.
3. Use the Flask admin/users page to create/update/delete users.
4. The Flask backend mirrors user CRUD to Firebase Authentication when `firebase_key.json` and `FIREBASE_DB_URL` are configured.

## 2. Realtime Database

Enable Realtime Database and use these paths:

- `/users`
- `/latest_data`
- `/sensor_history`
- `/control_state`
- `/control_history`
- `/activity_log`
- `/alerts`
- `/predictions`
- `/esp32_status`

## 3. Service account

Download a service account JSON and save locally as:

```text
firebase_key.json
```

Then create `.env` from `.env.example` and set:

```env
FIREBASE_CREDENTIALS=firebase_key.json
FIREBASE_DB_URL=https://YOUR_PROJECT_ID-default-rtdb.firebaseio.com
```

## 4. Development rules

See:

```text
firebase/firebase_rules_example.json
```

These rules are for development/testing only. For production, restrict ESP32 and dashboard access more carefully.


## Final redo notes

- Firebase Realtime Database is the live database.
- Email verification codes are stored under `/email_verifications` and removed after verification.
- Manual ON/OFF controls are disabled in the UI and ignored in the backend when global Auto mode is active.
- Green LED represents the lighting actuator prototype on GPIO 26.
- Red LED represents the fan/ventilation actuator prototype on GPIO 27.
- `smart_classroom_dataset.csv` now contains the original records plus 5000 converted synthetic rows for ML/demo history.
