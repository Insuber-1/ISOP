"""Small NumPy LSTM forecaster for chronological CPU/RAM/disk metrics.

This is a real next-step sequence regressor. It reports validation loss/MAE and
directional accuracy; these metrics are not anomaly-classification accuracy.
"""
from __future__ import annotations

import os
import tempfile
import json
from datetime import datetime
import numpy as np


class ResourceLSTM:
    FEATURES = ("cpu", "ram", "disk")
    SEQUENCE_LENGTH = 12
    HIDDEN_SIZE = 16
    EPOCHS = 16
    BATCH_SIZE = 64

    def __init__(self):
        self.wx = self.wh = self.bias = self.wy = self.by = None
        self.metadata = {"trained": False, "samples": 0, "epochs": 0,
                         "train_loss": None, "validation_loss": None,
                         "validation_mae": None, "directional_accuracy": None}

    @property
    def trained(self):
        return self.wx is not None

    def _forward(self, batch, training=False):
        batch_size = batch.shape[0]
        hidden_size = self.HIDDEN_SIZE
        h = np.zeros((batch_size, hidden_size), dtype=np.float64)
        c = np.zeros_like(h)
        cache = []
        for t in range(batch.shape[1]):
            x = batch[:, t, :]
            h_prev, c_prev = h, c
            z = x @ self.wx + h @ self.wh + self.bias
            zi, zf, zo, zg = np.split(z, 4, axis=1)
            i = 1.0 / (1.0 + np.exp(-np.clip(zi, -40, 40)))
            f = 1.0 / (1.0 + np.exp(-np.clip(zf, -40, 40)))
            o = 1.0 / (1.0 + np.exp(-np.clip(zo, -40, 40)))
            g = np.tanh(zg)
            c = f * c_prev + i * g
            tanh_c = np.tanh(c)
            h = o * tanh_c
            if training:
                cache.append((x, h_prev, c_prev, i, f, o, g, tanh_c))
        prediction = h @ self.wy + self.by
        return prediction, (cache, h) if training else None

    def _initialize(self, seed=20260930):
        rng = np.random.default_rng(seed)
        input_size, hidden_size = len(self.FEATURES), self.HIDDEN_SIZE
        self.wx = rng.normal(0, np.sqrt(2 / (input_size + 4 * hidden_size)),
                            (input_size, 4 * hidden_size))
        self.wh = rng.normal(0, np.sqrt(1 / hidden_size), (hidden_size, 4 * hidden_size))
        self.bias = np.zeros(4 * hidden_size, dtype=np.float64)
        # A mild positive forget-gate bias helps retain short-term context.
        self.bias[hidden_size:2 * hidden_size] = 1.0
        self.wy = rng.normal(0, np.sqrt(2 / (hidden_size + input_size)),
                            (hidden_size, input_size))
        self.by = np.zeros(input_size, dtype=np.float64)

    def _gradients(self, batch_x, batch_y):
        prediction, (cache, h_last) = self._forward(batch_x, training=True)
        batch_size = batch_x.shape[0]
        error = prediction - batch_y
        loss = float(np.mean(error * error))
        dy = (2.0 / (batch_size * len(self.FEATURES))) * error
        grads = {"wx": np.zeros_like(self.wx), "wh": np.zeros_like(self.wh),
                 "bias": np.zeros_like(self.bias), "wy": h_last.T @ dy,
                 "by": dy.sum(axis=0)}
        dh = dy @ self.wy.T
        dc = np.zeros_like(dh)
        for x, h_prev, c_prev, i, f, o, g, tanh_c in reversed(cache):
            do = dh * tanh_c
            dc_total = dh * o * (1.0 - tanh_c * tanh_c) + dc
            df = dc_total * c_prev
            dc = dc_total * f
            di = dc_total * g
            dg = dc_total * i
            dz = np.concatenate((di * i * (1.0 - i),
                                 df * f * (1.0 - f),
                                 do * o * (1.0 - o),
                                 dg * (1.0 - g * g)), axis=1)
            grads["wx"] += x.T @ dz
            grads["wh"] += h_prev.T @ dz
            grads["bias"] += dz.sum(axis=0)
            dh = dz @ self.wh.T
        # dy is already normalized by batch size and feature count above.
        return loss, grads

    @staticmethod
    def _mae(prediction, actual):
        return float(np.mean(np.abs(prediction - actual)) * 100.0)

    def fit(self, rows, progress_callback=None):
        values, timestamps = [], []
        for row in rows or []:
            try:
                item = [float(row[key]) for key in self.FEATURES]
            except (KeyError, TypeError, ValueError):
                continue
            if not np.isfinite(item).all():
                continue
            values.append(np.clip(item, 0.0, 100.0))
            stamp = row.get("timestamp")
            try:
                timestamps.append(float(stamp.timestamp()) if hasattr(stamp, "timestamp")
                                  else datetime.fromisoformat(str(stamp)).timestamp())
            except (TypeError, ValueError, OSError):
                timestamps.append(None)
        values = np.asarray(values, dtype=np.float64) / 100.0
        if len(values) < self.SEQUENCE_LENGTH + 24:
            return {"trained": False, "samples": int(len(values)),
                    "reason": "ต้องมีข้อมูลต่อเนื่องอย่างน้อย 36 แถว"}

        split = max(self.SEQUENCE_LENGTH + 1, int(len(values) * 0.8))
        windows, targets, target_indices = [], [], []
        for target_idx in range(self.SEQUENCE_LENGTH, len(values)):
            windows.append(values[target_idx - self.SEQUENCE_LENGTH:target_idx])
            targets.append(values[target_idx])
            target_indices.append(target_idx)
        windows = np.asarray(windows, dtype=np.float64)
        targets = np.asarray(targets, dtype=np.float64)
        target_indices = np.asarray(target_indices)
        train_mask = target_indices < split
        validation_mask = ~train_mask
        x_train, y_train = windows[train_mask], targets[train_mask]
        x_val, y_val = windows[validation_mask], targets[validation_mask]
        if len(x_train) < 16 or len(x_val) < 4:
            return {"trained": False, "samples": int(len(values)),
                    "reason": "ข้อมูลไม่พอแบ่งชุดฝึกและชุดตรวจสอบตามเวลา"}

        self._initialize()
        params = {"wx": self.wx, "wh": self.wh, "bias": self.bias,
                  "wy": self.wy, "by": self.by}
        first_m = {key: np.zeros_like(value) for key, value in params.items()}
        second_m = {key: np.zeros_like(value) for key, value in params.items()}
        step, beta1, beta2, learning_rate, epsilon = 0, 0.9, 0.999, 0.008, 1e-8
        rng = np.random.default_rng(2048)
        train_loss = 0.0
        validation_loss = validation_mae = directional_accuracy = 0.0
        for epoch in range(self.EPOCHS):
            order = rng.permutation(len(x_train))
            losses = []
            for start in range(0, len(order), self.BATCH_SIZE):
                batch_ids = order[start:start + self.BATCH_SIZE]
                loss, gradients = self._gradients(x_train[batch_ids], y_train[batch_ids])
                norm = np.sqrt(sum(float(np.sum(grad * grad)) for grad in gradients.values()))
                if norm > 1.0:
                    gradients = {key: grad / norm for key, grad in gradients.items()}
                step += 1
                for key in params:
                    first_m[key] = beta1 * first_m[key] + (1 - beta1) * gradients[key]
                    second_m[key] = beta2 * second_m[key] + (1 - beta2) * gradients[key] ** 2
                    m_hat = first_m[key] / (1 - beta1 ** step)
                    v_hat = second_m[key] / (1 - beta2 ** step)
                    params[key] -= learning_rate * m_hat / (np.sqrt(v_hat) + epsilon)
                losses.append(loss)
            train_loss = float(np.mean(losses)) if losses else 0.0
            val_prediction, _ = self._forward(x_val)
            validation_loss = float(np.mean((val_prediction - y_val) ** 2))
            validation_mae = self._mae(val_prediction, y_val)
            prior = x_val[:, -1, :]
            actual_delta, predicted_delta = y_val - prior, val_prediction - prior
            directional_accuracy = float(np.mean(
                np.sign(np.where(np.abs(actual_delta) < 0.01, 0, actual_delta)) ==
                np.sign(np.where(np.abs(predicted_delta) < 0.01, 0, predicted_delta))) * 100.0)
            if progress_callback:
                progress_callback({"epoch": epoch + 1, "epochs": self.EPOCHS,
                                   "train_loss": round(train_loss, 6),
                                   "validation_loss": round(validation_loss, 6),
                                   "validation_mae": round(validation_mae, 3),
                                   "directional_accuracy": round(directional_accuracy, 2)})
        valid_times = np.asarray([stamp for stamp in timestamps if stamp is not None], dtype=np.float64)
        time_deltas = np.diff(valid_times) if len(valid_times) >= 2 else np.asarray([])
        time_deltas = time_deltas[time_deltas > 0]
        sample_interval_seconds = (int(np.median(time_deltas)) if len(time_deltas)
                                   else 15)
        training_days = (round(float(valid_times[-1] - valid_times[0]) / 86400.0, 2)
                         if len(valid_times) >= 2 else 0.0)
        self.metadata = {
            "trained": True, "samples": int(len(values)), "train_samples": int(len(x_train)),
            "validation_samples": int(len(x_val)), "epochs": self.EPOCHS,
            "train_loss": round(train_loss, 6), "validation_loss": round(validation_loss, 6),
            "validation_mae": round(validation_mae, 3),
            "directional_accuracy": round(directional_accuracy, 2),
            "sample_interval_seconds": sample_interval_seconds,
            "training_days": training_days,
            "sequence_length": self.SEQUENCE_LENGTH, "features": list(self.FEATURES),
            "metric_note": "Validation MAE is percentage points; directional accuracy is trend direction, not anomaly accuracy.",
        }
        return dict(self.metadata)

    def predict(self, history, steps=3):
        if not self.trained:
            return None
        try:
            values = np.asarray(history, dtype=np.float64)
            if values.ndim != 2 or values.shape[1] != len(self.FEATURES):
                return None
            values = values[-self.SEQUENCE_LENGTH:]
            if len(values) < self.SEQUENCE_LENGTH or not np.isfinite(values).all():
                return None
            context = np.clip(values, 0.0, 100.0) / 100.0
            predictions = []
            for _ in range(max(1, int(steps))):
                normalized, _ = self._forward(context[None, :, :])
                next_value = np.clip(normalized[0], 0.0, 1.0)
                predictions.append(next_value * 100.0)
                context = np.vstack((context[1:], next_value))
            return np.asarray(predictions)
        except (TypeError, ValueError, FloatingPointError):
            return None

    def save(self, path):
        if not self.trained:
            return False
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".lstm-", suffix=".npz", dir=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                np.savez_compressed(stream, wx=self.wx, wh=self.wh, bias=self.bias,
                                    wy=self.wy, by=self.by,
                                    metadata=np.asarray([json.dumps(self.metadata)]))
            os.replace(temporary, path)
            return True
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def load(self, path):
        try:
            with np.load(path, allow_pickle=False) as model:
                wx, wh, bias = model["wx"], model["wh"], model["bias"]
                wy, by = model["wy"], model["by"]
                metadata = json.loads(str(model["metadata"][0]))
            expected = ((3, 4 * self.HIDDEN_SIZE), (self.HIDDEN_SIZE, 4 * self.HIDDEN_SIZE),
                        (4 * self.HIDDEN_SIZE,), (self.HIDDEN_SIZE, 3), (3,))
            if tuple(arr.shape for arr in (wx, wh, bias, wy, by)) != expected:
                return False
            self.wx, self.wh, self.bias, self.wy, self.by = wx, wh, bias, wy, by
            self.metadata = metadata
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False
