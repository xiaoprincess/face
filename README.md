# 零件智引 智能零件管家

[![License: MIT](https://img.shields.io/badge/License-MIT-7c61b5.svg)](LICENSE)

让校园实验室和创客空间的零件更容易查找，让借用留下记录，让共享器材更容易管理。

**主分支：[`main`](https://github.com/xiaoprincess/face/tree/main)。** 网站、MCP 与开源许可均随主分支发布。

[安装与使用](docs/QUICKSTART.md) · [网站功能与开发规范](unified_web_app/README.md) · [MCP 工具与模拟客户端](parts_mcp_server/README.md) · [第三方来源](THIRD_PARTY_NOTICES.md) · [MIT 许可证](LICENSE)

本仓库提供网站、MCP 服务、部署脚本和硬件集成接口。ESP32 固件单独获取，真实库存、账户和服务凭据由使用者配置。

面向科创室 / 实验室的零件管理与借用系统，由**统一网站 + 小智 AI MCP 服务 + ESP32 语音终端 + 树莓派硬件联动**组成。

- 统一网站 `unified_web_app/`：Flask + SQLite + Socket.IO，负责用户、库存、借用记录、博客、私聊和云盘。
- MCP 服务 `parts_mcp_server/`：FastMCP 工具，把小智 AI 接到真实库存和借用接口。
- ESP32 终端：运行单独获取的小智 AI 固件，负责语音入口。
- 树莓派硬件：摄像头、百度 AI 人脸识别、云台和激光指柜。硬件缺失时返回原因，维护人员可显式配置临时降级。

> **流程要点**：借用**不需要审批**。用户（或小智语音）确认零件与数量后，系统**直接登记一条“已登记”的借用记录**，管理员后台只查看记录和管理库存，没有“批准 / 拒绝”操作。

选择明确零件编号的新借用在同一事务中扣减可用库存并写入流水，归还时恢复库存；旧记录和自由文本登记不回溯扣减。统计报表仍待完善。查询主要按名称、型号和柜号匹配，相似零件需要继续确认；实体定位以柜体为目标，不保证精确到箱格。借用记录写入和后续云台指引分别反馈，指柜失败不代表记录没有保存。

web端可部署在Windows端上，但如需完全运行所有功能建议使用树莓派进行部署。

## 涉及技术

| 技术 | 用途 |
|---|---|
| Python / Flask / Flask-SQLAlchemy | 网站后端、权限、库存与借用接口 |
| SQLite | 统一数据库（用户、柜子、零件、借用记录、社区数据） |
| Jinja2 + 原生 CSS/JS | 页面与彩色积木风格（bento） |
| Flask-SocketIO | 私聊与实时通知 |
| FastMCP（stdio） | 小智 AI 的 MCP 工具服务 |
| 百度 AI 人脸识别（`baidu-aip`） | 人脸注册与 1:N 搜索 |
| Nginx | 反向代理、HTTP 5000 / HTTPS 入口 |
| Picamera2 / CSI 摄像头 | 人脸采集与柜子定位 |
| AprilTag + GPIO / 步进电机 | 云台转动与激光指向 |

## 系统架构

```text
ESP32 小智 AI
  → 小智云端 MCP Endpoint
  → 树莓派 MCP WebSocket Bridge（parts_mcp_server/xiaozhi_bridge.py）
  → 本地 stdio MCP Server（parts_mcp_server/parts_mcp.py）
  → 统一网站内部接口（POST /parts/api/device/borrow）
  → unified_web_app/instance/unified.db

浏览器 ── HTTPS/HTTP ──▶ Nginx ──▶ Flask(:5050) ──▶ SQLite
树莓派（摄像头 + 云台）◀── Flask 调用 ──▶ 百度 AI 人脸 API
```

MCP 的库存查询通过 `inventory_store.py` 直接读取共享 SQLite 数据库；借用登记通过网站接口写入，并使用设备令牌 `PARTS_DEVICE_TOKEN` 认证。库存导入脚本另有写库能力，不属于语音查询工具。

## AprilTag 硬件测试（2026-11）

`apriltag_sim/` 是电脑端联调程序：先浏览库存并选择 AprilTag，再通过 SSH 让小智 MCP 工具
`test_face_and_point` 在树莓派上完成"转向人脸 → 人脸识别 → 对准标签"，逐阶段返回结果。
它不会创建借用记录，也不会改动库存。网站侧对应接口为 `/parts/api/device/cabinets` 与
`/parts/api/device/tag-test`（设备令牌认证）。使用说明见 [apriltag_sim/README.md](apriltag_sim/README.md)，
部署与回退步骤见 [AprilTag 交付说明](docs/AprilTag完整流程模拟_交付说明.md)。

## 使用流程

### 一、注册与登录

```text
浏览器打开网站 → 注册（用户名、邮箱、密码与确认密码）
  → 首个注册账号成为管理员，后续注册账号为普通用户
  → 账号密码登录；人脸注册与登录使用浏览器所在电脑的摄像头及已配置的百度服务
```

> 浏览器调用摄像头必须使用 HTTPS（本地 `localhost` 例外）。

### 二、查询零件

```text
零件管家页 → 搜索名称 / 型号 / 柜号 / 箱号
  → 显示柜号、箱号、剩余数量和备注
```

### 三、登记借用（无需审批）

```text
选择零件和数量 → 填写用途（可上传照片）→ 提交
  → 立即生成一条“已登记”借用记录
  → 在「借用记录」页查看自己的记录
```

### 四、小智语音借用

```text
用户对小智说“我要借两个 AO3401A”
  → 小智调用 search_part 确认唯一零件
  → 调用 request_part_borrow(component_id, quantity)
  → 云台转向人脸 → 拍照 → 百度 AI 人脸验证
  → 验证通过后直接登记借用记录
  → 云台指向对应零件柜
```

摄像头不可用时可设置 `PARTS_SKIP_FACE_VERIFICATION=true` 进入临时降级模式：跳过人脸、摄像头和云台动作，仍然检查零件与库存后正常登记记录，并在结果中标注“未进行人脸识别”。摄像头上线后改回 `false`。

### 五、管理员后台

后台分为三个页签，默认进入「库存管理」：

| 页签 | 作用 |
|---|---|
| 借用记录 | 查看全部借用记录与历史记录 |
| 库存管理 | 新建/编辑/删除柜子，增删改柜内零件与数量，低库存筛选 |
| 新增柜子 | 创建柜子并一次性录入多个零件条目 |

管理员还可以维护系统/硬件设置、管理博客内容与用户。

## MCP 工具

| 工具 | 类型 | 作用 |
|---|---|---|
| `search_part` | 只读 | 按名称、型号、柜号查库存、柜号和箱号 |
| `get_cabinet_parts` | 只读 | 查询指定柜子的全部零件 |
| `get_low_stock` | 只读 | 查询低于阈值的零件 |
| `get_inventory_overview` | 只读 | 柜子数量、零件条目和库存概览 |
| `request_part_borrow` | 写入 | 用户明确借用且零件唯一、数量明确时登记借用记录 |

规则：只读查询绝不启动摄像头或云台；成功与否以工具返回为准，禁止编造库存、柜号或记录编号；`request_id` 作幂等键，避免语音重复登记。

## 快速部署

### 1. Windows 本地

建议使用 Python 3.11 或更新版本，并分别安装网站和 MCP 虚拟环境。完整实机流程需要树莓派，HTTPS 可通过 Nginx 提供。

```powershell
# 从本仓库 face 目录执行，安装两个虚拟环境
.\unified_web_app\install.bat
.\parts_mcp_server\install.bat

# 配置：复制 .env.example 为 .env 并填写
#   unified_web_app\.env   PARTS_DEVICE_TOKEN / BAIDU_* / PARTS_SKIP_FACE_VERIFICATION
#   parts_mcp_server\.env  XIAOZHI_MCP_ENDPOINT / PARTS_WEB_BASE_URL / PARTS_DEVICE_TOKEN

# 一起启动 / 停止
.\start_all.bat       # 网站 http://127.0.0.1:5050 + MCP
.\stop_all.ps1
```

`unified_web_app\.env` 与 `parts_mcp_server\.env` 中的 `PARTS_DEVICE_TOKEN` 必须是同一个随机值；没有令牌时借用接口会拒绝执行，不会降级为匿名调用。

### 2. 树莓派

- 网站内部监听 `127.0.0.1:5050`，Nginx 对外提供 5000 / 443（示例：`unified_web_app/nginx_unified.conf.example`）。
- 网站与 MCP 可使用 [systemd 用户服务模板](deploy/parts-stack.service) 自动启动，具体步骤见 [安装文档](docs/QUICKSTART.md)。
- `web/agent_yuntai/` 是已调试完成的云台模块，当前网站沿用；`motorDrive.py` 禁止修改。

### 3. ESP32 固件

固件单独从 [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 获取。按照所选固件版本的说明安装 ESP-IDF，选择实际开发板、配置并烧录。串口号以本机检测结果为准，本仓库不包含完整固件或 ESP-IDF。

## 目录结构

```text
face/
├── unified_web_app/           # 当前统一网站（Flask）
│   ├── app.py                 # 应用工厂、蓝图注册
│   ├── parts.py / parts_models.py    # 库存、借用记录、设备借用接口
│   ├── community.py / community_models.py  # 博客、私聊、通知、云盘
│   ├── hardware.py            # 人脸、摄像头、云台调用与降级
│   ├── templates/ static/     # 页面与彩色积木样式
│   ├── instance/unified.db    # 统一数据库（运行生成）
│   └── README.md              # 网站需求与开发规范（重要）
├── parts_mcp_server/          # 小智 AI 的 MCP 服务
│   ├── parts_mcp.py           # MCP 工具定义
│   ├── inventory_store.py     # 库存只读查询
│   ├── xiaozhi_bridge.py      # WebSocket Bridge
│   ├── xiaozhi_prompt_borrow_extension.md  # 小智提示词（借用规则）
│   └── 零零喵_完整提示词.txt
├── web/                       # 旧版独立站点（保留 agent_yuntai 云台模块）
├── deploy/parts-stack.service # 树莓派 systemd 启动单元
├── start_all.ps1 / stop_all.ps1
└── equipment_system.db        # 旧版站点数据库
```

## 安全与协作规则

- 密码使用哈希保存；管理员接口必须在后端校验权限，前端隐藏按钮不算权限控制。
- 百度 AI 凭据、设备令牌、Flask 密钥只放 `.env`，不写进源码、提示词、ESP32 固件或 Git。
- 上传文件校验扩展名、MIME、大小，存放于私有目录，不暴露到公共静态路径。
- 借用接口必须带幂等键；只读查询不得触发硬件。
- **没有明确指令不要执行 `git push`**；不用 `git reset --hard` 覆盖未提交修改。
- 回归检查：分别在 `unified_web_app` 和 `parts_mcp_server` 目录使用对应虚拟环境执行 `python -B -m unittest discover -s tests -t . -q`。软件测试不替代实机验收。

## 开源许可与致谢

本项目贡献者有权授权的原创软件代码和配套技术文档采用 [MIT License](LICENSE)。第三方代码、子模块、依赖和外部素材保留各自原始授权，详见 [第三方来源与授权范围](THIRD_PARTY_NOTICES.md)。人物照片、人脸数据、私人上传资料和比赛标识不纳入软件 MIT 授权。

项目基于 [小智 ESP32](https://github.com/78/xiaozhi-esp32) 语音终端及相关开源生态，参考 [小智 MCP 示例](https://github.com/78/mcp-calculator) 的接入方式，并集成百度人脸服务和 `agent_yuntai` 云台模块。团队工作重点是零件查询与借用工具、共享库存网站、身份关联、去重和软硬件联调，不宣称底层模型、协议或识别算法为团队原创。

## 旧版站点（附录）

`web/` 是早期的独立「实验器材申请管理系统」（`start.py` + `templates/` + `equipment_system.db`），流程为“提交申请 → 管理员审批（`/apply`、`/approve/<id>`、`/reject/<id>`）”，现已由 `unified_web_app` 取代，只在以下场景保留：

- 其 `agent_yuntai/` 云台底层模块被当前网站调用；
- 作为历史数据与实现的参考。

旧站点与新网站**不要混用同一份数据库**；新流程请使用 `unified_web_app`。
