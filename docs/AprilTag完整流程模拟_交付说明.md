# AprilTag 完整流程模拟 · 交付说明

更新日期：2026-11-04
对应任务清单：docs/AprilTag完整流程模拟_任务清单.md

## 0. 本轮范围

- 已完成：电脑端交互程序、网站设备接口、MCP 新工具、硬件定位状态化改造、自动化测试与文档。
- 已部署并实测：2026-11-06 在局域网内定位到树莓派 192.168.0.100（SSH 用户 xiao），
  备份后上传 7 个文件，重启 parts-stack.service，网站与 MCP 均正常。
- 实测结论：库存读取、SSH/MCP 握手、工具调用、转人脸位、人脸重试、云台返回全部正常；
  人脸识别已按旧站点流程改为持续重试（默认 30 秒），实测第 2 次识别通过（admin，78.2 分）；
  随后寻找标签 11 时连续 30 帧未检测到目标（云台当前朝向看不到该柜），返回 tag_not_found。
  本次仍未新增借用记录。
- 树莓派网站使用自签名 HTTPS（https://192.168.0.100，443 端口）；电脑端已配置
  verify_tls=false。

## 1. 交付清单

| 文件 | 类型 | 作用 |
|---|---|---|
| apriltag_sim/ | 新增 | 电脑端 Tkinter 程序（库存窗口、标签选择、MCP 调用、进度与结果） |
| apriltag_sim/README.md | 新增 | 使用手册：配置、操作、数据来源、故障排查 |
| apriltag_sim/run.bat | 新增 | Windows 启动入口 |
| apriltag_sim/tests/ | 新增 | 18 项离线单元测试 |
| unified_web_app/apriltag_test.py | 新增 | 硬件测试任务注册表（状态、阶段、取消、超时） |
| unified_web_app/tag_align.py | 新增 | 带明确结果的 AprilTag 对准（aligned/lost/timeout/cancelled/error） |
| unified_web_app/parts.py | 修改 | 设备库存接口、标签测试启动/状态/取消接口 |
| unified_web_app/hardware.py | 修改 | 分阶段拍照、状态化指位、退出清理 |
| unified_web_app/app.py | 修改 | PARTS_TAG_TEST_ENABLED 配置 |
| unified_web_app/security.py | 修改 | 新设备接口加入 CSRF 豁免名单（仍强制令牌） |
| unified_web_app/.env.example | 修改 | 新增开关说明 |
| unified_web_app/tests/test_device_apriltag.py | 新增 | 8 项接口测试（含“不产生借用记录”） |
| parts_mcp_server/parts_mcp.py | 修改 | 新增 test_face_and_point 工具（轮询、进度、超时、取消） |
| parts_mcp_server/.env.example | 修改 | 说明测试工具的时限参数 |
| parts_mcp_server/tests/test_tag_test_tool.py | 新增 | MCP 工具端到端测试（本地 HTTP 桩） |
| patches/agent_yuntai/panTilt.py | 新增（可选） | 让 lookAt 直接返回明确结果的替换文件，未部署 |

## 2. 网站新增接口

全部使用设备令牌 X-Device-Token（或 Authorization: Bearer），未配置令牌时返回 503，
令牌不符返回 401。标签测试两类写接口已加入 security.py 的设备接口白名单，只跳过
CSRF，不跳过令牌校验。

### GET /parts/api/device/cabinets

返回全部柜子与柜内全部零件（不是关键词搜索），字段：

    { "success": true, "source": "raspberry-pi", "generated_at": "...",
      "cabinet_count": 5, "component_count": 11,
      "cabinets": [ { "id": 1, "cabinet_number": "11", "equipment_name": "零件库存",
                      "description": "...", "updated_at": "...",
                      "components": [ { "id": 3, "component_name": "AO3401A",
                                        "quantity": 20, "unit": "个", "notes": "箱号 A1" } ] } ] }

### POST /parts/api/device/tag-test

请求：{ "tag_id": 11, "request_id": "abc123", "timeout_seconds": 45 }

- tag_id 必须是 0–586（tag36h11），否则 400。
- request_id 只允许字母、数字、- 和 _；重复的 request_id 不会重跑硬件，直接返回既有任务。
- timeout_seconds 为定位时限，范围 10–180，默认 45。
- 立即返回 202 与任务快照；硬件在后台线程执行。

### GET /parts/api/device/tag-test/<request_id>

