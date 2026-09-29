# 零件管家 MCP 服务

此服务使用小智官方示例同样的模式：本地 `stdio` MCP 工具通过 WebSocket 桥接到小智 MCP 端点。ESP32 不直接访问 SQLite，所有库存查询都由小智服务转发到这里。

## 数据来源

默认数据库是 `../unified_web_app/instance/unified.db`，与小智零件社区网站共用。Excel 库存表中：

- 柜号写入网站的柜号；
- 箱号写入零件的“箱号/备注”；
- 器材名称和剩余库存数量写入零件记录。

因此网站后台的修改会立即被 MCP 查询到，MCP 不维护第二份库存副本。

## 当前电脑运行

1. 确认已启动过一次 `../unified_web_app/start.bat`，使数据库初始化。
2. 运行 `install.bat`。
3. 首次导入库存：

   ```bat
   .venv\Scripts\python.exe sync_inventory.py "C:\Users\lenovo\Downloads\零件管家器材库存清单.xlsx"
   ```

4. 将小智 MCP WebSocket 地址保存到 `.env` 的 `XIAOZHI_MCP_ENDPOINT`。不要把 `.env` 发给别人或提交到 Git。
5. 运行 `start_mcp.bat`。窗口保持打开表示服务在线。

如果网站和 MCP 都已安装好，也可以直接运行上一级目录的 `start_all.bat`，一次启动两项服务并进入 MCP 实时监控台。监控台只记录连接状态、MCP 方法名、工具名和成功/失败，不记录密钥、调用参数或库存结果。按 `Q` 会同时停止网站、MCP 服务和监控；直接关闭监控窗口或按 `Ctrl+C` 不会主动停止后台服务。Windows 重启后只需运行这一个文件。

## 可调用工具

- `search_part`：按器材名称、型号或柜号查找位置、箱号和库存。
- `get_cabinet_parts`：列出某个柜号中的全部器材。
- `get_low_stock`：列出低库存器材。
- `get_inventory_overview`：获取柜子、器材记录和库存总数。
- `request_part_borrow`：仅在用户明确借用且数量明确时使用；由树莓派控制云台与相机进行人脸验证，并直接登记借用记录。

### 模拟小智客户端

`simulate_xiaozhi_borrow.py` 会启动本地 MCP stdio 服务，按小智工具调用顺序先搜索“锂电池”，再准备借用 1 个的参数。默认是演练模式，不会启动树莓派摄像头，也不会写入借用记录：

```bash
python simulate_xiaozhi_borrow.py
```

确认要实际触发树莓派人脸验证并登记借用后，再执行：

```bash
python simulate_xiaozhi_borrow.py --execute
```

如果从 Windows 电脑发起，但目标是树莓派上的 MCP，脚本已经写入默认的树莓派地址、用户名和主机指纹。电脑上可以直接通过 Plink 远程执行树莓派本地 MCP：

```powershell
python simulate_xiaozhi_borrow.py --remote --execute
```

远程模式不会启动电脑上的 MCP 子进程，而是在树莓派上运行 `parts_mcp_server/.venv/bin/python`。SSH 密码由 Plink 在终端中询问，不写入脚本或环境变量。
默认等待上限为 300 秒；需要调整时可使用 `--remote-timeout 秒数`。如果树莓派虚拟环境不在默认位置，可通过 `PARTS_REMOTE_PYTHON` 指定绝对路径。

脚本会要求库存中“锂电池”匹配到唯一零件；如果有多个型号，会停止并列出候选项，不会自行猜测。实际执行前请确认树莓派网站、MCP 配置、`PARTS_DEVICE_TOKEN`、摄像头和百度人脸服务均正常。

前四个工具只读。`request_part_borrow` 不会自动扣减库存，也不会创建待审批申请；人脸验证通过后，网站会直接登记借用记录。成功结果以接口返回为准，通常为 `message`“借用已登记”、`status` 为 `recorded`，并返回 `record_id`；为兼容旧调用方，仍可能同时返回 `application_id`。

## 借用登记配置

在 `parts_mcp_server/.env` 与 `unified_web_app/.env` 中设置相同的 `PARTS_DEVICE_TOKEN`，并在 MCP 的 `.env` 中设置 `PARTS_WEB_BASE_URL`（树莓派本机通常为 `http://127.0.0.1:5050`）。`unified_web_app/.env.example` 列出了网站需要的百度人脸配置。借用接口在没有 Token 时会拒绝执行，不会降级为匿名调用。不要把 Token 写进 ESP32 固件、提示词或 Git。

小智云端提示词的可直接追加内容见 [xiaozhi_prompt_borrow_extension.md](xiaozhi_prompt_borrow_extension.md)。新增工具后重启 MCP bridge，小智重新连接时会读取工具列表。

## 迁移到树莓派

复制 `parts_mcp_server` 和 `unified_web_app/instance/unified.db` 到树莓派的同级目录，配置 `.env`，运行 `chmod +x install.sh start_mcp.sh`，再执行 `./install.sh` 与 `./start_mcp.sh`。如果数据库路径不同，在 `.env` 设置 `PARTS_DB_PATH` 为数据库的绝对路径。

两项服务安装完成后，也可以在 `face` 目录运行 `chmod +x start_all.sh && ./start_all.sh`，一次启动网站和 MCP。
