"""
SMART CLASSROOM — IMPROVED TEMPERATURE MODEL v2
================================================
Fixes vs v1:
  • Session-aware lag / rolling / EMA features
    (gaps > 60 s reset the lag window — no cross-session leakage)
  • Per-session future-target matching
  • Tries GradientBoosting AND RandomForest, keeps the best
  • Full diagnostic plots saved to improved_model_analysis.png

Key new features:
  • temp_lag 1 / 2 / 3 / 5 / 10 (within-session)
  • rolling mean / std / min / max over 5 / 10 / 20 rows (within-session)
  • rate-of-change delta 1 / 5
  • EMA 5 / 10 / 20
  • hour encoded with sin/cos (cyclical)
  • humidity lag 1 / 5 / 10
"""

import os
import pickle
import warnings
from datetime import datetime

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

# ── Config ────────────────────────────────────────────────────────────────────
RAW_DATASET            = "smart_classroom_dataset.csv"
MODEL_FILE             = "smart_classroom_model.pkl"
SCALER_FILE            = "scaler.pkl"
INFO_FILE              = "model_info.pkl"
PLOT_FILE              = "improved_model_analysis.png"
PREDICTION_HORIZON_MIN = 10
MIN_ROWS_REQUIRED      = 50
SESSION_GAP_SECONDS    = 60      # gap > 60 s → new session
LAG_ROWS               = [1, 2, 3, 5, 10]
ROLLING_WINDOWS        = [5, 10, 20]
# ─────────────────────────────────────────────────────────────────────────────

print("=" * 65)
print("  SMART CLASSROOM — IMPROVED MODEL TRAINING v2")
print("=" * 65)


# ── 1. Load & basic clean ─────────────────────────────────────────────────────
print("\n[1] Loading dataset...")

df = pd.read_csv(RAW_DATASET, on_bad_lines="skip")
print(f"    Raw rows: {len(df)}")

df["server_timestamp"] = pd.to_datetime(df["server_timestamp"], errors="coerce")
for col in ["temperature", "humidity", "pressure", "pir", "ldr", "taux_occupation"]:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

df = df.dropna(subset=["server_timestamp", "temperature"])
df = df.sort_values("server_timestamp").drop_duplicates(subset=["server_timestamp"])
df = df[(df["temperature"] >= 0) & (df["temperature"] <= 100)]
df = df.reset_index(drop=True)

# Fill optional columns with 0 if missing
for col in ["humidity", "pressure", "pir", "ldr", "taux_occupation"]:
    if col not in df.columns:
        df[col] = 0
    else:
        df[col] = df[col].fillna(df[col].median())

print(f"    Clean rows: {len(df)}")
print(f"    Temp  min={df['temperature'].min():.2f}  max={df['temperature'].max():.2f}  "
      f"mean={df['temperature'].mean():.2f} °C")


# ── 2. Assign session IDs based on time gaps ──────────────────────────────────
print(f"\n[2] Detecting sessions (gap > {SESSION_GAP_SECONDS} s = new session)...")
df["dt_seconds"] = df["server_timestamp"].diff().dt.total_seconds().fillna(0)
df["session_id"] = (df["dt_seconds"] > SESSION_GAP_SECONDS).cumsum()
n_sessions = df["session_id"].nunique()
print(f"    Sessions found: {n_sessions}")
for sid, grp in df.groupby("session_id"):
    print(f"      Session {sid}: {len(grp)} rows  |  "
          f"{grp['server_timestamp'].min().strftime('%Y-%m-%d %H:%M')} – "
          f"{grp['server_timestamp'].max().strftime('%H:%M')}")


# ── 3. Within-session feature engineering ────────────────────────────────────
print("\n[3] Feature engineering (within-session)...")

