# 书序本机版：启动、停止与 MCP 接入

## 启动网页

双击项目根目录的 `start.cmd`。它会启动绑定在 `127.0.0.1` 的本机服务并打开网页。服务默认使用端口 `17831`；端口被其他程序占用且无法验证为书序实例时会报错，不会接管该程序。

关闭网页不会停止服务。双击 `stop.cmd` 可停止已核验的书序服务；再次打开网页或调用 MCP 时会按需启动。

## MCP 宿主接入

`config/mcp-registration.example.json` 是本机兼容 stdio MCP 注册示例。将其中的 `mcpServers.shuxu` 配置合并到所用 MCP 宿主的配置中，并按实际安装位置修改脚本绝对路径。它不含 bearer 凭据；本文件只是参考，尚未写入 CuteDuck 或其他宿主配置。

可从任意工作目录运行的完整命令：

```powershell
py -3.12 -X utf8 -B "F:\桌面\程序\shuxu\shuxu\mcp_server.py"
```

标准输入和输出只承载 MCP 协议；启动错误写到标准错误。MCP 首次连接会启动同一个本机 HTTP 服务，后续工具请求也会重新核验实例，因此服务停止后再次调用可重新启动。

MCP 服务会在初始化时把宿主指令交给 AI，并在握手中返回当前宿主可见的工具目录、协议版本和服务实例。外部宿主也可调用 `get_workflow_guide` 查看原有对话场景，或调用 `get_mcp_directory` 查询当前真实目录。若宿主不转发 MCP 初始化指令，将下面六份 TXT 按顺序交给 AI。当前身份能调用的能力仍以该宿主的真实工具目录为准。

- [识别意图与授权](../config/mcp-prompts/01-host-role.txt)
- [意图识别与工具路由](../config/mcp-prompts/02-tool-routing.txt)
- [资料、全文、来源与附件](../config/mcp-prompts/03-materials-and-full-text.txt)
- [更正与管理操作](../config/mcp-prompts/04-admin-operations.txt)
- [回执、错误与恢复](../config/mcp-prompts/05-timeouts-and-recovery.txt)
- [隔离宿主验收](../config/mcp-prompts/06-host-acceptance.txt)

如需由用户自行接入 CuteDuck 做 QQ 私聊验收，先阅读[隔离注册与自检](cuteduck/01-register-and-check.txt)，再阅读[同一私聊验收](cuteduck/02-acceptance-chat.txt)。这两份指令将测试项限定在独立端口、数据目录和运行目录；项目当前不会自动更改 CuteDuck 配置，也不会代替用户发送 QQ 消息。

规格 17 的 `shuxu.mcp25_host_acceptance` 是独立本机 CLI：它创建自己的临时数据和回环服务，不连接外部宿主；只有用户明确要求本机实现验收时才运行。规格 19 的目录比较通过 MCP 只读工具执行，不会启动服务或切换版本。规格 20 的维护工具需从已授权宿主显式调用，并按 [宿主指南](mcp24-host-guide.md) 查回操作状态；它与手动双击 `stop.cmd` 的普通关闭流程不同。

### 宿主调用示例

| 用户说的话 | 工具与条件 | 外部 AI 可使用的结果 |
| --- | --- | --- |
| 给我一题，五分钟，别碰工作 | `next_prompt`，传 `conversation_id`、`request_id`、`minutes: 5` 和 `exclude_directions` | 一条题、实际筛选依据和 `decision_id` |
| 先列出还能继续的题 | `list_unfinished_prompts`，传 `conversation_id`，按实际条件及 `next_offset` 分页 | 不会为翻页新增选题记录；结果含状态、最近活动和续接点 |
| 今晚轻一点，之后换题沿用 | 先读 `get_session_conditions`，按当前版本用 `set_session_conditions` 保存；再用 `next_prompt` 选题 | 条件来源、有效值、过期字段、选题决策及版本查回 |
| 这题和之前那题是不是一组 | `get_prompt_family`；只有用户确认后才调用 `set_prompt_family_relation` | 区分用户确认关系与自动近似候选，按题族结果查回 |
| 问我一个具体经历，再看之前问过什么 | `guide_thought` 指定 `question_mode: experience`，再用 `get_guide_history` 查询 | 当前一个追问和仅含已保存问题/线索的分页历史 |
| 把刚才那题标成写到一半 | `resolve_reference` 取得题号和版本，再调用 `set_prompt_state`。若还有其他一步，可用 `apply_actions` 的 `use_current_prompt` | 唯一目标、确切状态、版本、可撤销回执；歧义时只追问缺失选择 |
| 接着想上次卡住的那件事 | `continue_thought` 按线索返回卡片；用户选定并授权后再按 `card_id` 读取，或使用 `share_excerpt` 只取片段 | 卡片、可访问的关联题和未完问题 |
| 用这份素材拟一题 | `search_library` 定位；用户明确允许该条后调用 `draft_from_material` | 一条临时草题、素材编号和可核对的剪藏出处。用户要保留时才 `save_prompt` |
| 把两条已选资料拟成对照题 | `draft_prompt_from_materials`，按有序 ID 和精确 selection 只读拟题；用户明确要求保留后才用返回的 `save_parameters` | 草题、逐素材版本与范围、查重和新 request ID 提示；保存后从关系和证据接口查回 |
| 上月和本月实际写完数比较 | `review_prompt_events`，传用户给定本地日期区间与 IANA 时区；再按 `next_call` 查时间线 | 事件计数、覆盖缺口、统计公式和分页明细 |
| 比较升级前后工具目录 | 保存完整的 `get_mcp_directory` 单轮快照，调用 `compare_mcp_directories` 并按 `next_offset` 读取差异 | 协议/目录/schema/权限可比性、变更路径、缺失必填字段和迁移提示；不自动执行迁移调用 |
| 安全重启本机服务 | 用户明确要求后，调用 `restart_service`；用同一 `operation_id` 查询 `get_service_restart`，仅失败可重试时调用 `retry_service_restart` | 从已受理、排空到新实例健康/握手及原请求回执持久化的过程；只有完整 `completed` 回执才算完成 |
| 备份，并恢复某一份 | `create_backup`、`preview_backup`；向用户展示预览，收到明确要求再调用 `restore_backup` | 本机路径、备份 ID、指纹及恢复前安全备份 ID |

