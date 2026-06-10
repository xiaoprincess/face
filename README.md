# 实验器材申请管理系统

基于 Flask + 百度 AI 人脸识别的器材申请管理系统，支持 Web 端和 ESP32 硬件端申请器材。

## 系统架构

```plain
ESP32 ──HTTP(:5000)──▶ Nginx ──▶ Flask(:5050) ──▶ Baidu AI 人脸API
浏览器──HTTPS(:443)──▶ Nginx ──▶ Flask(:5050) ──▶ SQLite
树莓派(摄像头) ───────▶ Flask API ──▶ Baidu AI 人脸API
```

## 部署步骤

### 1. 环境要求

- Python 3.8+
- pip
- Nginx（可选，用于 HTTPS 和反向代理）
- 树莓派（可选，用于拍照人脸验证）

### 2. 克隆项目

```bash
git clone <仓库地址> face
cd face/web
```

### 3. 创建虚拟环境

```bash
python3 -m venv face_env
source face_env/bin/activate   # Linux/Mac
# 或
face_env\Scripts\activate      # Windows
```

### 4. 安装依赖

```bash
pip install flask baidu-aip
```

依赖说明：

| 包名 | 用途 |
|------|------|
| `flask` | Web 框架 |
| `baidu-aip` | 百度 AI 人脸识别 SDK |

### 5. 配置百度 AI 人脸识别

编辑 `start.py`，修改以下配置项（约第 17-19 行）：

```python
APP_ID = '你的APP_ID'
API_KEY = '你的API_KEY'
SECRET_KEY = '你的SECRET_KEY'
```

> 需要在 [百度 AI 开放平台](https://console.bce.baidu.com/ai/#/ai/face/overview) 创建人脸识别应用获取以上信息。

### 6. 启动服务

#### 开发模式（直接运行 Flask）

```bash
python3 start.py
```

Flask 将在 `127.0.0.1:5050` 启动。

首次启动会自动创建 SQLite 数据库和默认管理员账号：

- 用户名：`admin`
- 密码：`admin123`

#### 生产模式（配合 Nginx）

参考 `face_nginx.conf`，复制到 Nginx 配置目录：

```bash
sudo cp face_nginx.conf /etc/nginx/sites-available/face
sudo ln -s /etc/nginx/sites-available/face /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default   # 可选，移除默认站点
```

**修改 SSL 证书路径**（`face_nginx.conf` 第 44-45 行）：

```nginx
ssl_certificate     /path/to/your/cert.pem;
ssl_certificate_key /path/to/your/key.pem;
```

> 浏览器人脸识别需要 HTTPS，可以使用 Let's Encrypt 或自签名证书。

重载 Nginx：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 7. 浏览器访问

| 入口 | 地址 | 说明 |
|------|------|------|
| 登录页 | `https://<服务器IP>/login` | 账号密码或人脸登录 |
| 注册页 | `https://<服务器IP>/register` | 需采集人脸 |
| 用户面板 | `https://<服务器IP>/user` | 申请和查看器材 |
| 管理面板 | `https://<服务器IP>/admin` | 审批申请、管理用户 |

> 开发模式直接访问 `http://127.0.0.1:5050` 即可。

## API 接口

### Web 端

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/login` | 登录 |
| GET/POST | `/register` | 注册（含人脸采集） |
| GET | `/user` | 用户面板 |
| GET | `/admin` | 管理员面板 |
| POST | `/apply` | 提交器材申请 |
| POST | `/approve/<id>` | 批准申请（管理员） |
| POST | `/reject/<id>` | 拒绝申请（管理员） |
| POST | `/cancel/<id>` | 取消申请（用户） |
| POST | `/api/face_login` | 人脸识别登录 |
| POST | `/api/face_register` | 人脸注册 |

### ESP32 / 硬件端

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/command?cmd=<器材名>` | 触发人脸验证（调用树莓派拍照） |
| POST | `/api/face_apply` | 提交 ESP32 的人脸识别结果并自动创建申请 |

### `/api/face_apply` 请求格式

```json
{
  "user_id": "1",
  "equipment_name": "显微镜",
  "quantity": "1",
  "score": 85.5,
  "device": "esp32",
  "timestamp": "2024-01-01 12:00:00"
}
```

## 目录结构

```
web/
├── start.py              # Flask 主程序（服务端入口）
├── face.py               # 树莓派人脸验证脚本（独立运行）
├── face_nginx.conf       # Nginx 反向代理配置
├── cert.pem              # SSL 证书
├── key.pem               # SSL 私钥
├── equipment_system.db   # SQLite 数据库（自动生成）
├── templates/
│   ├── login.html        # 登录页
│   ├── register.html     # 注册页
│   ├── user.html         # 用户面板
│   └── admin.html        # 管理员面板
└── static/
    └── uploads/          # 上传的器材照片
```

## 人脸验证流程（树莓派）

`face.py` 用于树莓派端，通过 `/command` 接口触发：

1. ESP32 发送 `GET /command?cmd=显微镜`
2. Flask 启动 `face.py` 子进程
3. 树莓派摄像头拍照
4. 调用百度 AI 人脸搜索识别身份
5. 识别成功后自动创建器材申请

## 默认账号

| 角色 | 用户名 | 密码 |
|------|--------|------|
| 管理员 | `admin` | `admin123` |

> 生产环境请务必修改默认密码。
