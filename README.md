# 实验器材申请管理系统

基于 Flask + 百度 AI 人脸识别的实验室器材借还系统，支持 Web 端和 ESP32 硬件端。人脸识别成功后，树莓派控制云台和激光自动指向对应器材柜。

## 涉及技术

| 技术 | 用途 |
|---|---|
| Python Flask | Web 服务端框架 |
| SQLite | 本地数据库（用户、申请、柜子、系统设置） |
| Jinja2 | 服务端模板渲染 |
| 百度 AI SDK (`baidu-aip`) | 人脸注册、1:N 人脸搜索 |
| WebRTC (`getUserMedia`) | 浏览器调用摄像头拍照 |
| Nginx | 反向代理 + HTTPS |
| Picamera2 | 树莓派摄像头控制 |
| AprilTag | 视觉标签定位 |
| GPIO / 步进电机 | 云台转动 + 激光指向 |

## 系统架构

```
ESP32 ──HTTP(:5000)──▶ Nginx ──▶ Flask(:5050) ──▶ 百度 AI 人脸API
浏览器──HTTPS(:443)──▶ Nginx ──▶ Flask(:5050) ──▶ SQLite
树莓派(摄像头+云台) ─▶ Flask API ──▶ 百度 AI 人脸API
```

---

## 使用流程

### 一、用户注册

```
浏览器打开 https://服务器IP/register
  → 填写用户名、密码
  → 浏览器调用摄像头拍一张人脸照片
  → 提交 → 本地数据库建用户 → 百度 AI 注册人脸
  → 注册成功，跳转登录页
```

- 不拍人脸照也可以注册，但只能账号密码登录，无法使用人脸识别。
- 百度 AI 使用用户数据库 ID 作为人脸标识（`user_id`），两边保持同步。

### 二、Web 端登录与申请

#### 账号密码登录

```
浏览器 → /login → 输入用户名密码 → 登录成功 → 进入用户面板
```

#### 人脸识别登录

```
浏览器 → /login → 点击"人脸识别登录"
  → 浏览器弹出摄像头 → 拍照
  → POST /api/face_login (base64图片)
  → 后端调用百度 AI 1:N 人脸搜索
  → 匹配成功(score ≥ 70) → 写 Session → 返回跳转地址
  → 自动进入用户面板
```

> 浏览器调用摄像头必须使用 HTTPS（本地开发 `localhost` 例外）。

#### 申请器材

```
用户面板 → 填写器材名称、数量、用途、期望日期、拍照上传
  → POST /apply → 创建申请（状态：pending）
  → 等待管理员审批
```

#### 查看申请状态

```
用户面板 → 显示所有申请的列表
  → pending（待审批）/ approved（已通过）/ rejected（已拒绝）
  → 可取消 pending 状态的申请
```

### 三、管理员审批

```
管理员登录 → 管理面板
  → 查看所有用户申请（按 pending > approved > rejected 排序）
  → 点击"批准" → 状态变为 approved
  → 点击"拒绝" → 填写原因 → 状态变为 rejected
```

管理员还可以：
- **管理用户**：删除用户（admin 不可删）
- **管理柜子**：创建/编辑/删除器材柜子，每个柜子可添加多个元件
- **云台设置**：调整激光亮灯时长、人脸识别时云台角度

### 四、ESP32 硬件端申请

```
用户站在摄像头前，ESP32 发送 GET /command?cmd=显微镜
  → Flask 启动后台线程，30 秒内循环尝试：

  ① 云台转向人脸位置（_turn_to_face）
  ② Picamera2 拍照（640x480）
  ③ 百度 AI 人脸搜索 → 匹配用户
  ④ 匹配成功 → 自动创建申请记录
  ⑤ 关键词匹配对应柜子（match_cabinet）
  ⑥ 云台转回原位（_turn_back_from_face）
  ⑦ Picamera2 切高分辨率（1920x1080）
  ⑧ 开激光 → AprilTag 视觉对准柜子 → 持续亮灯 → 关激光
```