每次业务调用返回 `status` 和 `data` 或 `error`。`needs_clarification` 的 `error.details` 提供问题和候选。`version_conflict` 对应重新读取目标与版本。`submission_unknown` 未确认结果，应沿用原请求编号和参数核对。标准结果在 `structuredContent` 中，文本内容复制同一 JSON 供仅支持文本的宿主使用。

具名宿主会按本机配置的 scope 获得工具目录；请求与会话 ID 在服务内按宿主隔离。普通具名 scope 只授予具体工具/数据范围；只有可信管理员明确登记 `tool:*` 且当前目录显示 `full_access=true` 时，远端具名宿主才具备完整内容访问能力。全权限仍按用户本轮点名范围读取；普通具名宿主读取私密片段须走 `share_excerpt` 并由本机操作员批准一次性许可。远端导出只返回 actor 绑定的限时引用和校验信息，不返回本机路径。

### 工具索引

- 出题与检查：`pick_prompt`、`draft_prompt`、`get_prompt`、`narrow_prompt`、`get_angles`、`check_prompt`
- 题目与写作记录：`save_prompt`、`list_prompts`、`set_prompt_state`、`snooze_prompt`、`clear_snooze`
- 主题与偏好：`get_preferences`、`get_preference_reason`、`add_topic`、`update_topic`、`set_direction_weight`、`set_topic_weight`、`reset_weights`、`end_preference`
- 反馈：`record_feedback`、`list_feedback`
- 灵感：`add_inspiration`、`search_inspirations`、`get_inspiration`、`update_inspiration`、`remove_inspiration`
- 社会话题剪藏：`save_clipping`、`list_clippings`、`get_clipping`、`update_clipping`、`remove_clipping`
- 思考续接卡：`save_resume_card`、`list_resume_cards`、`get_resume_card`、`update_resume_card`、`clear_resume_card`
- 本机对话工作流：`get_workflow_guide` 可列出新工具与适用场景。记录类撤销自本版本开始登记，权重、专题及整库恢复不走单条记录撤销。批量动作只有全部成员支持撤销时才返回可撤销回执。
- 会话与追问：`get_session_conditions`、`set_session_conditions`、`end_session_conditions`、`get_prompt_family`、`set_prompt_family_relation`、`pick_unfinished_prompt`、`list_unfinished_prompts`、`get_guide_history`、`set_question_preference`、`get_question_preferences`、`end_question_preference`。
- 时间线与专题资料：`get_prompt_timeline`、`review_prompt_events`、`draft_prompt_from_materials`、`save_prompt_material_relations`、`remove_prompt_material_relation`、`read_entity_relations`、`write_claim_evidence`、`read_prompt_evidence`、`get_topic_project_progress`。
- 目录兼容与服务维护：`compare_mcp_directories`、`restart_service`、`get_service_restart`、`retry_service_restart`。规格 17 的本机验收 CLI 不属于工具目录。

`draft_prompt` 和收窄时生成的草题只在当前请求返回。用户明确要保留时，再调用 `save_prompt`；收窄草题保存时带上返回的 `parent_id`。

方向和主题调权可填写 `reason`。按范围保存的反馈在 7 天内调整选题顺序，明确指定的方向、主题和深浅优先。反馈不会改动权重。

