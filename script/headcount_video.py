import cv2
import time
import requests
from ultralytics import YOLO

def enhance_low_light(image):
    """
    Fungsi Pre/Post-processing untuk menyeimbangkan dan meningkatkan 
    kecerahan pada kondisi pencahayaan yang kurang di dalam bus.
    """
    # Mengubah format warna BGR menjadi LAB
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    
    # Menerapkan CLAHE dengan clipLimit=2.0 agar noise tidak muncul berlebihan
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    cl = clahe.apply(l)
    
    # Menggabungkan kembali seluruh channel
    limg = cv2.merge((cl, a, b))
    
    # Mengembalikan gambar ke format BGR
    enhanced_image = cv2.cvtColor(limg, cv2.COLOR_LAB2BGR)
    return enhanced_image

# KONFIGURASI SISTEM
MODEL_PATH = "yolo26n.pt"
API_URL = "http://localhost:5000/api/update_density"
BUS_ID = "Koridor_1_A"
MAX_CAPACITY = 5 

# Ganti dengan path video Anda (atau gunakan 0 untuk webcam bawaan PC/Laptop)
VIDEO_PATH = "WhatsApp Video 2026-06-05 at 16.02.13.mp4" 

# Waktu interval (detik) untuk mengirim data ke server agar video tidak lag
SEND_INTERVAL_SEC = 5

model = YOLO(MODEL_PATH)
print(f"[INFO] Memulai simulasi deteksi video pada bus {BUS_ID}...")

cap = cv2.VideoCapture(VIDEO_PATH)

if not cap.isOpened():
    print(f"[ERROR] Video tidak dapat dibuka: {VIDEO_PATH}")
    exit()

last_send_time = time.time()

while cap.isOpened():
    ret, frame = cap.read()
    if not ret:
        print("[INFO] Video selesai atau terputus.")
        break

    # PRE-PROCESSING: Mencerahkan kondisi bus yang redup
    enhanced_frame = enhance_low_light(frame)

    # 1. INFERENCE OBJECT DETECTION
    start_time = time.time()
    
    # Menurunkan conf (confidence) agar objek kecil/tertutup bisa terdeteksi,
    # Mengatur iou untuk lebih toleran pada kepala yang tumpang tindih (dempet),
    # imgsz=640 menjaga resolusi agar kepala di belakang tidak hilang
    results = model(enhanced_frame, classes=[0], conf=0.20, iou=0.6, imgsz=640, verbose=False)
    
    inference_time_ms = int((time.time() - start_time) * 1000)
    # Menghindari pembagian dengan 0 jika sangat cepat
    fps = 1000 / inference_time_ms if inference_time_ms > 0 else 0
    
    detections = results[0].boxes
    headcount = len(detections)
    
    annotated_frame = results[0].plot()

    # 2. LOGIKA STATUS KEPADATAN
    # Load Factor (LF) = Headcount / Max Capacity
    lf = headcount / MAX_CAPACITY
    
    if lf < 0.50:
        status_text, color = "SEPI (GREEN)", (0, 255, 0)
    elif 0.50 <= lf < 0.80:
        status_text, color = "SEDANG (YELLOW)", (0, 255, 255)
    elif 0.80 <= lf < 1.00:
        status_text, color = "PADAT (ORANGE)", (0, 165, 255) # Format BGR (Oranye)
    else:
        status_text, color = "SANGAT_PADAT (RED)", (0, 0, 255)

    # ==========================================
    # 3. PENGIRIMAN DATA IOT KE SERVER
    # ==========================================
    current_time = time.time()
    if current_time - last_send_time >= SEND_INTERVAL_SEC:
        payload = {
            "bus_id": BUS_ID,
            "headcount": headcount,
            "status": status_text.split(" ")[0]
        }
        try:
            # Gunakan timeout singkat agar video tidak patah-patah kalau server down
            response = requests.post(API_URL, json=payload, timeout=0.5)
            print(f"[SUCCESS] Payload terkirim: {payload}")
        except requests.exceptions.RequestException:
            print(f"[WARNING] Gagal mengirim data ke server.")
        
        last_send_time = current_time

    # ==========================================
    # 4. OVERLAY METRIK PADA GAMBAR
    # ==========================================
    cv2.putText(annotated_frame, f"Total: {headcount}/{MAX_CAPACITY} (LF: {lf:.2f})", (20, 40), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    cv2.putText(annotated_frame, f"Status: {status_text}", (20, 80), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    cv2.putText(annotated_frame, f"FPS: {fps:.1f}", (20, 120), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 255), 2)
    
    cv2.imshow("Hasil Deteksi Video", annotated_frame)
    
    # Tekan 'q' untuk keluar
    if cv2.waitKey(1) & 0xFF == ord('q'):
        print("[INFO] Deteksi dihentikan pengguna.")
        break

cap.release()
cv2.destroyAllWindows()
