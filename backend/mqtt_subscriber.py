import paho.mqtt.client as mqtt
import redis

# ==========================================
# KONFIGURASI SESUAI LAPORAN (BAB 8.1)
# ==========================================
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_TOPIC = "capstone/stefany/esp32s3/camera/base64"

# Koneksi ke Redis (Pastikan aplikasi Redis Server sudah berjalan di laptopmu)
redis_client = redis.Redis(host='localhost', port=6379, db=0)
REDIS_QUEUE_NAME = "cv_task_queue"

def on_connect(client, userdata, flags, rc):
    print(f"[MQTT SERVICE] Terhubung ke HiveMQ (Status: {rc})")
    client.subscribe(MQTT_TOPIC)
    print(f"[MQTT SERVICE] Berlangganan ke topik: {MQTT_TOPIC}")

def on_message(client, userdata, msg):
    try:
        # Langsung lempar payload mentah ke dalam Redis Queue
        payload_str = msg.payload.decode('utf-8')
        
        # lpush = Left Push (Memasukkan data ke antrean Redis)
        redis_client.lpush(REDIS_QUEUE_NAME, payload_str)
        print("[MQTT SERVICE] Pesan baru diterima dan dimasukkan ke Redis Queue.")
        
    except Exception as e:
        print(f"[MQTT ERROR] Gagal memproses pesan: {e}")

# Inisialisasi dan jalankan MQTT Subscriber
mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

print("[INFO] Menjalankan MQTT Subscriber Service...")
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_forever()