返回任务快照：state、stage、stages[{name,label,status,message,elapsed}]、result、
error_code、username、face_score、cancel_requested、terminal。

state 取值：running / done / failed / cancelled / timeout。
result.status 取值：aligned / lost / timeout / cancelled / face_failed / hardware_unavailable / error。

### POST /parts/api/device/tag-test/<request_id>/cancel

设置取消标记。对准循环在每个控制周期检查，退出时关闭指示灯、释放相机并停止电机。
任务还有最终时限看门狗，避免客户端断开后电机继续运行。

### 安全与数据

- 不创建借用记录、不修改库存（测试断言 PartApplication 计数为 0）。
- 不返回令牌、密码、百度密钥或人脸图片。
- PARTS_TAG_TEST_ENABLED=false 可整体关闭测试接口。

## 3. MCP 新工具

    test_face_and_point(tag_id: int, request_id: str = "", timeout_seconds: float = 45) -> dict

- 调用网站启动任务，再按 0.5 秒间隔轮询状态，把阶段变化通过 progress 通知转发给客户端。
- 等待上限 = timeout_seconds + 90 秒；超过后请求网站取消并返回 timeout。
- 返回：success、state、status、tag_id、username、face_score、stages、message、
  borrow_record_created=false。
- 原有 5 个工具保持不变；借用工具仍只登记借用，不能替代本工具。

## 4. 硬件层

- hardware.capture_face_at_position(pan, tilt, on_stage=None)：转向人脸位、拍照、返回原位，
  并上报 face_position / capture / return_position 阶段；原有调用不受影响。
- hardware.point_to_cabinet_status(tag_id, delay, timeout, cancel_event, on_stage)：
  在锁内完成相机初始化、指示灯、对准、保持、清理；任何退出路径都关闭指示灯并释放相机。
- tag_align.align_to_tag()：沿用原调校参数（PID 10/1/0.8、焦距 947、标签 5 cm、
  0.3° 容差、30 帧丢失上限），新增 deadline、取消事件和明确结果。
- 旧的 point_to_cabinet() 保留给借用流程，行为不变；motorDrive.py 未改动。
- patches/agent_yuntai/panTilt.py 是“让 lookAt 本身返回结果”的可选替换文件，
  未部署、未验证；部署前必须与线上调校版本逐行比对。

## 5. 部署步骤

### 5.1 本次已执行记录（2026-11-06 21:34）

- 目标：xiao@192.168.0.100（树莓派 5，Debian 13，Python 3.13.5）
- 备份：/home/xiao/face/backups/apriltag-20261106-213418/
  （parts.py、hardware.py、app.py、security.py、parts_mcp.py 原文件）
- 上传：parts.py、hardware.py、app.py、security.py、apriltag_test.py、tag_align.py、parts_mcp.py
- 重启：systemctl --user restart parts-stack.service（gunicorn + xiaozhi_bridge + parts_mcp）
- 部署端自测：unified_web_app 8 项、parts_mcp_server 2 项，全部通过
- 接口自检：/health 200；设备库存接口返回 9 个柜子 12 条零件；硬件状态 face/camera/pan_tilt 就绪

### 5.2 通用步骤（供以后重复部署）

1. 备份（示例，时间戳按实际替换）：

       cd ~/face
       ts=$(date +%Y%m%d-%H%M)
       cp -a unified_web_app/parts.py        unified_web_app/parts.py.bak-$ts
       cp -a unified_web_app/hardware.py     unified_web_app/hardware.py.bak-$ts
       cp -a unified_web_app/app.py          unified_web_app/app.py.bak-$ts
       cp -a unified_web_app/security.py     unified_web_app/security.py.bak-$ts
       cp -a parts_mcp_server/parts_mcp.py   parts_mcp_server/parts_mcp.py.bak-$ts
       echo "$ts" > /tmp/apriltag-deploy-$ts.txt

2. 同步本次新增与修改的文件（scp 或 git pull 后 checkout 指定文件）。
3. 配置：确认 unified_web_app/.env 与 parts_mcp_server/.env 使用同一个
   PARTS_DEVICE_TOKEN；需要时设置 PARTS_TAG_TEST_ENABLED=true。
4. 重启（systemd 用户服务，单元见 deploy/parts-stack.service）：

       systemctl --user restart parts-stack.service
       systemctl --user status parts-stack.service --no-pager

   若使用 start_all.sh 手动运行，则停止旧进程后重新执行 start_all.sh。

