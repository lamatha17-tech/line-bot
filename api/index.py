import sys
import os

# เพิ่ม root directory ใน sys.path เพื่อให้สามารถ import main.py ได้
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app