parts = []
for sid, grp in df.groupby("session_id"):
    g = grp.copy().reset_index(drop=True)

    # Time features
    hour_frac = g["server_timestamp"].dt.hour + g["server_timestamp"].dt.minute / 60.0
    g["hour_sin"]    = np.sin(2 * np.pi * hour_frac / 24)
    g["hour_cos"]    = np.cos(2 * np.pi * hour_frac / 24)
    g["minute"]      = g["server_timestamp"].dt.minute
    g["day_of_week"] = g["server_timestamp"].dt.dayofweek

    # Lag features (only temperature & humidity within session)
    for lag in LAG_ROWS:
        g[f"temp_lag_{lag}"]  = g["temperature"].shift(lag)
        g[f"humid_lag_{lag}"] = g["humidity"].shift(lag)

    # Rolling features
    for w in ROLLING_WINDOWS:
        g[f"temp_roll_mean_{w}"] = g["temperature"].rolling(w, min_periods=1).mean()
        g[f"temp_roll_std_{w}"]  = g["temperature"].rolling(w, min_periods=1).std().fillna(0)
        g[f"temp_roll_min_{w}"]  = g["temperature"].rolling(w, min_periods=1).min()
        g[f"temp_roll_max_{w}"]  = g["temperature"].rolling(w, min_periods=1).max()

    # Rate of change
    g["temp_delta_1"] = g["temperature"].diff(1).fillna(0)
    g["temp_delta_5"] = g["temperature"].diff(5).fillna(0)

    # EMA
    g["temp_ema_5"]  = g["temperature"].ewm(span=5,  adjust=False).mean()
    g["temp_ema_10"] = g["temperature"].ewm(span=10, adjust=False).mean()
    g["temp_ema_20"] = g["temperature"].ewm(span=20, adjust=False).mean()

    # Polynomial / interaction
    g["temp_squared"]  = g["temperature"] ** 2
    g["temp_humidity"] = g["temperature"] * g["humidity"] / 100.0

    # Future target: match within this session only
    g["target_time"] = g["server_timestamp"] + pd.Timedelta(minutes=PREDICTION_HORIZON_MIN)
    future_ref = g[["server_timestamp", "temperature"]].rename(
        columns={"server_timestamp": "future_ts", "temperature": "temp_future"}
    )
    g = pd.merge_asof(
        g.sort_values("target_time"),
        future_ref.sort_values("future_ts"),
        left_on="target_time",
        right_on="future_ts",
        direction="forward",
        tolerance=pd.Timedelta(minutes=3),
    )
    g = g.sort_values("server_timestamp").reset_index(drop=True)

    parts.append(g)

df_feat = pd.concat(parts, ignore_index=True)
df_feat = df_feat.dropna().reset_index(drop=True)
print(f"    Rows after feature engineering: {len(df_feat)}")

if len(df_feat) < MIN_ROWS_REQUIRED:
    raise ValueError(f"Not enough rows ({len(df_feat)}) to train. Need ≥ {MIN_ROWS_REQUIRED}.")


# ── 4. Define X / y ───────────────────────────────────────────────────────────
feature_columns = (
    ["temperature", "humidity", "pressure", "pir", "ldr", "taux_occupation"]
    + ["hour_sin", "hour_cos", "minute", "day_of_week"]
    + [f"temp_lag_{l}"  for l in LAG_ROWS]
    + [f"humid_lag_{l}" for l in LAG_ROWS]
    + [f"temp_roll_mean_{w}" for w in ROLLING_WINDOWS]
    + [f"temp_roll_std_{w}"  for w in ROLLING_WINDOWS]
    + [f"temp_roll_min_{w}"  for w in ROLLING_WINDOWS]
    + [f"temp_roll_max_{w}"  for w in ROLLING_WINDOWS]
    + ["temp_delta_1", "temp_delta_5"]
    + ["temp_ema_5", "temp_ema_10", "temp_ema_20"]
    + ["temp_squared", "temp_humidity"]
)
feature_columns = [c for c in feature_columns if c in df_feat.columns]
print(f"    Features used: {len(feature_columns)}")

X = df_feat[feature_columns].copy()
y = df_feat["temp_future"].copy()


# ── 5. Chronological split ────────────────────────────────────────────────────
print("\n[4] Chronological 80/20 split...")
split = int(0.8 * len(df_feat))
X_train, X_test = X.iloc[:split], X.iloc[split:]
y_train, y_test = y.iloc[:split], y.iloc[split:]
print(f"    Train: {len(X_train)}   Test: {len(X_test)}")


# ── 6. Scale ──────────────────────────────────────────────────────────────────
scaler = StandardScaler()
X_train_sc = scaler.fit_transform(X_train)
X_test_sc  = scaler.transform(X_test)


# ── 7. Train & compare models ─────────────────────────────────────────────────
print("\n[5] Training models...")

models_cfg = {
    "GradientBoosting": GradientBoostingRegressor(
        n_estimators=500, learning_rate=0.05, max_depth=5,
        subsample=0.85, min_samples_leaf=3, random_state=42,
    ),
    "RandomForest": RandomForestRegressor(
        n_estimators=400, max_depth=12, min_samples_leaf=2,
        random_state=42, n_jobs=-1,
    ),
}

results = {}
for name, m in models_cfg.items():
    print(f"    {name}...", end=" ", flush=True)
    m.fit(X_train_sc, y_train)
    pred = m.predict(X_test_sc)
    mae  = mean_absolute_error(y_test, pred)
    rmse = np.sqrt(mean_squared_error(y_test, pred))
    r2   = r2_score(y_test, pred)
    results[name] = {"model": m, "pred": pred, "mae": mae, "rmse": rmse, "r2": r2}
    print(f"MAE={mae:.4f}  RMSE={rmse:.4f}  R²={r2:.4f}")

