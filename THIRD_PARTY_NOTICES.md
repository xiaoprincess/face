# 第三方项目与授权范围

## 本项目的 MIT 授权

根目录 [LICENSE](LICENSE) 对本项目贡献者有权授权的原创软件代码和配套技术文档提供 MIT 授权。保留来源、原作者版权声明和许可文本后，可以使用、修改和再分发相应内容。

第三方代码、Git 子模块、依赖包、字体及其他外部素材继续遵循各自许可证。根目录的 MIT 文本不会替换它们的授权，也不授予外部服务账号或云服务额度。人物照片、人脸数据、用户上传资料及比赛标识不属于本项目软件 MIT 授权的范围。

## 上游项目

| 项目 | 在系统中的作用 | 来源与许可说明 |
|---|---|---|
| 小智 ESP32 固件 | ESP32 语音终端，与小智服务通信 | [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)，固件单独获取，不包含在本仓库。其上游 [LICENSE](https://github.com/78/xiaozhi-esp32/blob/main/LICENSE) 为 MIT；分发固件或其修改版本时保留对应版本的原始版权和许可文本 |
| 小智 MCP 示例 | 本地 stdio 服务经 WebSocket 接入小智的架构参考 | [78/mcp-calculator](https://github.com/78/mcp-calculator)。参考其接入方式不表示获得对所有示例代码重新授权的权利；本项目不宣称其为团队原创或替上游指定许可证 |
| agent_yuntai | 树莓派云台和电机控制 | [Gitee 源仓库](https://gitee.com/senlinjun/agent_yuntai.git)，以 Git 子模块引用，路径 `web/agent_yuntai`。具体版本由父仓库 gitlink 固定。使用、修改或再分发前查看该版本的许可和作者说明；本仓库不将它重新授权为 MIT |

当前整理时，本地未检出云台子模块内容，因此没有核实该版本的许可证。不要仅凭本仓库根目录 LICENSE 判断该子模块可自由再分发。

## 软件依赖和外部服务

运行依赖分别列于 [网站 requirements.txt](unified_web_app/requirements.txt) 与 [MCP requirements.txt](parts_mcp_server/requirements.txt)。Flask 及其扩展、SQLAlchemy、Werkzeug、FastMCP、websockets、python-dotenv、openpyxl、baidu-aip、Gunicorn、apriltag-python、simple-pid，以及树莓派环境安装的 Picamera2 等组件，都保留其原作者和许可证。具体安装版本及传递依赖以实际环境为准；打包再分发时同时保留所需的第三方许可文件。

百度人脸识别和小智云端属于外部服务。使用者自行配置账号、凭据、网络和额度，并遵循相应服务条款。软件 MIT 授权不代表这些服务永久免费或允许公开个人数据。

## 项目贡献边界

团队在已有开源固件、MCP 协议及第三方识别能力基础上，开发零件查询与借用工具、网站与共享库存、用户身份关联、请求去重及软硬件联调流程。介绍和展示项目时，请同时说明这些本项目贡献和上游基础。