整个流程全自动，无需人工干预。

---

## 数据库表

| 表 | 说明 |
|---|---|
| `users` | 用户（id 同步为百度 AI user_id） |
| `applications` | 器材申请记录 |
| `cabinets` | 器材柜子 |
| `cabinet_components` | 柜子内元件 |
| `settings` | 系统设置持久化（激光延时、云台角度） |

---

## API 接口

### Web 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/login` | 账号密码登录 |
| GET/POST | `/register` | 注册 + 人脸采集 |
| GET | `/logout` | 登出 |
| GET | `/user` | 用户面板 |
| GET | `/admin` | 管理员面板 |
| POST | `/apply` | 提交器材申请 |
| POST | `/approve/<id>` | 批准申请 |
| POST | `/reject/<id>` | 拒绝申请 |
| POST | `/cancel/<id>` | 取消申请 |

### 人脸 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/face_login` | 人脸登录（搜索 → 写 Session → 跳转） |
| POST | `/api/face_verify` | 人脸验证 + 创建申请 + 云台指向 |
| POST | `/api/face_apply` | ESP32 提交人脸结果 + 创建申请 |

### 云台设置

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/laser_delay` | 获取/设置激光延时（秒） |
| GET/POST | `/api/face_angles` | 获取/设置人脸云台角度 |

### 柜子管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/cabinets` | 柜子列表 JSON |
| GET | `/api/cabinet/<id>` | 柜子详情（含元件 + 关联申请） |
| POST | `/cabinet/create` | 创建柜子 |
| POST | `/cabinet/edit/<id>` | 编辑柜子 |
| POST | `/cabinet/delete/<id>` | 删除柜子 |
| POST | `/cabinet/batch_delete` | 批量删除 |

### ESP32 硬件

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/command?cmd=<器材名>` | 触发人脸识别 + 自动申请 + 激光指向 |

---

## 快速部署

### 1. 环境

- Python 3.8+
- 树莓派（可选，硬件控制需要）
- Nginx（可选，生产环境 HTTPS 需要）

### 2. 安装

```bash
git clone <仓库地址> face
cd face/web
python3 -m venv face_env
source face_env/bin/activate
pip install flask baidu-aip
```

### 3. 配置百度 AI

编辑 `start.py`，替换百度 AI 凭据：

```python
APP_ID = '你的APP_ID'
API_KEY = '你的API_KEY'
SECRET_KEY = '你的SECRET_KEY'
```

> 在 [百度 AI 开放平台](https://console.bce.baidu.com/ai/#/ai/face/overview) 创建人脸识别应用获取。

### 4. 启动

```bash
python3 start.py
```

首次启动自动创建数据库 + 默认管理员：

| 角色 | 用户名 | 密码 |
|---|---|---|
| 管理员 | `admin` | `admin123` |

> 生产环境务必修改默认密码。

### 5. Nginx（可选）

```bash
sudo cp face_nginx.conf /etc/nginx/sites-available/face
sudo ln -s /etc/nginx/sites-available/face /etc/nginx/sites-enabled/
# 修改 SSL 证书路径
sudo nginx -t && sudo systemctl reload nginx
```

---

## 目录结构

```
face/
├── web/
│   ├── start.py              # Flask 主程序
│   ├── face.py               # 树莓派拍照工具
│   ├── face_nginx.conf       # Nginx 配置
│   ├── agent_yuntai/         # 云台控制子模块
│   ├── templates/
│   │   ├── login.html        # 登录页（账号+人脸双通道）
│   │   ├── register.html     # 注册页（含人脸采集）
│   │   ├── user.html         # 用户面板
│   │   └── admin.html        # 管理面板
│   └── static/uploads/       # 器材照片
├── 自定义唤醒词.hd             # 语音唤醒词
└── equipment_system.db        # SQLite 数据库
```
