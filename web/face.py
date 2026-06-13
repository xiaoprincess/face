# face.py - 百度AI人脸验证（使用 picamera2 库拍照）
import time
import requests
import base64
import sys
import os
import io
import urllib3
from picamera2 import Picamera2

urllib3.disable_warnings()

SERVER_URL = "http://127.0.0.1:5050"
TIMEOUT = 30
IMAGE_PATH = "/tmp/face_capture.jpg"

# ========== 初始化摄像头（只初始化一次） ==========
_picam2 = None

def init_camera():
    """初始化摄像头（全局只做一次）"""
    global _picam2
    if _picam2 is None:
        _picam2 = Picamera2()
        config = _picam2.create_still_configuration(
            main={"size": (640, 480), "format": "RGB888"}
        )
        _picam2.configure(config)
        _picam2.start()
        time.sleep(1)  # 等摄像头稳定
    return _picam2


def capture_photo():
    """用 picamera2 库拍照，返回 base64 字符串"""
    try:
        picam2 = init_camera()
        
        # 用 BytesIO 在内存中保存照片，不写磁盘
        stream = io.BytesIO()
        picam2.capture_file(stream, format='jpeg')
        stream.seek(0)
        
        # 转 base64
        image_base64 = base64.b64encode(stream.read()).decode('utf-8')
        
        # 同时也存一份到磁盘（可选，方便调试查看）
        picam2.capture_file(IMAGE_PATH)
        
        return image_base64
    except Exception as e:
        print(f"📷 拍照失败: {e}")
        return None


def close_camera():
    """释放摄像头"""
    global _picam2
    if _picam2:
        _picam2.stop()
        _picam2 = None


def main():
    equipment = sys.argv[1] if len(sys.argv) > 1 else "测试"
    print(f"🔍 验证: {equipment}")
    print(f"⏰ 超时: {TIMEOUT}秒")
    
    start_time = time.time()
    
    try:
        while time.time() - start_time < TIMEOUT:
            remaining = TIMEOUT - int(time.time() - start_time)
            print(f"📷 拍照中... 剩余 {remaining} 秒")
            
            image = capture_photo()
            if not image:
                time.sleep(2)
                continue
            
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
            
            time.sleep(2)
        
        print("⏰ 超时，无人脸识别成功")
    
    finally:
        close_camera()


if __name__ == '__main__':
    main()