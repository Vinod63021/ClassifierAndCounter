# =========================================================
# SMART TRAFFIC MONITORING SYSTEM (SPEC v1.0 – FEB 2026)
# =========================================================

import cv2
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
from ultralytics import YOLO
from openpyxl import Workbook
from datetime import datetime
import time
import numpy as np
import math
from tkinter import ttk


# =========================================================
# CONFIGURATION (SPEC COMPLIANT)
# =========================================================
DETECTION_CONFIDENCE = 0.4
FRAME_SKIP = 2

CALIBRATION_FRAMES = 80
WAITING_PIXEL_THRESHOLD = 3
WRONG_DIRECTION_ANGLE = 120.0

DENSITY_RED_THRESHOLD = 10
WAIT_TIME_RED_THRESHOLD = 30.0

# -------- LINE COUNTING --------
LINE_Y = 270          # adjust if needed for your video
counted_ids = set()  # to avoid double counting
prev_y_positions = {}


# =========================================================
# YOLO + TRACKING
# =========================================================
model = YOLO("yolov8n.pt")

VEHICLE_CLASSES = [2, 3, 5, 7]
CLASS_NAMES = {
    2: "Car",
    3: "Motorcycle",
    5: "Bus",
    7: "Truck"
}

# =========================================================
# GLOBAL STATE
# =========================================================
cap = None
running = False
frame_count = 0

vehicle_log = {}
prev_positions = {}
speed_records = []

# Direction calibration
calibration_vectors = []
dominant_direction = None
calibrated = False

# Traffic signal
signal_state = "GREEN"
signal_log = []

# =========================================================
# MATH HELPERS
# =========================================================
def normalize(v):
    n = np.linalg.norm(v)
    return v / n if n > 0 else v

def angle_between(v1, v2):
    v1, v2 = normalize(v1), normalize(v2)
    dot = np.clip(np.dot(v1, v2), -1.0, 1.0)
    return math.degrees(math.acos(dot))

# =========================================================
# ANALYTICS ENGINE
# =========================================================
def compute_passed_vehicle_counts():
    counts = {"Car": 0, "Motorcycle": 0, "Bus": 0, "Truck": 0}

    for v in vehicle_log.values():
        if v["exit_time"] is not None and not v["is_wrong_direction"]:
            counts[v["vehicle_type"]] += 1

    return counts

def compute_live_counts():
    counts = {"Car": 0, "Motorcycle": 0, "Bus": 0, "Truck": 0}

    for v in vehicle_log.values():
        if v["exit_time"] is None and not v["is_wrong_direction"]:
            counts[v["vehicle_type"]] += 1

    total = sum(counts.values())
    return total, counts
def update_vehicle_table():
    vehicle_table.delete(*vehicle_table.get_children())

    for v in vehicle_log.values():
        entry = v["entry_time"].strftime("%H:%M:%S")
        exit_t = v["exit_time"].strftime("%H:%M:%S") if v["exit_time"] else "--"
        status = "OUT" if v["exit_time"] else "IN"

        vehicle_table.insert(
            "",
            "end",
            values=(
                v["track_id"],
                v["vehicle_type"],
                entry,
                exit_t,
                status
            )
        )

def compute_analytics():
    valid = [v for v in vehicle_log.values() if not v["is_wrong_direction"]]
    density = len(valid)

    avg_speed = round(np.mean(speed_records), 2) if speed_records else 0
    avg_wait = round(np.mean([v["waiting_time"] for v in valid]), 2) if valid else 0

    return density, avg_speed, avg_wait

def update_signal(density, avg_wait):
    global signal_state
    new_state = "GREEN"

    if density > DENSITY_RED_THRESHOLD or avg_wait > WAIT_TIME_RED_THRESHOLD:
        new_state = "RED"

    if new_state != signal_state:
        signal_log.append((datetime.now(), new_state))
        signal_state = new_state

# =========================================================
# VIDEO PROCESSING
# =========================================================
def start_video(path=None):
    global cap, running
    running = True
    cap = cv2.VideoCapture(path if path else 0)
    process_frame()

def stop_video():
    global running, cap
    running = False
    if cap:
        cap.release()
    cap = None

