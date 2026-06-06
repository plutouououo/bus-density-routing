import cv2
import time
import json
import base64
import numpy as np
import redis
from ultralytics import YOLO

# (Di-comment sementara karena belum pakai Supabase)
# from supabase import create_client, Client
# SUPABASE_URL = "https://xyzcompany.supabase.co"
# SUPABASE_KEY = "ey-your-anon-key"
# supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================
# KONFIGURASI SISTEM
# ==========================================
redis_client = redis.Redis(host='localhost', port=6379, db=0)
REDIS_QUEUE_NAME = "cv_task_queue"

MODEL_PATH = "yolo26n.pt"
MAX_CAPACITY = 80  
model = YOLO(MODEL_PATH)

def safe_enhance(image):
    """ Ekualisasi pencahayaan (CLAHE) yang aman untuk model CV """
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
    cl = clahe.apply(l)
    limg = cv2.merge((cl, a, b))
    return cv2.cvtColor(limg, cv2.COLOR_LAB2BGR)

print("[INFO] CV Worker Service berjalan. Menunggu antrean dari Redis... (Tekan Ctrl+C untuk keluar)")

try:
    while True:
        try:
            # Menggunakan timeout=1 agar responsif terhadap Ctrl+C
            queue_item = redis_client.brpop(REDIS_QUEUE_NAME, timeout=1)
            
            if queue_item:
                payload_str = queue_item[1].decode('utf-8')
                
                # --- PERBAIKAN ERROR JSON ---
                # Coba baca sebagai JSON. Jika gagal, anggap sebagai string Base64 mentah
                try:
                    data = json.loads(payload_str)
                    bus_id = data.get("bus_id", "Koridor_1_A")
                    b64_string = data.get("image", "")
                except json.JSONDecodeError:
                    # Ini akan berjalan jika kamu hanya mengirim teks Base64 mentah dari MQTT
                    bus_id = "Koridor_Test_Lokal"
                    b64_string = payload_str
                # ----------------------------

                # Cek jika string base64 kosong untuk menghindari error OpenCV
                if not b64_string:
                    print("[WARNING] String gambar kosong, mengabaikan proses.")
                    continue

                # 1. DEKODE BASE64 KE JPG
                img_data = base64.b64decode(b64_string)
                np_arr = np.frombuffer(img_data, np.uint8)
                frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

                if frame is None:
                    print("[WARNING] Gagal mendekode gambar Base64. Pastikan string Base64 valid.")
                    continue

                # 2. PRA-PEMROSESAN (CLAHE)
                enhanced_frame = safe_enhance(frame)

                # 3. INFERENSI YOLO26
                results = model(enhanced_frame, classes=[0], conf=0.20, iou=0.6, imgsz=640, verbose=False)
                
                # 4. FILTERING BOUNDING BOX & HEADCOUNT
                valid_headcount = 0
                total_pixels = frame.shape[0] * frame.shape[1]
                
                for box in results[0].boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0].cpu().numpy())
                    box_width, box_height = (x2 - x1), (y2 - y1)
                    box_area = box_width * box_height
                    
                    if box_area < (total_pixels * 0.0005) or box_area > (total_pixels * 0.7):
                        continue
                    aspect_ratio = box_height / float(box_width)
                    if aspect_ratio < 0.2: 
                        continue
                        
                    valid_headcount += 1

                # 5. PERHITUNGAN LOAD FACTOR
                lf = valid_headcount / MAX_CAPACITY
                
                if lf < 0.50:
                    status_text = "SEPI"
                elif 0.50 <= lf < 0.80:
                    status_text = "SEDANG"
                elif 0.80 <= lf < 1.00:
                    status_text = "PADAT"
                else:
                    status_text = "SANGAT PADAT"

                # 6. PENYIMPANAN SEMENTARA (Tanpa Supabase)
                db_payload = {
                    "bus_id": bus_id,
                    "jumlah_penumpang": valid_headcount,
                    "kepadatan": round(lf, 2)
                }
                
                # Di-comment sementara:
                # response = supabase.table("kepadatan_realtime").insert(db_payload).execute()
                
                print(f"[CV WORKER] {bus_id} diproses | Penumpang: {valid_headcount}/{MAX_CAPACITY} | Status: {status_text} | LF: {lf:.2f}")
                print(f"            [SIMULASI DB] Data yang akan dikirim ke DB: {db_payload}\n")
                
        except Exception as e:
            # Peringatan error tidak akan mematikan program, melainkan melewatinya
            print(f"[CV WORKER ERROR] Terjadi kesalahan saat memproses data antrean: {e}")

except KeyboardInterrupt:
    print("\n[INFO] Mematikan CV Worker Service dengan aman...")