5. 自检：

       TOKEN=...; HOST=http://127.0.0.1:5000
       curl -s -H "X-Device-Token: $TOKEN" $HOST/parts/api/device/cabinets | head -c 400
       curl -s -H "X-Device-Token: $TOKEN" -H "Content-Type: application/json" \
            -d '{"tag_id":11,"request_id":"deploycheck1","timeout_seconds":45}' \
            $HOST/parts/api/device/tag-test

6. 电脑端：运行 apriltag_sim，确认库存来源、标签映射，再执行一次测试并观察逐阶段结果。

## 6. 回退步骤

1. 恢复对应 .bak 文件（parts.py、hardware.py、app.py、security.py、parts_mcp.py）。
2. 删除新增文件 apriltag_test.py、tag_align.py（可选；删除前确认没有其他引用）。
3. 重启 parts-stack 服务。
4. 用 /health 与 /parts/api/hardware/status 确认网站恢复；确认设备借用流程仍可用。

回退后电脑端仍可浏览库存，但“执行测试”会因缺少 test_face_and_point 或接口而报错，
这是预期结果，不会产生借用记录。

## 7. 验证记录

| 项目 | 方式 | 结果 |
|---|---|---|
| 电脑端标签映射 | 18 项单元测试中的 7 项 | 通过 |
| 线上库存解析 / 令牌失败 / 连接失败 | 本地 HTTP 桩 | 通过 |
| 本地回退（只读、旧库识别、空柜） | 临时 SQLite | 通过 |
| MCP 握手、工具检查、进度、取消 | 进程内假 MCP 服务 | 通过 |
| 网站接口鉴权、tag 校验、阶段、取消、幂等 | Flask 测试客户端 + 模拟硬件 | 通过 |
| MCP 工具端到端（注册、轮询、结果映射） | FastMCP 内存客户端 + 本地 HTTP 桩 | 通过 |
| 不产生借用记录 | 断言 PartApplication 计数 | 通过 |
| 编译检查 | py_compile 全部改动文件 | 通过 |
| 真实 SSH / plink | 已执行 | 192.168.0.100 登录、MCP stdio 会话正常 |
| 线上设备库存接口 | 已执行 | 9 个柜子 / 12 条零件 |
| 转人脸位 / 拍照 / 云台返回 | 已执行 | 3.0 s / 1.5 s / 0.3 s |
| 人脸持续重试 | 已执行 | 按旧参数 30 秒内重试 10 次；最近一次因镜头前无人返回 pic not has face |
| 相机释放 | 已核对 | 循环/对准结束后无进程占用，另起进程 0.57 秒可重新打开 |
| 实机取消 | 已执行 | 人脸循环中取消：客户端等待服务端确认，任务 3 秒内转为 cancelled，云台返回 ok，电机由紧急停止兜底 |
| 借用记录 | 已核对 | 取消前后均为 28 条，最新 2026-10-19，未新增 |
| 对准标签、激光指示 | 未完成 | 人脸通过后开始搜索，但当前视野看不到目标柜，8 秒后报 tag_not_found |
| 项目原有测试套件 | 受当前 Windows 沙箱临时目录权限限制，未能完整执行 | 需在普通环境复跑 |

本轮新增自动化测试共 38 项（电脑端 22 + 网站 14 + MCP 工具 2），全部通过。

## 8. 与任务清单的对照

| 任务清单章节 | 状态 | 说明 |
|---|---|---|
| 一、数据获取与回退 | 完成 | 线上全量接口、只读本地回退、来源标注、多候选选择、失败弹窗 |
| 二、输入前的库存弹窗 | 完成 | 全量分组、空柜、字段、详情、重复与非数字提示、执行前确认 |
| 三、MCP 模拟调用 | 完成 | 独立工具、SSH stdio、握手顺序、超时、错误解析、进度通知 |
| 四、硬件完整流程 | 代码完成，未实机验证 | 人脸失败即停止；视野内定位；取消/超时/清理已实现 |
| 五、进度与结果窗口 | 完成 | 后台线程、阶段列表、取消、结果字段、禁止重复提交 |
| 六、验证、备份与交付 | 部分完成 | 已部署并保留 5 次备份；在线取消、相机释放已验证；成功对准标签仍待补 |

## 9. 已知限制

1. 第一版只在当前相机视野内寻找标签；目标不在视野时返回 lost，不做大范围扫描。
2. tag_align 复制了 panTilt 的控制律而不是修改子模块；已对齐线上参数
   （PID 10/0.8/1、速度 ±150、焦距 947、标签 5 cm）。如果线上再次调校，需要同步常量。