灵感、剪藏和续接卡的 MCP 查询须按当前请求指定关键词、方向、主题或关联命题。列表只返回定位所需字段。读取某条私密内容时，等用户明确许可，再以精确 ID 调用 `get_*` 并设置 `include_private: true`。新闻出处、链接和日期由用户提供，可从网页打开原始链接核对。拟题只使用给出的线索，不自动拉取新闻。

上述 `include_private` 用法适用于本机网页和旧的无名 stdio 宿主。具名宿主只能通过 `share_excerpt` 请求精确字段与片段范围，并等待本机操作员按当前 request ID 批准；审批只可消费一次。跨宿主交接只传被选中的题目和明确选定的线索字段。

## 具名远端宿主（可选）

默认 stdio 服务只连接本机回环服务，也不会启动网桥监听器。需要跨设备接入时，先由可信本机操作员通过 `python -m shuxu.mcp24_admin` 配置具名宿主与工具 scope；bearer 只在本机安全注入，不写入 MCP 配置、日志或提示词。完整命令、授权字段与回执示例见 [具名宿主指南](mcp24-host-guide.md)。

远端宿主使用 `python -m shuxu.mcp24_transport` 作为 stdio 客户端，并设置 `SHUXU_GATEWAY_URL`、`SHUXU_GATEWAY_HOST_ID`、`SHUXU_GATEWAY_BEARER`。本机网桥必须显式运行 `python -m shuxu.mcp24_transport gateway --host <IP> --port <端口>`；仅字面回环 IP 可用 HTTP，非回环监听必须同时提供 `--certfile` 与 `--keyfile`。没有这条配置时不启动远端监听，也不会退回本机模式。外部部署请使用 TLS 与受限网络；不得开放公网明文监听。

连接先握手并固定服务实例与实际工具目录。授权变化、服务实例变化或目录版本变化时，宿主应重新握手。上游消息 ID 与操作槽位是可选的额外去重元数据；普通具名写入仍可使用稳定 `request_id`。具名宿主只能按预览后的指纹恢复；批量删除、私文片段与保留期清理各自使用绑定预览或单次许可。

## 本机数据与隐私

Windows 默认数据文件为 `I:\data\shuxu\data\shuxu.sqlite3`。题目、写作状态、偏好、灵感、截图、剪藏和续接卡保存在这个数据库。HTTP 服务仅监听回环地址。网页会话令牌只保存在当前服务进程内存，MCP 注册配置不包含令牌。具名宿主授权账本和运维任务/诊断账本分别保存在数据库旁的 restore-independent sidecar；授权只存 bearer 摘要，诊断只存阶段耗时与脱敏状态。

本机网页内按 `Alt+I` 打开灵感录入，可选择或粘贴不超过 5 MB 的 PNG、JPEG、GIF 或 WebP 图片。截图读取与维护均在本机网页完成。新增表和字段会在服务启动时自动补齐。使用旧版本时，先运行 `stop.cmd`，再从快捷方式或 MCP 启动新版服务。

状态页只显示服务状态和脱敏诊断日志；诊断日志不记录题文、请求参数或会话令牌。手动备份时，请先停止服务，再复制 `shuxu.sqlite3`。

MCP 导出和备份分别写入数据目录下的 `mcp-exports` 与 `mcp-backups`，可在服务运行时创建。备份包含 SQLite 内的截图。整库恢复会替换当前数据，先自动保留恢复前的安全备份；在预览之后还需要用户明确确认。

保留期清理没有预设天数，也不会自动运行。用户必须先提供统一期限或类别期限，查看候选和保留原因，再用相同计划指纹明确确认；当前会话、未决请求、活动任务、幂等键和提交证明会保留。它管理临时指代、选题历史、已撤销记录、写入回执正文和可选诊断记录，不清除题目、素材或业务数据库记录。

## 隔离运行参数

验收或临时运行时，可以在启动 MCP 宿主前设置以下环境变量。MCP 子进程和它启动的本机服务会继承这些值：

- `SHUXU_PORT`：HTTP 回环端口，默认 `17831`。
- `SHUXU_DATA_DIR`：SQLite 数据目录，Windows 默认为 `I:\data\shuxu\data`。
- `SHUXU_RUNTIME_DIR`：实例身份文件和启动错误日志目录，Windows 默认为 `I:\data\shuxu\runtime`。

隔离验收应同时使用独立端口、数据目录和运行目录，避免与日常实例共用。清理临时数据前先用对应环境变量运行 `stop` 并确认服务已退出。

## 交付状态与接入边界

交付入口为 `F:\桌面\程序\APP\书序.lnk`。它使用 Python 3.12 的 `pythonw.exe` 启动本机服务并调用系统默认浏览器打开维护页；服务本身不显示控制台窗口。实际 Windows Shell 启动已在隔离数据目录中验证，捕获到本机页面 URL 并收到 HTTP 200，测试数据已清理。

本版本提供本机网页和标准 stdio MCP 服务。CuteDuck、QQ 或桌面宠物尚未接入；MCP 示例配置还需由用户随后交给 CuteDuck。
