import cv2
import time
import numpy as np
from ultralytics import YOLO

def safe_enhance(image):
    """
    Hanya mencerahkan bagian gelap (CLAHE) tanpa merusak tekstur asli.
    Tidak menggunakan Sharpening agar YOLO tidak bingung.
    """
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    
    # clipLimit diturunkan jadi 1.5 agar pencerahannya sangat natural (tidak over-ekspos)
    clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
    cl = clahe.apply(l)
    
    limg = cv2.merge((cl, a, b))
    return cv2.cvtColor(limg, cv2.COLOR_LAB2BGR)

# ==========================================
# KONFIGURASI SISTEM
# ==========================================
MODEL_PATH = "yolo26n.pt"
BUS_ID = "Koridor_1_A"
IMAGE_PATH = r"bus_banyak.jpeg"

# MAX_CAPACITY disesuaikan dengan Bab 4.2 di Laporan Capstone (80 penumpang).
# Catatan: Ubah kembali ke 5 atau 10 jika kamu ingin melihat status berubah 
# menjadi "PADAT" saat testing menggunakan gambar bus_banyak.jpeg
MAX_CAPACITY = 10 

model = YOLO(MODEL_PATH)

print(f"[INFO] Memulai deteksi cerdas pada bus {BUS_ID}...")

frame = cv2.imread(IMAGE_PATH)
if frame is None:
    print(f"[ERROR] Gambar tidak ditemukan: {IMAGE_PATH}")
else:
    # 1. PRA-PEMROSESAN YANG AMAN
    enhanced_frame = safe_enhance(frame)

    # 2. INFERENCE OBJECT DETECTION
    start_time = time.time()
    
    results = model(enhanced_frame, classes=[0], conf=0.20, iou=0.6, imgsz=640, verbose=False)
    
    inference_time_ms = int((time.time() - start_time) * 1000)
    
    # 3. PASCA-PEMROSESAN (FILTERING BOUNDING BOX)
    valid_headcount = 0
    annotated_frame = frame.copy() 
    
    total_pixels = frame.shape[0] * frame.shape[1]
    
    boxes = results[0].boxes
    for box in boxes:
        x1, y1, x2, y2 = map(int, box.xyxy[0].cpu().numpy())
        conf_score = float(box.conf[0].cpu().numpy())
        
        box_width = x2 - x1
        box_height = y2 - y1
        box_area = box_width * box_height
        
        # FILTER AREA
        if box_area < (total_pixels * 0.0005) or box_area > (total_pixels * 0.7):
            continue
            
        # FILTER RASIO
        aspect_ratio = box_height / float(box_width)
        if aspect_ratio < 0.2: 
            continue
            
        valid_headcount += 1
        
        # MENGGAMBAR BOUNDING BOX BERWARNA BIRU (Format OpenCV: BGR)
        box_color = (255, 0, 0)
        cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), box_color, 2)
        cv2.putText(annotated_frame, f"person {conf_score:.2f}", (x1, y1 - 5), 
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, box_color, 2)

    # 4. LOGIKA 4 TINGKAT KEPADATAN (Sesuai Bab 4.2 Laporan Capstone)
    lf = valid_headcount / MAX_CAPACITY
    
    if lf < 0.50:
        status_text, status_color = "SEPI", (0, 255, 0)          # Hijau
    elif 0.50 <= lf < 0.80:
        status_text, status_color = "SEDANG", (0, 255, 255)      # Kuning
    elif 0.80 <= lf < 1.00:
        status_text, status_color = "PADAT", (0, 165, 255)       # Oranye (BGR)
    else:
        status_text, status_color = "SANGAT PADAT", (0, 0, 255)  # Merah

    # OVERLAY METRIK DI SUDUT KIRI ATAS
    cv2.putText(annotated_frame, f"Total: {valid_headcount}/{MAX_CAPACITY} (LF: {lf:.2f})", (20, 40), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
    cv2.putText(annotated_frame, f"Status: {status_text}", (20, 80), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, status_color, 2)
    
    cv2.imshow("Hasil Deteksi Cerdas", annotated_frame)
    
    cv2.waitKey(10000)
    cv2.destroyAllWindows()