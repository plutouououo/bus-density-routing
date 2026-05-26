"""Konfigurasi pytest: tambahkan folder backend/ ke sys.path agar
import `services.dijkstra` bisa di-resolve saat test dijalankan dari
direktori manapun (mis. dari `backend/` atau dari root proyek).
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
