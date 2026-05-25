# face.py - 百度AI人脸验证（跳过本地检测）
import time
import requests
import base64
import sys
import subprocess
import os
import urllib3
urllib3.disable_warnings()

SERVER_URL = "https://127.0.0.1:5000"
TIMEOUT = 30
IMAGE_PATH = "/tmp/face_capture.jpg"

def capture_photo():
    """拍照"""
    if os.path.exists(IMAGE_PATH):
        os.remove(IMAGE_PATH)
    
    cmd = ['rpicam-still', '-o', IMAGE_PATH, '--width', '640', '--height', '480', '-t', '200', '--nopreview']
    result = subprocess.run(cmd, capture_output=True, timeout=10)
    
    if os.path.exists(IMAGE_PATH) and os.path.getsize(IMAGE_PATH) > 0:
        with open(IMAGE_PATH, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    return None

def main():
    equipment = sys.argv[1] if len(sys.argv) > 1 else "测试"
    print(f"🔍 验证: {equipment}")
    print(f"⏰ 超时: {TIMEOUT}秒")
    
    start_time = time.time()
    
    while time.time() - start_time < TIMEOUT:
        remaining = TIMEOUT - int(time.time() - start_time)
        print(f"📷 拍照中... 剩余 {remaining} 秒")
        
        image = capture_photo()
        if not image:
            time.sleep(2)
            continue
        
        # 直接发给百度AI识别
        try:
            resp = requests.post(
                f"{SERVER_URL}/api/face_verify",
                json={"image": image, "equipment_name": equipment},
                verify=False, timeout=30
            )
            result = resp.json()
            
            if result.get('success'):
                print(f"✅ {result.get('username')} 申请成功！")
                return
            else:
                msg = result.get('message', '')
                if '未找到匹配' in msg:
                    print(f"⏳ 未识别到人脸，重试...")
                else:
                    print(f"❌ {msg}")
        except Exception as e:
            print(f"⚠️ 请求失败: {e}")
        
        time.sleep(3)
    
    print("⏰ 超时，无人脸识别成功")

if __name__ == '__main__':
    main()