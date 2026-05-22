#source /home/xiao/face/face_env/bin/activate
# face_verify.py - 人脸验证服务
import cv2
import time
import requests
import json
import base64
import os
from datetime import datetime

# 配置
FLASK_SERVER = "https://127.0.0.1:5000"  # 本机Flask地址
CAMERA_INDEX = 0  # 摄像头编号

def capture_face():
    """拍照并返回base64"""
    cap = cv2.VideoCapture(CAMERA_INDEX)
    time.sleep(1)  # 等摄像头预热
    
    ret, frame = cap.read()
    cap.release()
    
    if not ret:
        print("❌ 拍照失败")
        return None
    
    # 转base64
    _, buffer = cv2.imencode('.jpg', frame)
    image_base64 = base64.b64encode(buffer).decode('utf-8')
    return image_base64

def verify_face(equipment_name):
    """人脸验证并创建申请"""
    print("\n" + "="*50)
    print("🔍 正在进行人脸验证...")
    
    # 1. 拍照
    image = capture_face()
    if not image:
        return False
    
    # 2. 调用Flask的人脸登录API
    try:
        # 先用人脸搜索识别是谁
        resp = requests.post(
            f"{FLASK_SERVER}/api/face_verify",
            json={"image": image, "equipment_name": equipment_name},
            verify=False,  # 忽略自签证书
            timeout=10
        )
        result = resp.json()
        
        if result.get('success'):
            print(f"✅ 人脸验证通过: {result.get('username')}")
            print(f"✅ 已创建申请: {equipment_name}")
            return True
        else:
            print(f"❌ 人脸验证失败: {result.get('message')}")
            return False
            
    except Exception as e:
        print(f"❌ 请求失败: {e}")
        return False

if __name__ == '__main__':
    # 测试：验证人脸并申请"酒精"
    verify_face("酒精")