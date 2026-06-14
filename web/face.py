# face.py - 相机工具模块（拍照返回 base64）
import io
import base64


def capture_photo(picam2):
    """用给定的 Picamera2 实例拍照，返回 base64 字符串"""
    stream = io.BytesIO()
    picam2.capture_file(stream, format='jpeg')
    stream.seek(0)
    return base64.b64encode(stream.read()).decode('utf-8')
