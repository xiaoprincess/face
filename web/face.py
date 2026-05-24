# face.py - 人脸验证程序（CSI摄像头版）
import time
import requests
import base64
import sys
import subprocess
import os
import cv2
import urllib3
urllib3.disable_warnings()

SERVER_URL = "https://127.0.0.1:5000"
TIMEOUT = 10
IMAGE_PATH = "/tmp/face_capture.jpg"

def capture_photo():
    """用rpicam拍照"""
    print("📷 正在拍照...")
    
    # 尝试不同的拍照命令
    commands = [
        ['rpicam-still', '-o', IMAGE_PATH, '--width', '640', '--height', '480', '-t', '100'],
        ['libcamera-still', '-o', IMAGE_PATH, '--width', '640', '--height', '480', '-t', '100'],
        ['raspistill', '-o', IMAGE_PATH, '-w', '640', '-h', '480', '-t', '100'],
    ]
    
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=5)
            if result.returncode == 0 and os.path.exists(IMAGE_PATH):
                print(f"✅ 拍照成功: {cmd[0]}")
                
                # 读取照片
                frame = cv2.imread(IMAGE_PATH)
                if frame is not None:
                    _, buffer = cv2.imencode('.jpg', frame)
                    return base64.b64encode(buffer).decode('utf-8')
        except:
            continue
    
    print("❌ 所有拍照命令都失败")
    return None

def capture_face():
    """检测人脸"""
    print(f"⏰ 等待人脸中...（超时: {TIMEOUT}秒）")
    
    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    start_time = time.time()
    
    while time.time() - start_time < TIMEOUT:
        # 拍照
        image_base64 = capture_photo()
        if not image_base64:
            return None
        
        # 检测人脸
        img_data = base64.b64decode(image_base64)
        nparr = cv2.imdecode(np.frombuffer(img_data, np.uint8), cv2.IMREAD_COLOR) if 'np' in dir() else None
        
        import numpy as np
        nparr = np.frombuffer(base64.b64decode(image_base64), np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(80, 80))
        
        elapsed = int(time.time() - start_time)
        remaining = TIMEOUT - elapsed
        
        if len(faces) > 0:
            print(f"✅ 检测到人脸！剩余 {remaining} 秒")
            return image_base64
        
        if elapsed % 5 == 0:
            print(f"⏳ 未检测到人脸... 剩余 {remaining} 秒")
        
        time.sleep(1)
    
    print(f"⏰ 超时！{TIMEOUT}秒内未检测到人脸")
    return None

def verify_and_apply(equipment_name):
    print(f"\n🔍 正在验证: {equipment_name}")
    
    image = capture_face()
    if not image:
        print("❌ 无人脸，申请取消")
        return False
    
    try:
        resp = requests.post(
            f"{SERVER_URL}/api/face_verify",
            json={"image": image, "equipment_name": equipment_name},
            verify=False, timeout=10
        )
        result = resp.json()
        
        if result.get('success'):
            print(f"✅ {result.get('username')} 申请成功")
            return True
        else:
            print(f"❌ {result.get('message')}")
            return False
    except Exception as e:
        print(f"❌ 请求失败: {e}")
        return False

if __name__ == '__main__':
    if len(sys.argv) > 1:
        equipment = sys.argv[1]
        verify_and_apply(equipment)
    else:
        print("用法: python3 face.py <器材名称>")