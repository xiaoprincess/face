#source /home/xiao/face/face_env/bin/activate
import base64
import cv2
import time
import threading
import queue
import sys
import socket
import json
from aip import AipFace
from picamera2 import Picamera2

# ==================== 配置（请核对您的API信息） ====================
APP_ID = '123044418'
API_KEY = 'VWtQmoKO44mDYdD3vGlXbi0I'
SECRET_KEY = 'BsnaarSH1Bv5TfC8nWDTLYtKTln70whR'
GROUP_ID = 'equipment_users'
MATCH_THRESHOLD = 60
DETECT_INTERVAL = 1.5
FRAME_WIDTH = 640
FRAME_HEIGHT = 480

# ==================== 局域网通知配置 ====================
# 接收端设备的IP地址和端口（根据实际情况修改）
RECEIVER_IP = '192.168.231.67'   # 🔴 替换为接收端设备的实际IP
RECEIVER_PORT = 5000             # 通信端口（与接收端保持一致）
NOTIFICATION_ENABLED = True      # 是否启用通知功能

# 初始化百度AI客户端
client = AipFace(APP_ID, API_KEY, SECRET_KEY)

# 初始化摄像头
picam2 = Picamera2()
config = picam2.create_preview_configuration(main={"size": (FRAME_WIDTH, FRAME_HEIGHT), "format": "RGB888"})
picam2.configure(config)
picam2.start()
time.sleep(1)

task_queue = queue.Queue(maxsize=5)
result_queue = queue.Queue()

# 用于防重复通知（同一个用户短时间内只通知一次）
last_notification_time = 0
last_notification_user = None
NOTIFICATION_COOLDOWN = 5  # 同一用户5秒内不重复通知

# ==================== 发送通知函数 ====================
def send_notification(user_id, score):
    """
    向局域网内接收端发送识别成功通知
    """
    global last_notification_time, last_notification_user
    
    # 防重复通知检查
    current_time = time.time()
    if user_id == last_notification_user and (current_time - last_notification_time) < NOTIFICATION_COOLDOWN:
        print(f"⏱️ 通知冷却中，跳过重复通知: {user_id}")
        return False
    
    if not NOTIFICATION_ENABLED:
        print("📤 通知功能已禁用")
        return False
    
    try:
        # 创建socket连接
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)  # 设置2秒超时
        
        # 准备要发送的数据
        notification = {
            'event': 'face_recognized',
            'user_id': user_id,
            'score': score,
            'timestamp': current_time,
            'message': f'用户 {user_id} 验证通过',
            'device': 'raspberry_pi'
        }
        
        # 连接到接收端
        sock.connect((RECEIVER_IP, RECEIVER_PORT))
        
        # 发送JSON格式的数据
        sock.send(json.dumps(notification).encode('utf-8'))
        
        # 等待接收端确认
        response = sock.recv(1024).decode('utf-8')
        print(f"通知已发送: {notification['message']}")
        if response:
            print(f" 接收端回复: {response}")
        
        sock.close()
        
        # 更新最后通知记录
        last_notification_time = current_time
        last_notification_user = user_id
        return True
        
    except socket.timeout:
        print(f"连接接收端超时 ( {RECEIVER_IP}:{RECEIVER_PORT} )，请检查接收端是否运行")
        return False
    except ConnectionRefusedError:
        print(f"连接被拒绝，请确认接收端程序已启动")
        return False
    except Exception as e:
        print(f"发送通知异常: {e}")
        return False

def get_user_id_from_face(image_base64):
    """从人脸图片中识别并返回云端匹配的user_id"""
    options = {
        "match_threshold": str(MATCH_THRESHOLD),
        "quality_control": "NONE",
        "max_user_num": 1
    }
    try:
        result = client.search(image_base64, 'BASE64', GROUP_ID, options)
        if result['error_msg'] == 'SUCCESS':
            user_id = result['result']['user_list'][0]['user_id']
            score = result['result']['user_list'][0]['score']
            print(f"！识别成功！用户ID: {user_id}, 匹配得分: {score}")
            return user_id, score
        else:
            if result['error_msg'] not in ['pic not has face', 'match user is not found']:
                print(f"识别失败: {result['error_msg']}")
            return None, 0
    except Exception as e:
        print(f"API调用异常: {e}")
        return None, 0

def image_to_base64(frame):
    _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return base64.b64encode(buffer).decode('utf-8')

def process_worker():
    """后台处理线程"""
    while True:
        try:
            frame, image_base64 = task_queue.get(timeout=1)
            user_id, score = get_user_id_from_face(image_base64)
            if user_id:
                result_queue.put((user_id, score))
            task_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            print(f"处理线程异常: {e}")

# 启动后台线程
worker_thread = threading.Thread(target=process_worker, daemon=True)
worker_thread.start()

# ==================== 主循环 ====================
print("=" * 50)
print("人脸识别系统已启动")
print(f"通知目标: {RECEIVER_IP}:{RECEIVER_PORT}")
print("按 Ctrl+C 退出...")
print("=" * 50)

last_detect_time = 0
last_user_id = None

try:
    while True:
        # 获取当前帧
        frame_rgb = picam2.capture_array()
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        
        current_time = time.time()
        
        # 每隔一段时间处理一帧
        if current_time - last_detect_time >= DETECT_INTERVAL:
            if task_queue.qsize() < 3:
                image_base64 = image_to_base64(frame_bgr)
                task_queue.put((frame_bgr.copy(), image_base64))
                last_detect_time = current_time
                print(".", end="", flush=True)
        
        # 检查是否有新的识别结果
        try:
            while not result_queue.empty():
                user_id, score = result_queue.get_nowait()
                if user_id:
                    last_user_id = user_id
                    print(f"\n检测到授权用户: {user_id} (得分: {score})")
                    
                    # 🔴 在这里发送局域网通知
                    send_notification(user_id, score)
                    
                    # 在这里添加您的自定义动作，比如控制GPIO
                    # import RPi.GPIO as GPIO
                    # GPIO.output(17, GPIO.HIGH)  # 开门
                    # time.sleep(2)
                    # GPIO.output(17, GPIO.LOW)
                    
                result_queue.task_done()
        except queue.Empty:
            pass
            
        time.sleep(0.05)  # 短暂休息，避免CPU占用过高

except KeyboardInterrupt:
    print("\n程序被用户中断")
finally:
    picam2.stop()
    cv2.destroyAllWindows()
    print("系统已关闭")