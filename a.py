#source face_env/bin/activate
import base64
from aip import AipFace

# ==================== 初始化配置 ====================
APP_ID = '123044418'
API_KEY = 'VWtQmoKO44mDYdD3vGlXbi0I'
SECRET_KEY = 'BsnaarSH1Bv5TfC8nWDTLYtKTln70whR'
GROUP_ID = 'equipment_users'          # 人脸库组名，如 'raspberry_group'
MATCH_THRESHOLD = 80              # 匹配阈值（0-100），官方推荐80分以上为同一人[citation:4][citation:5]

# 初始化客户端
client = AipFace(APP_ID, API_KEY, SECRET_KEY)

def get_user_id_from_face(image_base64):
    """从人脸图片中识别并返回云端匹配的user_id"""
    # 定义搜索参数，关键是要指定在人脸库的哪个组（GROUP_ID）中搜索[citation:4]
    options = {
        "match_threshold": str(MATCH_THRESHOLD),   # 设置阈值，低于此分的用户不会返回
        "quality_control": "NORMAL",               # 质量控制
        "max_user_num": 1                          # 最多只返回1个最匹配的用户
    }
    
    # 调用百度云的人脸搜索接口
    # image_type: 'BASE64' 表示传入的是经过base64编码的图片数据[citation:5]
    result = client.search(image_base64, 'BASE64', GROUP_ID, options)
    
    # 解析返回结果
    if result['error_msg'] == 'SUCCESS':
        # 从返回的JSON结构中提取user_id
        # 官方返回结构: result -> user_list[0] -> user_id[citation:3][citation:4][citation:9]
        user_id = result['result']['user_list'][0]['user_id']
        score = result['result']['user_list'][0]['score']
        
        print(f"识别成功！用户ID: {user_id}, 匹配得分: {score}")
        return user_id
    else:
        print(f"识别失败: {result['error_msg']}")
        return None

# ==================== 使用示例 ====================
# 假设你已经有一张包含人脸的图片，并且已经转换为Base64
# 实际使用时，这个函数会接收树莓派上传的每一帧画面

with open("sea fu star.jpg", "rb") as f:
    base64_data = base64.b64encode(f.read()).decode('utf-8')
    
identified_user = get_user_id_from_face(base64_data)
if identified_user:
    print(f"欢迎回来，用户 {identified_user}")
else:
    print("未在云端找到匹配的人脸")