# 安装与使用

以下命令从本仓库根目录 `face/` 执行。网站可先在普通电脑运行；MCP 连接需要自己的小智端点，实机人脸和指柜还需要树莓派硬件。

## 获取代码

```bash
git clone --branch main https://github.com/xiaoprincess/face.git
cd face
```

当前发布版本位于 `main` 分支。只有需要实机云台时才执行 `git submodule update --init --recursive`，并先核对 [第三方说明](../THIRD_PARTY_NOTICES.md)。固件单独从 [小智 ESP32 项目](https://github.com/78/xiaozhi-esp32) 获取。

## Windows 网站

确保命令行可以运行 Python 3.11 或更新版本。依赖能否安装仍取决于具体 Python 版本和平台。

```powershell
.\unified_web_app\install.bat
Copy-Item .\unified_web_app\.env.example .\unified_web_app\.env
.\unified_web_app\start.bat
```

复制配置仅在首次安装且 `.env` 尚不存在时执行。浏览器访问 `http://127.0.0.1:5050`。网站自动初始化 `unified_web_app/instance/unified.db`，首个注册账号成为管理员；没有预设的公开管理员密码。管理员可在库存管理页新建柜子和录入零件。

只使用账号密码和网页库存功能，无需百度凭据或树莓派摄像头。浏览器人脸注册、登录使用访问网站这台电脑的摄像头，并要求配置百度人脸服务。浏览器摄像头通常需要 HTTPS，回环地址是例外。

## 接入小智 MCP

```powershell
.\parts_mcp_server\install.bat
Copy-Item .\parts_mcp_server\.env.example .\parts_mcp_server\.env
```

编辑两个 `.env` 文件：

| 配置位置 | 字段 | 内容 |
|---|---|---|
| MCP | `XIAOZHI_MCP_ENDPOINT` | 从自己的小智控制台取得完整 MCP WebSocket 端点 |
| MCP | `PARTS_WEB_BASE_URL` | 本机网站通常为 `http://127.0.0.1:5050` |
| 网站与 MCP | `PARTS_DEVICE_TOKEN` | 同一个自行生成的随机值；MCP 示例中需取消该行注释 |
| 网站 | `BAIDU_APP_ID`、`BAIDU_API_KEY`、`BAIDU_SECRET_KEY`、`BAIDU_GROUP_ID` | 人脸识别服务所需配置 |

在网站页面中配置和核对硬件参数。网站与 MCP 默认共用同一数据库；跨目录部署时通过 MCP 的 `PARTS_DB_PATH` 指定同一数据库的绝对路径。

配置完成后，关闭单独启动的网站，再联合启动：

```powershell
.\start_all.bat
```

启动后可从小智询问“查询 AO3401A 的库存和位置”。查询不触发摄像头或电机。只有明确零件和数量并确认借用后，才进入借用接口、人脸核验、记录写入和柜体指引流程。

硬件缺失默认返回原因。只有显式设置网站的 `PARTS_SKIP_FACE_VERIFICATION=true`，才跳过人脸、摄像头和云台，按 `PARTS_FALLBACK_APPLICANT_EMAIL` 对应的现有账号登记，并标注未核验。它不会自动创建该账号。恢复硬件流程时设回 `false`。修改配置后重启服务。

如需从自己的 Excel 表导入库存，可从 `parts_mcp_server/` 目录运行：

```powershell
.\.venv\Scripts\python.exe sync_inventory.py "你的库存清单.xlsx"
```

导入会写入库存，已有数据先备份。真实库存和人脸资料不随仓库分发。

## 树莓派

安装适合设备的 Python 3、venv，以及摄像头、GPIO 所需系统依赖，再执行：

```bash
sh unified_web_app/install.sh
sh parts_mcp_server/install.sh
cp unified_web_app/.env.example unified_web_app/.env
cp parts_mcp_server/.env.example parts_mcp_server/.env
# 编辑两个 .env，使用自己的配置
bash start_all.sh
```

网站内部绑定 `127.0.0.1:5050`。需要其他电脑访问时，根据 [Nginx 示例](../unified_web_app/nginx_unified.conf.example) 配置反向代理、自己的域名和 HTTPS 证书。`install.sh` 安装 Python 依赖，不负责安装 Nginx、签发证书或配置实机驱动。

[systemd 用户服务模板](../deploy/parts-stack.service) 默认代码位于 `~/face`，执行前需为 `start_all.sh` 添加执行权限，并按自己的部署路径调整服务。实机驱动沿用 `web/agent_yuntai`，不要随意改动已校准的 `motorDrive.py`。

## 验证与测试

网站健康检查为 `GET /health`。先通过网站确认库存，再进行 MCP 只读查询。模拟脚本默认演练模式：

```powershell
cd parts_mcp_server
.\.venv\Scripts\python.exe simulate_xiaozhi_borrow.py
```

加入 `--execute` 会实际登记借用并可能驱动硬件，具体参数见 [MCP 文档](../parts_mcp_server/README.md)。

开发回归测试分别在以下目录执行：

```powershell
cd unified_web_app
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -t . -q
cd ..\parts_mcp_server
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -t . -q
```

明确零件编号的新借用会原子扣减库存、写入流水，归还恢复库存。历史与自由文本记录不自动改库存；重复请求不会重复扣减。指引状态独立于借用状态，定位失败后记录仍保留。软件测试不能替代摄像头、云台和激光的实机验收。