def process_frame():
    global frame_count, calibrated, dominant_direction

    if not running or cap is None:
        return

    ret, frame = cap.read()
    if not ret:
        stop_video()
        return

    frame = cv2.resize(frame, (960, 540))
    cv2.line(frame, (0, LINE_Y), (frame.shape[1], LINE_Y), (0, 0, 255), 2)

    frame_count += 1
    current_time = datetime.now()

    if frame_count % FRAME_SKIP == 0:
        results = model.track(
            frame,
            conf=DETECTION_CONFIDENCE,
            persist=True,
            tracker="bytetrack.yaml",
            verbose=False
        )

        if results and results[0].boxes.id is not None:
            boxes = results[0].boxes

            for box, cls, oid in zip(boxes.xyxy, boxes.cls, boxes.id):
                cls = int(cls)
                oid = int(oid)

                if cls not in VEHICLE_CLASSES:
                    continue

                x1, y1, x2, y2 = map(int, box)
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                label = CLASS_NAMES[cls]

                # ---------- LINE CROSSING CHECK ----------
                prev_y = prev_y_positions.get(oid, cy)
                prev_y_positions[oid] = cy

                if prev_y < LINE_Y <= cy and oid not in counted_ids:
                    vehicle_log[oid] = {
                        "track_id": oid,
                        "vehicle_type": label,
                        "entry_time": current_time,
                        "exit_time": None,
                        "waiting_time": 0.0,
                        "positions": [],
                        "is_wrong_direction": False,
                        "last_seen": current_time,
                        "stopped_since": None
                    }
                    counted_ids.add(oid)

                # ---------- UPDATE ONLY IF COUNTED ----------
                if oid in vehicle_log:
                    v = vehicle_log[oid]
                    v["positions"].append((cx, cy, frame_count))
                    v["last_seen"] = current_time

                    prev = prev_positions.get(oid)
                    prev_positions[oid] = (cx, cy)

                    if prev:
                        motion = np.array([cx - prev[0], cy - prev[1]])
                        dist = np.linalg.norm(motion)

                        # Direction calibration
                        if not calibrated and dist > 2:
                            calibration_vectors.append(normalize(motion))
                            if len(calibration_vectors) >= CALIBRATION_FRAMES:
                                dominant_direction = normalize(
                                    np.mean(calibration_vectors, axis=0)
                                )
                                calibrated = True

                        # Wrong direction
                        if calibrated and dist > 2:
                            angle = angle_between(motion, dominant_direction)
                            if angle > WRONG_DIRECTION_ANGLE:
                                v["is_wrong_direction"] = True

                        # Waiting / speed
                        if dist > WAITING_PIXEL_THRESHOLD:
                            speed_records.append(dist / FRAME_SKIP)
                            v["stopped_since"] = None
                        else:
                            if v["stopped_since"] is None:
                                v["stopped_since"] = time.time()
                            else:
                                v["waiting_time"] += time.time() - v["stopped_since"]
                                v["stopped_since"] = time.time()

                    # Drawing
                    color = (0, 0, 255) if v["is_wrong_direction"] else (0, 255, 0)
                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(
                        frame,
                        f"{label} ID:{oid}",
                        (x1, y1 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        color,
                        2
                    )

    # ---------- EXIT DETECTION ----------
    now = datetime.now()
    for oid, v in vehicle_log.items():
        if v["exit_time"] is None and (now - v["last_seen"]).seconds > 2:
            v["exit_time"] = v["last_seen"]

    density, avg_speed, avg_wait = compute_analytics()
    update_signal(density, avg_wait)
    update_dashboard(density, avg_speed, avg_wait)

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = ImageTk.PhotoImage(Image.fromarray(rgb))
    video_label.config(image=img)
    video_label.image = img

    window.after(20, process_frame)


# =========================================================
# EXCEL EXPORT
# =========================================================
def download_excel():
    wb = Workbook()
    ws = wb.active
    ws.title = "Traffic Data"

    ws.append([
        "Vehicle ID",
        "Vehicle Type",
        "Entry Time",
        "Exit Time",
        "Total Waiting Time (sec)",
        "Wrong Direction"
    ])

    for v in vehicle_log.values():
        if v["exit_time"] is None:
            continue

        ws.append([
            v["track_id"],
            v["vehicle_type"],
            v["entry_time"].strftime("%Y-%m-%d %H:%M:%S"),
            v["exit_time"].strftime("%Y-%m-%d %H:%M:%S"),
            round(v["waiting_time"], 2),
            "YES" if v["is_wrong_direction"] else "NO"
        ])

    filename = f"traffic_data_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.xlsx"
    wb.save(filename)
    messagebox.showinfo("Export Successful", f"Saved as {filename}")

# =========================================================
# GUI
# =========================================================
window = tk.Tk()
window.title("Smart Traffic Monitoring System")
window.geometry("1400x800")

main = tk.Frame(window)
main.pack()

video_label = tk.Label(main)
video_label.grid(row=0, column=0, padx=10, pady=10)

dash = tk.Frame(main)
tk.Label(
    dash,
    text="🚘 Vehicle Passage Log",
    font=("Arial", 15, "bold")
).pack(pady=8)

columns = ("id", "type", "entry", "exit", "status")

vehicle_table = ttk.Treeview(
    dash,
    columns=columns,
    show="headings",
    height=14
)

vehicle_table.heading("id", text="ID")
vehicle_table.heading("type", text="Type")
vehicle_table.heading("entry", text="Entry Time")
vehicle_table.heading("exit", text="Exit Time")
vehicle_table.heading("status", text="Status")

vehicle_table.column("id", width=60, anchor="center")
vehicle_table.column("type", width=100, anchor="center")
vehicle_table.column("entry", width=120, anchor="center")
vehicle_table.column("exit", width=120, anchor="center")
vehicle_table.column("status", width=80, anchor="center")

vehicle_table.pack(pady=6)

lbl_total = tk.Label(dash, font=("Arial", 16, "bold"), fg="blue")
lbl_car = tk.Label(dash, font=("Arial", 13))
lbl_bike = tk.Label(dash, font=("Arial", 13))
lbl_bus = tk.Label(dash, font=("Arial", 13))
lbl_truck = tk.Label(dash, font=("Arial", 13))

lbl_total.pack(pady=8)
lbl_car.pack()
lbl_bike.pack()
lbl_bus.pack()
lbl_truck.pack()

dash.grid(row=0, column=1, sticky="n")

lbl_density = tk.Label(dash, font=("Arial", 14))
lbl_speed = tk.Label(dash, font=("Arial", 14))
lbl_wait = tk.Label(dash, font=("Arial", 14))
lbl_signal = tk.Label(dash, font=("Arial", 20, "bold"))

lbl_density.pack(pady=6)
lbl_speed.pack(pady=6)
lbl_wait.pack(pady=6)
lbl_signal.pack(pady=10)

def update_dashboard(density, speed, wait):
    passed = compute_passed_vehicle_counts()
    total_passed = sum(passed.values())

    lbl_total.config(text=f"🚘 Total Passed: {total_passed}")
    lbl_car.config(text=f"🚗 Cars: {passed['Car']}")
    lbl_bike.config(text=f"🏍 Motorcycles: {passed['Motorcycle']}")
    lbl_bus.config(text=f"🚌 Buses: {passed['Bus']}")
    lbl_truck.config(text=f"🚚 Trucks: {passed['Truck']}")

    lbl_density.config(text=f"Live Density: {density}")
    lbl_speed.config(text=f"Avg Speed: {speed} px/frame")
    lbl_wait.config(text=f"Avg Wait: {wait} sec")

    lbl_signal.config(
        text=f"🚦 SIGNAL: {signal_state}",
        fg="red" if signal_state == "RED" else "green"
    )

    update_vehicle_table()




controls = tk.Frame(window)
controls.pack(pady=10)

tk.Button(controls, text="Open Video", width=15,
          command=lambda: start_video(
              filedialog.askopenfilename(filetypes=[("Video Files", "*.mp4 *.avi *.mov")])
          )).grid(row=0, column=0, padx=5)

tk.Button(controls, text="Start Webcam", width=15,
          command=lambda: start_video()).grid(row=0, column=1, padx=5)

tk.Button(controls, text="Stop", width=15,
          command=stop_video).grid(row=0, column=2, padx=5)

tk.Button(controls, text="Download Excel", width=18,
          command=download_excel).grid(row=0, column=3, padx=5)

window.mainloop()