best_name = min(results, key=lambda n: results[n]["mae"])
best      = results[best_name]
model     = best["model"]
y_pred    = best["pred"]
mae, rmse, r2 = best["mae"], best["rmse"], best["r2"]

print(f"\n    ✅  Best: {best_name}  |  MAE={mae:.4f}  RMSE={rmse:.4f}  R²={r2:.4f}")


# ── 8. Feature importance ─────────────────────────────────────────────────────
print("\n[6] Feature importance (top 15):")
importances = model.feature_importances_
imp_pairs = sorted(zip(feature_columns, importances), key=lambda x: x[1], reverse=True)
for feat, score in imp_pairs[:15]:
    bar = "█" * int(score * 300)
    print(f"    {feat:28s} {score:.4f}  {bar}")


# ── 9. Plots ──────────────────────────────────────────────────────────────────
print("\n[7] Generating plots...")
fig, axes = plt.subplots(2, 2, figsize=(14, 10))
fig.suptitle(
    f"Smart Classroom — Improved Temperature Model ({best_name})\n"
    f"MAE={mae:.4f}°C  RMSE={rmse:.4f}°C  R²={r2:.4f}  |  Horizon={PREDICTION_HORIZON_MIN}min",
    fontsize=12, fontweight="bold"
)

ax = axes[0, 0]
ax.scatter(y_test, y_pred, alpha=0.5, s=12, color="#3b6ff0", edgecolors="none")
lo, hi = min(float(y_test.min()), float(y_pred.min())), max(float(y_test.max()), float(y_pred.max()))
ax.plot([lo, hi], [lo, hi], "r--", lw=2, label="Perfect")
ax.set(xlabel="Actual Temp (°C)", ylabel="Predicted Temp (°C)", title="Actual vs Predicted")
ax.legend(); ax.grid(True, alpha=0.3)

ax = axes[0, 1]
residuals = np.array(y_test) - y_pred
ax.scatter(y_pred, residuals, alpha=0.5, s=12, color="#20c997", edgecolors="none")
ax.axhline(0, color="r", lw=2, linestyle="--")
ax.set(xlabel="Predicted Temp (°C)", ylabel="Residual (°C)", title="Residual Plot")
ax.grid(True, alpha=0.3)

ax = axes[1, 0]
n = min(300, len(y_test))
ax.plot(range(n), list(y_test)[-n:], color="#3b6ff0", lw=1.5, label="Actual")
ax.plot(range(n), y_pred[-n:], color="#f97316", lw=1.5, ls="--", label=f"+{PREDICTION_HORIZON_MIN}min pred")
ax.set(xlabel="Sample", ylabel="Temp (°C)", title=f"Time Series (last {n} test rows)")
ax.legend(); ax.grid(True, alpha=0.3)

ax = axes[1, 1]
top15 = imp_pairs[:15]
names_r  = [p[0] for p in top15[::-1]]
scores_r = [p[1] for p in top15[::-1]]
colors = plt.cm.Blues(np.linspace(0.4, 0.9, len(names_r)))
bars = ax.barh(names_r, scores_r, color=colors)
ax.set(xlabel="Importance", title="Top 15 Feature Importances")
ax.grid(True, axis="x", alpha=0.3)
for bar, val in zip(bars, scores_r):
    ax.text(val + 0.001, bar.get_y() + bar.get_height() / 2,
            f"{val:.3f}", va="center", fontsize=8)

plt.tight_layout()
plt.savefig(PLOT_FILE, dpi=150, bbox_inches="tight")
print(f"    Saved → {PLOT_FILE}")


# ── 10. Save ──────────────────────────────────────────────────────────────────
print("\n[8] Saving model...")
joblib.dump(model,  MODEL_FILE)
joblib.dump(scaler, SCALER_FILE)

model_info = {
    "model_name":                  best_name,
    "features":                    feature_columns,
    "mae":                         float(mae),
    "rmse":                        float(rmse),
    "r2":                          float(r2),
    "prediction_horizon_minutes":  PREDICTION_HORIZON_MIN,
    "training_date":               datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "n_rows_trained":              int(len(df_feat)),
    "session_gap_seconds":         SESSION_GAP_SECONDS,
    "feature_importance":          {n: float(s) for n, s in imp_pairs},
    "lag_rows":                    LAG_ROWS,
    "rolling_windows":             ROLLING_WINDOWS,
}
with open(INFO_FILE, "wb") as f:
    pickle.dump(model_info, f)

print(f"    {MODEL_FILE}  ✓")
print(f"    {SCALER_FILE}  ✓")
print(f"    {INFO_FILE}   ✓")

print("\n" + "=" * 65)
print(f"  DONE  —  {best_name}")
print(f"  MAE  = {mae:.4f} °C")
print(f"  RMSE = {rmse:.4f} °C")
print(f"  R²   = {r2:.4f}")
print("=" * 65)