3. 本地回退支持 SQLite 与 JSON 导出，不直接解析 Excel 库存表；Excel 需先导入数据库。
4. 电脑端的阶段显示依赖树莓派返回 progress 通知；旧版本 MCP 服务只会显示“等待硬件”
   与最终结果。
5. Plink 密码模式会把密码作为进程参数传给本机 plink；仅建议在可信电脑上临时使用，
   长期应改用密钥或 Pageant。
6. 线上 gunicorn 为单 worker（--workers 1），任务注册表在进程内；若以后改成多 worker，
   状态查询会落到不同进程，需要共享存储或粘性会话。
7. 百度人脸的质量控制为 NORMAL，姿态或遮挡会直接拒绝；实测两次均因遮挡失败，
   需要用户在摄像头正前方保持正脸后再测。
8. 电脑端默认校验 HTTPS 证书；树莓派是自签名证书，需在设置中关闭校验或填写 CA 文件。
9. 人脸阶段已按 web/start.py 逐项对齐：640×480 RGB888、0.5 秒预热、JPEG 抓拍、
   失败后 2 秒重试、30 秒超时、百度 search 不带 quality/liveness、匹配阈值 70。
   每次尝试单独 start/stop 相机，循环结束统一 close；参数和抓取节奏与原流程一致。
10. 标签定位仍是第一版的“视野内定位”：人脸通过后云台按原流程返回，再从当前朝向找标签；
    当前朝向看不到目标时如实返回 tag_not_found，不做未经机械行程验证的大范围扫描。
    实测时相机朝向桌面/人，柜子不在画面内，需要调整云台初始朝向或把目标柜移入视野。
11. 丢失判定由“30 帧”改为“连续 8 秒”（PARTS_TAG_LOST_TIMEOUT，2–30 秒可调）：
    1080p 下 30 帧只有约 2.3 秒，短于相机自动曝光稳定和云台余振时间；
    返回信息会写成“连续 X.X 秒（N 帧）未检测到标签”。

## 10. 审查问题修复记录（2026-11-06 23:20）

| 审查问题 | 处理 | 位置 |
|---|---|---|
| 取消测试不保证停止树莓派任务 | 客户端取消时除发 MCP notifications/cancelled 外，直接用设备令牌 POST 网站 cancel 端点，并等待任务进入终态；MCP 工具在 asyncio 取消时也 POST cancel；网站取消端点立即 emergency_stop | mcp_client.py、parts_mcp.py、parts.py |
| 最终超时只改状态 | 超时看门狗先调用 hardware.emergency_stop()（电机 0、指示灯关），再收尾状态；百度客户端加 5/8 秒连接与读超时；人脸重试的 2 秒间隔改为每 0.2 秒检查取消 | parts.py、hardware.py |
| PID 参数不一致 | tag_align 改为与线上 panTilt 一致：PID 10 / 0.8 / 1、速度 ±150、焦距 947、标签 5 cm | tag_align.py |
| 刷新后旧目标仍保留 | 每次加载/刷新库存都清空 target_tag/cabinet/source，并提示重新选择 | ui.py |
| 无库存时手动测试不可执行 | 手动输入后允许在 snapshot=None 时执行，确认框标注“未核实库存” | ui.py |
| 本地时间与来源不可靠 | 库存记录时间与文件修改时间分开显示；空库直接判为不可用；多候选仍由用户选择 | inventory.py、ui.py |
| 主窗口关闭按钮未绑定 | 绑定 WM_DELETE_WINDOW → on_close（测试中会先取消并停止硬件） | ui.py |

新增测试：tests/test_hardware_emergency.py（紧急停止）、tests/test_tag_align.py（丢失时间窗）、
test_device_apriltag.py 增加取消紧急停止断言、电脑端增加空库与时间字段测试。

实机取消验证（2026-11-06 23:10）：人脸循环中取消，客户端等待服务端终态；
网站任务在约 3 秒内变为 cancelled，阶段显示“人脸识别 cancelled / 云台返回 ok”，
借用记录保持 28 条不变，相机随后 0.78 秒可重新打开。

仍未完成：一次“人脸通过 + 目标标签实际对准”的成功验收。当前云台朝向看不到目标柜，
需要先调整朝向，或在确认机械行程后增加有限角度扫描。
