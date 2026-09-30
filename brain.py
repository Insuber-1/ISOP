"""Resource monitor combining explicit thresholds and unsupervised detection.

The Isolation Forest is trained from recent persisted metrics when available;
new installs continue with EMA, adaptive median/MAD thresholds, and hysteresis
until enough data has accumulated. Forecast values are estimates, not guarantees.
"""


import math
import random
import os
import time
from statistics import median
from lstm_model import ResourceLSTM
from paths import app_path

class AIServerBrain:
    EMA_ALPHA      = 0.3     # น้ำหนักค่าใหม่ใน EMA (0-1) — น้อย = เรียบ/เสถียรกว่า, มาก = ไวกว่า
    ANOMALY_ENTER  = 90.0    # เกณฑ์ fallback ก่อน baseline มีข้อมูลเพียงพอ และเพดานเกณฑ์ปรับตัว
    ANOMALY_EXIT   = 80.0    # ส่วนต่าง hysteresis จากเกณฑ์เข้า
    BASELINE_MIN_SAMPLES = 30
    BASELINE_WINDOW = 360   # 12 นาทีเมื่อประเมินทุก 2 วินาที
    ADAPTIVE_MIN = 70.0
    ADAPTIVE_MAX = 90.0
    FOREST_TREES = 48
    FOREST_SAMPLE_SIZE = 128
    FOREST_CONTAMINATION = 0.02

    def __init__(self):
        # พร้อมใช้งานทันที ไม่ต้องเทรน
        self.is_trained = True
        self._ema = {"cpu": None, "ram": None, "disk": None}
        self._history_cpu:  list[float] = []   # เก็บค่า EMA ล่าสุด 20 ตัว (สำหรับ trend)
        self._history_ram:  list[float] = []
        self._history_disk: list[float] = []
        self._baseline = {"cpu": [], "ram": [], "disk": []}
        self._thresholds = {"cpu": self.ANOMALY_ENTER, "ram": self.ANOMALY_ENTER,
                            "disk": self.ANOMALY_ENTER}
        self._anomaly_active = False
        self._anomaly_keys: set[str] = set()
        self.last_score: float = 0.0           # ค่า EMA สูงสุด (cpu/ram/disk) ล่าสุด ใช้ทำ risk score
        # Lightweight Isolation Forest implemented with the Python standard
        # library. It complements explicit resource thresholds; it does not
        # claim a measured accuracy until labeled incidents are available.
        self._forest: list[tuple] = []
        self._forest_threshold: float | None = None
        self._forest_features: tuple[str, ...] = ()
        self._forest_training_rows = 0
        self._forest_training_days = 0
        self._forest_sample_size = self.FOREST_SAMPLE_SIZE
        self.last_anomaly_score: float | None = None
        self._last_forest_anomaly = False
        self._lstm = ResourceLSTM()
        self._lstm_model_path = app_path("lstm_resource_model.npz")
        self._lstm_status = dict(self._lstm.metadata)
        self._lstm_last_sample_epoch = 0.0
        if self._lstm.load(self._lstm_model_path):
            self._lstm_status = dict(self._lstm.metadata)
            self._lstm_status["loaded_from_disk"] = True
        self._lstm_history: list[tuple[float, float, float]] = []

    def train_lstm_from_history(self, rows: list[dict], progress_callback=None) -> dict:
        """Train a next-sample LSTM from chronological CPU/RAM/disk history."""
        candidate = ResourceLSTM()
        result = candidate.fit(rows, progress_callback=progress_callback)
        if result.get("trained"):
            try:
                candidate.save(self._lstm_model_path)
                result["saved"] = True
            except Exception as exc:
                result["saved"] = False
                result["save_error"] = str(exc)
            self._lstm = candidate
            self._lstm_status = dict(result)
            self.seed_lstm_history(rows)
        elif self._lstm.trained:
            self._lstm_status = dict(self._lstm.metadata)
            self._lstm_status["last_training_attempt"] = result
        else:
            self._lstm_status = dict(result)
        return dict(self._lstm_status)

    def seed_lstm_history(self, rows: list[dict]):
        history = []
        for row in (rows or [])[-self._lstm.SEQUENCE_LENGTH:]:
            try:
                values = tuple(float(row[key]) for key in self._lstm.FEATURES)
            except (TypeError, ValueError, KeyError):
                continue
            if all(math.isfinite(value) for value in values):
                history.append(values)
        if history:
            self._lstm_history = history[-self._lstm.SEQUENCE_LENGTH:]
            for row in reversed((rows or [])[-self._lstm.SEQUENCE_LENGTH:]):
                stamp = row.get("timestamp") if isinstance(row, dict) else None
                try:
                    self._lstm_last_sample_epoch = float(stamp.timestamp())
                    break
                except (AttributeError, TypeError, ValueError, OSError):
                    continue

    def add_lstm_sample(self, metrics: dict):
        try:
            sample = tuple(float(metrics[key]) for key in self._lstm.FEATURES)
        except (TypeError, ValueError, KeyError):
            return
        if all(math.isfinite(value) for value in sample):
            now = time.time()
            interval = max(15, int(self._lstm_status.get("sample_interval_seconds", 15) or 15))
            if self._lstm_last_sample_epoch and now - self._lstm_last_sample_epoch < interval:
                return
            self._lstm_history.append(sample)
            self._lstm_history = self._lstm_history[-self._lstm.SEQUENCE_LENGTH:]
            self._lstm_last_sample_epoch = now

    def forecast_lstm(self, steps: int = 3) -> dict | None:
        """Return autoregressive LSTM estimates from saved 15-second samples."""
        if not self._lstm.trained or len(self._lstm_history) < self._lstm.SEQUENCE_LENGTH:
            return None
        predictions = self._lstm.predict(self._lstm_history, steps=steps)
        if predictions is None:
            return None
        return {key: [round(float(value), 1) for value in
                      [self._lstm_history[-1][idx], *predictions[:, idx].tolist()]]
                for idx, key in enumerate(self._lstm.FEATURES)}

    def get_lstm_status(self) -> dict:
        return dict(self._lstm_status)

    @staticmethod
    def _path_length_correction(size: int) -> float:
        if size <= 1:
            return 0.0
        if size == 2:
            return 1.0
        # Harmonic-number approximation used by the Isolation Forest score.
        return 2.0 * (math.log(size - 1) + 0.5772156649) - 2.0 * (size - 1) / size

    @classmethod
    def _build_tree(cls, rows: list[tuple[float, ...]], depth: int,
                    max_depth: int, rng: random.Random) -> tuple:
        size = len(rows)
        if size <= 1 or depth >= max_depth:
            return (None, None, None, None, size)
        width = len(rows[0])
        candidates = [i for i in range(width)
                      if min(row[i] for row in rows) < max(row[i] for row in rows)]
        if not candidates:
            return (None, None, None, None, size)
        feature = rng.choice(candidates)
        low = min(row[feature] for row in rows)
        high = max(row[feature] for row in rows)
        split = rng.uniform(low, high)
        left_rows = [row for row in rows if row[feature] < split]
        right_rows = [row for row in rows if row[feature] >= split]
        if not left_rows or not right_rows:
            return (None, None, None, None, size)
        return (feature, split,
                cls._build_tree(left_rows, depth + 1, max_depth, rng),
                cls._build_tree(right_rows, depth + 1, max_depth, rng), size)

    @classmethod
    def _tree_path_length(cls, tree: tuple, row: tuple[float, ...], depth: int = 0) -> float:
        feature, split, left, right, size = tree
        if feature is None:
            return depth + cls._path_length_correction(size)
        return cls._tree_path_length(left if row[feature] < split else right, row, depth + 1)

    @staticmethod
    def _row_features(row: dict, features: tuple[str, ...]) -> tuple[float, ...] | None:
        values = []
        for feature in features:
            try:
                value = float(row.get(feature, 0) or 0)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value):
                return None
            values.append(max(0.0, value))
        return tuple(values)

    def _isolation_score(self, row: tuple[float, ...]) -> float:
        if not self._forest:
            return 0.0
        average_path = sum(self._tree_path_length(tree, row) for tree in self._forest) / len(self._forest)
        normalizer = self._path_length_correction(self._forest_sample_size)
        return 2.0 ** (-average_path / normalizer) if normalizer > 0 else 0.0

    def train_from_history(self, rows: list[dict], *, days: int = 30) -> dict:
        """Fit an unsupervised anomaly baseline from persisted recent metrics."""
        core_features = ("cpu", "ram", "disk")
        optional_features = ("swap_percent", "disk_read_mb_s", "disk_write_mb_s",
                             "network_recv_mb_s", "network_sent_mb_s", "process_count")
        available = [row for row in (rows or []) if isinstance(row, dict)]
        feature_candidates = core_features + tuple(
            feature for feature in optional_features
            if available and sum(row.get(feature) not in (None, "") for row in available) /
            len(available) >= 0.8
        )
        usable = []
        for source in available:
            row = self._row_features(source, feature_candidates)
            if row is not None:
                usable.append(row)
        if len(usable) < 64:
            self._forest = []
            self._forest_threshold = None
            self._forest_features = ()
            self._forest_training_rows = len(usable)
            self._forest_training_days = max(1, int(days))
            return {"trained": False, "samples": len(usable), "reason": "warmup"}

        sample_size = min(self.FOREST_SAMPLE_SIZE, len(usable))
        self._forest_sample_size = sample_size
        features = tuple(feature_candidates)
        rng = random.Random(202603)
        forest = []
        max_depth = math.ceil(math.log2(sample_size))
        for _ in range(self.FOREST_TREES):
            sample = rng.sample(usable, sample_size) if len(usable) > sample_size else list(usable)
            forest.append(self._build_tree(sample, 0, max_depth, rng))
        self._forest = forest
        self._forest_features = features
        scores = sorted(self._isolation_score(row) for row in usable)
        threshold_index = min(len(scores) - 1, max(0, math.ceil((1.0 - self.FOREST_CONTAMINATION) * len(scores)) - 1))
        self._forest_threshold = scores[threshold_index]
        self._forest_training_rows = len(usable)
        self._forest_training_days = max(1, int(days))
        return {"trained": True, "samples": len(usable), "days": self._forest_training_days,
                "threshold": round(self._forest_threshold, 4)}

    def get_model_status(self) -> dict:
        return {"trained": bool(self._forest), "samples": self._forest_training_rows,
                "days": self._forest_training_days, "trees": len(self._forest),
                "method": "Isolation Forest" if self._forest else "warmup"}

    # ------------------------------------------------------------------
    def _update_ema(self, key: str, value: float) -> float:
        prev = self._ema[key]
        smoothed = value if prev is None else (self.EMA_ALPHA * value + (1 - self.EMA_ALPHA) * prev)
        self._ema[key] = smoothed
        return smoothed

    def _adaptive_threshold(self, key: str) -> float:
        values = self._baseline[key]
        if len(values) < self.BASELINE_MIN_SAMPLES:
            return self.ANOMALY_ENTER
        center = median(values)
        mad = median([abs(value - center) for value in values])
        # 1.4826 ปรับ MAD ให้เทียบสเกลส่วนเบี่ยงเบนมาตรฐานในข้อมูลปกติ
        estimate = center + max(15.0, 3.0 * 1.4826 * mad)
        return round(max(self.ADAPTIVE_MIN, min(self.ADAPTIVE_MAX, estimate)), 1)

    # ------------------------------------------------------------------
    # ตรวจ anomaly แบบ deterministic (EMA + hysteresis)  คืน -1 (ผิดปกติ) หรือ 1 (ปกติ)
    # ------------------------------------------------------------------
    def check_anomaly(self, metrics: dict) -> int:
        cpu  = metrics.get('cpu',  0)
        ram  = metrics.get('ram',  0)
        disk = metrics.get('disk', 0)

        cpu_s  = self._update_ema('cpu',  cpu)
        ram_s  = self._update_ema('ram',  ram)
        disk_s = self._update_ema('disk', disk)

        for lst, val in [(self._history_cpu, cpu_s),
                         (self._history_ram, ram_s),
                         (self._history_disk, disk_s)]:
            lst.append(val)
            if len(lst) > 20:
                lst.pop(0)

        smoothed = {"cpu": cpu_s, "ram": ram_s, "disk": disk_s}
        self._thresholds = {key: self._adaptive_threshold(key) for key in smoothed}
        self.last_score = max(smoothed.values())
        above_keys = {key for key in smoothed if smoothed[key] > self._thresholds[key]}

        forest_row = self._row_features(metrics, self._forest_features) if self._forest else None
        self.last_anomaly_score = self._isolation_score(forest_row) if forest_row is not None else None
        self._last_forest_anomaly = bool(
            self.last_anomaly_score is not None and self._forest_threshold is not None
            and self.last_anomaly_score > self._forest_threshold
        )

        if self._last_forest_anomaly:
            self._anomaly_active = True
            self._anomaly_keys.update(above_keys or {"ml_anomaly"})
        elif self._anomaly_active:
            self._anomaly_keys.update(above_keys)
            self._anomaly_keys = {
                key for key in self._anomaly_keys if key in smoothed
                and smoothed[key] >= max(0.0, self._thresholds[key] -
                                         (self.ANOMALY_ENTER - self.ANOMALY_EXIT))
            }
            if not self._anomaly_keys:
                self._anomaly_active = False
        elif above_keys:
            self._anomaly_active = True
            self._anomaly_keys = set(above_keys)

        # อย่าเรียนรู้ตัวอย่างที่กำลังผิดปกติเป็น baseline ปกติ
        if not self._anomaly_active and not above_keys:
            for key, value in smoothed.items():
                self._baseline[key].append(value)
                if len(self._baseline[key]) > self.BASELINE_WINDOW:
                    del self._baseline[key][:-self.BASELINE_WINDOW]

        return -1 if self._anomaly_active else 1

    # ------------------------------------------------------------------
    # พยากรณ์แนวโน้ม CPU จาก moving average ของค่า EMA (เรียบกว่าค่าดิบ)
    # ------------------------------------------------------------------
    def predict_trend(self) -> str:
        if len(self._history_cpu) < 6:
            return "รอเก็บข้อมูล..."

        recent = self._history_cpu[-3:]
        older  = self._history_cpu[-6:-3]
        avg_recent = sum(recent) / len(recent)
        avg_older  = sum(older)  / len(older)
        diff = avg_recent - avg_older

        if diff > 5:
            return f"กำลังสูงขึ้น ↗️ (+{diff:.1f}%)"
        elif diff < -5:
            return f"กำลังลดลง ↘️ ({diff:.1f}%)"
        else:
            return f"คงที่ ➡️ ({avg_recent:.1f}%)"

    # ------------------------------------------------------------------
    def forecast_metrics(self, histories: dict, steps: int = 10, window: int = 12,
                         min_samples: int = 6) -> dict | None:
        """สร้างค่าคาดการณ์แบบเส้นตรงที่ถูกหน่วงและจำกัดขอบเขต (0–100).

        ใช้ regression กับหน้าต่างล่าสุด ลดความชันลงและหน่วงตามระยะ เพื่อไม่ให้
        การแกว่งสั้น ๆ ถูกตีความเป็นการพุ่งต่อเนื่องระยะยาว. คืน None จนกว่าจะมี
        ตัวอย่างเพียงพอ และผลลัพธ์เป็นค่าประมาณ ไม่ใช่ค่าที่รับประกัน.
        """
        if not isinstance(histories, dict) or steps < 1 or min_samples < 2:
            return None
        forecast = {}
        for key in ("cpu", "ram", "disk"):
            values = []
            for value in histories.get(key, []):
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number):
                    values.append(max(0.0, min(100.0, number)))
            if len(values) < min_samples:
                return None
            sample = values[-max(min_samples, window):]
            size = len(sample)
            x_mean = (size - 1) / 2.0
            y_mean = sum(sample) / size
            denominator = sum((i - x_mean) ** 2 for i in range(size))
            slope = (sum((i - x_mean) * (value - y_mean) for i, value in enumerate(sample)) / denominator
                     if denominator else 0.0)
            slope = max(-1.5, min(1.5, slope)) * 0.65
            projected = [round(values[-1], 1)]
            current = values[-1]
            for index in range(1, steps + 1):
                current = max(0.0, min(100.0, current + slope * (0.88 ** (index - 1))))
                projected.append(round(current, 1))
            forecast[key] = projected
        return forecast
    # risk score เป็น % (0-100) มาจากค่า EMA สูงสุดของ cpu/ram/disk โดยตรง
    # เสถียรกว่าเดิมเพราะไม่ขึ้นกับว่าโมเดลถูกเทรนมาดีแค่ไหน
    # ------------------------------------------------------------------
    def get_risk_score(self) -> float:
        # Keep this UI value interpretable as the highest smoothed resource
        # utilization. The unsupervised forest has its own score and threshold.
        return round(min(100.0, max(0.0, self.last_score)), 1)

    def get_thresholds(self) -> dict[str, float]:
        """เกณฑ์ที่ใช้ปัจจุบัน แยกตาม CPU/RAM/Disk เพื่อแสดงผลหรือบันทึกรายงาน"""
        return dict(self._thresholds)
