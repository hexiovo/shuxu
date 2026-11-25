# 书序 MCP 外部 AI 宿主指南

本文说明外部 AI 宿主如何发现、认证并调用书序 MCP。每次使用时以握手和当前宿主的 `tools/list` / `get_mcp_directory` 为准；具名宿主只会看到其授权目录。协议、服务版本、注册工具总数、完整 inputSchema 与既有路由映射见本文件末尾生成附录；该附录只由注册代码生成，不包含任何正式数据。

当前仓库注册代码的服务版本为 1.4.0。正在运行的服务进程可能仍是旧实例；运行版本只以本次握手为准。以下 1.4.0 样例只在对应工具进入当前宿主目录、实际 schema 与本附录一致时适用；旧服务继续按自己的握手、工具目录和实际合同操作。

《书序 MCP 下一版产品规格》的 20 项能力编号与既有 `get_workflow_guide` 的 `01`–`20` 路由 ID 是两套编号。本指南按三批交付收录产品规格 01–20；每项的可用状态仍以实际宿主握手、`tools/list` 和 `get_mcp_directory` 为准。旧版“24 项功能调用索引”仍作为 1.3.0 基线说明保留，不能当成新版能力状态或验收凭据。生成附录反映仓库当前注册定义，不证明一个正在运行的进程已经升级。

外部宿主指令拆成六份独立 TXT，可按完整职责顺序交给不转发 MCP initialization instructions 的宿主：[角色、授权与数据边界](../config/mcp-prompts/01-host-role.txt)、[意图识别与工具路由](../config/mcp-prompts/02-tool-routing.txt)、[资料、全文、来源与附件](../config/mcp-prompts/03-materials-and-full-text.txt)、[更正与管理操作](../config/mcp-prompts/04-admin-operations.txt)、[回执、错误与恢复](../config/mcp-prompts/05-timeouts-and-recovery.txt)、[隔离宿主验收](../config/mcp-prompts/06-host-acceptance.txt)。

## 本批调用样例：按原回执更正命题状态

本例对应产品规格第 10 项。只有当前 `tools/list` 含 `correct_prompt_status` 且 schema 与本指南生成附录一致时才可执行；目录尚无该工具时，不可把本例当成已部署功能或改用猜测参数。

用户说“把我刚才标成写过的那题改成写到一半，再缓到周日”时，先确认本会话有且仅有一条可更正的已提交状态回执；若原回执改过多题，则补问 `prompt_id`。按当前注册 schema 调用 `correct_prompt_status`：

```json
{
  "request_id": "u7-20261125-correction-01",
  "conversation_id": "u7-session-20261125-a",
  "target_request_id": "u7-20261125-written-01",
  "prompt_id": "p-104",
  "state": "in_progress",
  "snoozed_until": "2030-01-06T00:00:00+08:00"
}
```

示例中的 ID 和日期均为说明格式的占位值；实际值必须来自本轮会话与用户给出的具体日期。

`request_id` 和 `conversation_id` 必填；`target_request_id`、`source_conversation_id`、`prompt_id`、`state`、`snoozed_until` 可选。`state` 枚举为 `want_to_write`、`in_progress`、`written`；`snoozed_until` 是带时区的 `date-time` 或 `null`。必须提供 `state` 或 `snoozed_until` 至少一项。以上示例同时改状态和暂缓时间；仅清除暂缓时传 `"snoozed_until": null`。

成功返回中包含原始提交 `original_receipt`、更正对象 `prompt_id`、更正前后的 `before` / `after`（含版本、状态和暂缓时间）、`correction_receipt`、`undo_available` 和 `next_call`。该工具的 `next_call` 指向 `get_prompt`，以准确题目 ID 查回更正后状态和版本。只有实际 receipt 的 `committed=true` 才可报告更正完成。

未提供 `target_request_id` 且当前会话没有最近可核对回执时返回 `needs_clarification`，要求提供原编号或确认来源会话；显式指定的原编号不存在、未提交或不属于该会话时返回 `not_found`。原回执修改多题且没有 `prompt_id` 时返回候选并要求选择。回执与当前命题版本不同或目标已撤销时返回 `version_conflict`，附当前状态及 `get_prompt` 下一调用；不覆盖并发修改。参数缺少 `state` 和 `snoozed_until` 时返回 `invalid_params`。

## 本批调用样例：跨天续接同一条思路

用户说“把这道题现在卡住的地方存下来，明天接着想”时，先读取当前题的 ID 和版本，再只保存用户明确给出的线索，不根据上下文补写用户没说的事实。`save_thought_checkpoint` 的完整输入 schema 由生成附录给出；必填字段及约束如下：

- `request_id`、`conversation_id`、`prompt_id`：非空 ID 字符串，长度 1–200。
- `expected_version`：正整数；必须使用刚读取题目的当前版本。
- `cue`、`unanswered_question`、`progress`：非空字符串，最长 500 字符。
- `guide_step`：整数 0–3。

成功 `data` 包含 `checkpoint_id`、`prompt_id`、`prompt_version`、`saved_at` 和完整 `checkpoint` 对象，其中带来源会话、保存时题目正文与版本、线索、未答问题、进度及追问步骤。只有实际提交回执确认 `committed=true` 后，才回复检查点已保存。版本冲突返回 `version_conflict`，附 `prompt_id`、`expected_version`、`actual_version`；读取新版本后重新核对用户意图。

例如，用户说“把这题卡住的地方存下来，明天接着想”并已确认本轮题目版本时，完整必需参数如下；示例 ID、版本和线索仅用于说明：

```json
{
  "request_id":"session-a-checkpoint-01",
  "conversation_id":"session-a",
  "prompt_id":"p-104",
  "expected_version":7,
  "cue":"我已经比较了两个选项，但还没决定按哪一个解释。",
  "unanswered_question":"还缺哪条证据能判断两种解释？",
  "progress":"完成问题拆分，下一步核对来源。",
  "guide_step":1
}
```

成功 `data` 的核心结构是 `request_id,committed,checkpoint_id,prompt_id,prompt_version,checkpoint,saved_at`。`checkpoint` 含 `id,source_conversation_id,prompt_id,prompt_version,prompt:{id,text,direction,topic_id,version},cue,unanswered_question,progress,guide_step,created_at`。只保存用户明确给出的思路，不把宿主推断写进这几个字段。

次日新会话可按题目、来源会话或检查点精确定位，例如：

```json
{
  "conversation_id": "session-b",
  "prompt_id": "p-104",
  "source_conversation_id": "session-a",
  "history_scope": "same_host",
  "limit": 10
}
```

以上 ID 为格式占位值。`continue_thought` 的输入字段均可选；命中后必须提供当前稳定的 `conversation_id`，用于建立新会话关系。日期过滤使用带时区的 `created_after` / `created_before`；`history_scope` 取 `same_host` 或 `all_hosts`，`limit` 为 1–50。

如果用户只说“接着上次”且没有指定检查点过滤条件，宿主传当前 `conversation_id`，服务先检索当前宿主默认范围：仅一条检查点时直接恢复；多条时返回候选并要求选择；没有检查点才回退查询旧续接卡。宿主不自行挑选最新检查点。

成功结果含保存时的 `checkpoint` / `prompt`、当前可见 `current_prompt`、`prompt_changed`、`prompt_text_changed`、`resume_status`、`conversation:{source_conversation_id,conversation_id,relation}`、其它 `saved_points` 和 `next_calls`。题目版本或正文已变化时先展示保存版本与当前版本的差异；只有 `resume_status="ready"` 才按返回的 `guide_thought` 下一调用，等用户回答后再传新的 `request_id` 与一句获准的线索。`guide_thought` 必填 `conversation_id` 与 `request_id`，并按当前 schema 传 `prompt_id`、`mode:"continue"`、`resume_checkpoint_id`、可选 `history_scope` 和本轮短 `focus`。

多个命中返回 `needs_clarification`，带 `question`、`candidate_count`、`candidates` 和 `has_more`；无匹配返回 `not_found`。`card_id` 与检查点过滤字段同时出现返回 `invalid_params`；非全权限 actor 请求 `history_scope="all_hosts"` 返回 `forbidden`。宿主只可读取当前 actor 实际有权查看的题和检查点。

## 本批调用样例：没有题时核对单项放宽

用户说“给我一题，五分钟，避开社会话题，近一周不要重复”时，使用 `next_prompt`。必填参数是稳定的 `conversation_id` 和 `request_id`；本例的条件字段为 `minutes`（整数 1–240）、`exclude_directions`（1–3 个方向枚举）、`avoid_days`（整数 0–90）。方向取 `self_exploration`、`consultation_reflection` 或 `social_topics`；没有传 `avoid_days` 时按 7 天避重。

成功返回一题和 `decision_id`；`data.decision` 记录实际候选总数、交集标志和逐阶段 `filter_stages`，并包含服务实际选题理由。若没有候选，错误 `details` 包含 `cause`、`candidate_count=0`、`intersection_exists=false`、`effective_conditions`、`filter_stages`、`relaxations` 及顶层 `next_calls`。每个过滤阶段列 `filter`、`before`、`excluded`、`after`，解释候选在该阶段前后变化。

只有本轮输入中实际生效的约束会单独试算；可见的过滤名包括 `direction`、`topic`、`depth`、`time_depth`、`excluded_directions`、`recent`。每项给出候选数和保留条件。若放宽后没有新增候选，服务不会为该项提供可执行 `next_call`；产生候选时，`next_call` 给出精确的 `next_prompt` 工具和 arguments，顶层 `next_calls` 只列可选的正数结果。用户选定某一项后才按该调用重试，补一个新的 `request_id`；不同时放宽两项，也不自动代替用户决定。

下面只展示结构，所有过滤阶段、计数、条件和调用参数都必须来自本次实际回执：

```json
{
  "status": "no_candidate",
  "error": {
    "code": "no_candidate",
    "details": {
      "cause": "no_eligible_prompts",
      "candidate_count": 0,
      "intersection_exists": false,
      "effective_conditions": {
        "minutes": 5,
        "allowed_depths": ["light"],
        "excluded_directions": ["social_topics"],
        "avoid_days": 7,
        "recent_prompt_count": 4
      },
      "filter_stages": [
        {"filter": "recent", "before": 2, "excluded": 2, "after": 0}
      ],
      "relaxations": [
        {
          "filter": "recent",
          "candidate_count": 2,
          "remaining_conditions": ["time_depth", "excluded_directions"],
          "next_call": {
            "tool": "next_prompt",
            "arguments": {
              "conversation_id": "<same-session>",
              "minutes": 5,
              "exclude_directions": ["social_topics"],
              "avoid_days": 0
            },
            "condition": "用户选择此项后补一个新的 request_id"
          }
        }
      ],
      "next_calls": [
        {
          "tool": "next_prompt",
          "arguments": {
            "conversation_id": "<same-session>",
            "minutes": 5,
            "exclude_directions": ["social_topics"],
            "avoid_days": 0
          },
          "condition": "用户选择此项后补一个新的 request_id"
        }
      ]
    }
  }
}
```

示例计数为合成结构值，不代表任何书库。指定方向与排除方向冲突时返回 `needs_clarification`，询问用户保留还是避开该方向；不要用 `no_candidate` 或擅自删条件处理。

## 本批调用样例：核对全权限身份与完整目录

产品规格第 01 项使用同一个具名宿主身份目录，不依赖额外的 `content:full`、管理 scope 或 profile 保留字。授权唯一全权限 grant 是 `scopes=["tool:*"]`；它覆盖工具及完整内容。普通具名 scope 仍按精确工具/数据范围生效，不能由 `profile` 名称、提示词或某一项读取权限推导为全权限。

本机可信管理员通过现有隔离或正式书库运行 `python -m shuxu.mcp24_admin --database <库路径> host create --host-id <唯一ID> --profile <自选名称> --scopes tool:*`。CLI 隐藏提示操作员提供 bearer，不生成、不回显凭据；回执为 `host_id`、`profile`、`scopes`、`authorization_revision`、`enabled`。更改 profile/scope 时用 `host configure`；配置有变化会增加授权修订号，旧握手、游标和绑定状态须重新建立。任何文档中的命令都是说明，不会替用户运行或配置外部宿主。

连接后先查 `get_mcp_directory({"limit":100,"offset":0})`，必要时用 `next_offset` 读取下一段。响应 `identity` 标明 `host_id`、`profile`、`authorization_revision`，`full_access` 是当前身份实际全权限结果；`directory_version` 是服务版本，`directory_schema_version` 当前为 1。`capabilities` 的实际字段包括 `full_access`、`all_tools`、`complete_record_reads`、`full_text_context`、`indexed_search`、`original_image_chunks`、`complete_library_export`、`administration` 和 `tool_count`。`categories` 列出每类实际工具名和数量。`tools` 中每项含 `name`、`description`、完整 `inputSchema`、`category`、`read_only`、`destructive`、`idempotent` 和 `receipt:{type,request_id_required,status_tool}`。`total_count` 是当前筛选后的总项数，`count` 是本页项数，`complete` 与 `next_offset` 决定是否还有下一页；用 `query` 时总数只对应该检索结果。以实际工具名和 schema 决定是否调用，不把注册总表当作当前身份可见列表。

HTTP 握手仍是另一份连接证据：本批代码定义协议版本 1、服务版本 1.4.0，并保留 `instance_id`、授权修订、目录摘要及完整 `tool_directory`。外部运行进程若报告不同版本或目录，按它的实际结果处理并重新连接/检查；不要仅凭仓库版本号宣称服务已升级。`diagnose_mcp_compatibility` 的必要参数是 `expected_protocol_version` 和 `expected_service_version`，可选 `expected_instance_id`、`expected_directory_id`；成功 `data` 返回逐项 `checks`、`compatible`、实际版本/实例、宿主目录 ID 和两侧授权修订。握手失败返回 `service_unavailable`；版本/实例不同是兼容检查结果，不是调用成功证明。

## 本批调用样例：读取完整档案、图片与范围导出

以下完整读取工具只对 `tool:*` full actor 开放。标准本机 stdio 的本地身份有全权限；具名远端宿主必须由可信管理员明确登记 `tool:*`，并在 `get_mcp_directory` 核对 `full_access=true` 及实际工具名。普通具名 host 调用完整档案、附件或导出会被拒绝；不得转用本机/其他 host 凭据。

精确读取一条记录：

```json
{"kind":"prompt","id":"p-example"}
```

`kind` 可为 `prompt`、`inspiration`、`clipping`、`resume`、`topic`。命题结果包含原始 `record`、`content_provenance`、`related_materials`、`claim_sources` 和 `continuation_cards`；主题结果包含原始 `record`、`aliases`、别名版本及子主题、题目和材料 ID；材料结果包含完整 `record.content`，剪藏还可带 `claim_references`，以及可能为空的 `attachment` 校验元信息。精确 ID 不存在或当前 actor 不可见时为 `not_found`。

范围列表分页：

```json
{"kind":"all","direction":"self_exploration","created_from":"2026-01-01T00:00:00Z","created_to":"2027-01-01T00:00:00Z","page_size":50}
```

`list_full_records` 可选 `kind`（默认为 `all`）、`prompt_id`、`direction`、`topic`、`state`、`created_from`、`created_to`、`page_size`（默认 20，1–100）或 `cursor`。日期均为 date-time；开始包含、结束不包含，且开始须早于结束。`state` 只匹配命题；主题记录不应用题目 ID、状态、日期条件。成功结果含 `items`、`total`、`record_count`、`current_range:{start,end}`、`page_size`、`complete`、`next_cursor`、`snapshot_version`、`snapshot_revision`、`sort:["kind","id"]` 和规范化后的 `filters`。翻页只传上一页 `cursor`；它绑定 host/profile、授权修订、scope hash 和快照。`cursor_forbidden` 表示游标不属于当前身份；`cursor_invalid`、`cursor_expired`、`cursor_stale` 均丢弃旧游标并用原筛选重新开始，不能将两个快照的页面拼在一起。

读取已存灵感原图时，使用材料 ID 作为 `id`，`offset` 从 0 开始，`max_bytes` 为 1–65536：

```json
{"id":"inspiration-example","offset":0,"max_bytes":65536}
```

结果有 `content_type`、`size_bytes`、整图 `sha256`、本段 `offset`、`chunk_base64`、`chunk_sha256`、`next_offset` 和 `eof`。连续取到 `eof=true` 后解码连接全部分段，核对总字节数和整图 hash。仅灵感原图支持此接口；不存在材料/图片返回 `not_found`，offset 大于文件大小返回 `invalid_params` 并带 `size_bytes`。

用户明确要求导出时，先缩小范围，并使用稳定的 `request_id`：

```json
{"request_id":"export-session-7-01","kind":"prompt","direction":"self_exploration"}
```

`export_full_library` 的范围字段为可选 `kind`、`prompt_id`、`direction`、`topic`、`created_from`、`created_to`；格式固定为 JSONL，不传 `format`。仅首次成功回执带 `download:{tool:"read_full_export_chunk",max_bytes:65536}`；重放同一 request ID 时应另调 `get_full_export_info({"export_id":"<返回值>"})` 取回 `format`、`size_bytes`、整文件 `sha256`、`record_count`、`attachment_count`、`expires_at` 和 `max_chunk_bytes`。导出 JSONL 含清单、所选完整记录和灵感原图 base64；若存在，还含关联的检查点、来源声明、专题项目，以及方向和偏好理由。`record_count` 是文件中的记录行总数，可能包含这些关联记录，不等同于 `list_full_records.total`。文件在服务数据目录私有子目录生成；接口只返回 actor 绑定的一小时 `export_id`，不返回本机路径。

用 `read_full_export_chunk({"export_id":"<返回值>","offset":0,"max_bytes":65536})` 顺序取段，结果带 `size_bytes`、整文件 `sha256`、段 `chunk_base64` / `chunk_sha256`、`next_offset`、`eof`。拿到 `eof=true` 后核对整文件 SHA-256。相同 request ID 只能配相同导出范围；范围不同返回 `idempotency_conflict`。过期引用返回 `expired`；引用找不到或不属于本 host 返回 `not_found`；本地文件丢失/篡改返回 `version_conflict`。游标与导出引用不跨身份或授权修订迁移。

## 本批调用样例：先生成自然语言调用计划

产品规格第 02 项的 `plan_mcp_calls` 是只读规划器，不执行计划中的任何工具。按当前 schema，可传 `scenario` 和该场景各工具的 `arguments_by_tool`；或选择结构化 `intent` 并给目标线索：`intent` 仅接受 `get_prompt`、`set_prompt_state`、`snooze_prompt`，定位字段包括 `prompt_id`、`conversation_id`、`reference`、`keywords`，另可传 `state`、`time_text`、`timezone`、`expected_version`。完整字段、长度、枚举见本指南生成 schema。

例如，用户说“查一下刚才那题现在什么状态”时，仅规划：

```json
{"intent":"get_prompt","conversation_id":"session-a","reference":"current"}
```

成功结果包括 `intent`、`resolved:{prompt_id,version,direction,topic_id}`、`time_resolution:{source_text,timezone,absolute}`（非时间意图时为 `null`）、`calls`、`dependencies`、`candidate_evidence`、`missing_fields`、`needs_clarification=false`、`writes_performed=false`。调用项列出 `tool`、精确 `arguments`、目标版本 `precondition`、`depends_on` 和 `callable_now`。如果目标无法唯一解析，结果以 `needs_clarification=true` 给出问题和候选证据；若调用写入意图，则即使 `callable_now` 不为真，规划也不会执行写入。用户要求写入后仍需确认具体值、按真实工具 schema 另行调用并核对提交回执，不能直接执行计划 JSON。时间文本不完整、无效的夏令时本地时间或重复的夏令时时刻会返回 `needs_clarification` 和 `question`；缺少/错误 IANA 时区或时分字段错误返回 `invalid_params`，宿主不替用户猜时区或选重复时刻。

`scenario` 方式只接受当前 `get_workflow_guide` 声明的路由 ID；`arguments_by_tool` 的工具名只能来自该场景，错误输入为 `invalid_params`。结构化 intent 的字段缺失会在结果中返回 `missing_fields`；歧义目标返回 clarification，不制造工具调用。参考旧版本计划内容时，重新取得当前路由和工具目录。

## 批次 II：会话条件、题族、未完成题与追问方式

### 规格 05｜当前会话条件沿用

会话条件按当前 `conversation_id` 保存。`get_session_conditions` 必填 `conversation_id`；成功返回 `version`、`conditions`、逐字段 `sources`、`expired_fields`、`updated_at` 和优先级 `precedence:["call","session","preference"]`。首次版本为 0。先读当前值和版本，再写入。

`set_session_conditions` 必填 `request_id,conversation_id,expected_version,conditions`。`conditions` 至少包含一项，可选字段为 `direction`、`topic`、`depth`（`light|medium|deep`）、`minutes`（1–240）、`exclude_directions`（1–3 个方向）和 `avoid_days`（0–90）；可选 `expires_at` 是带时区 date-time，须晚于当前时间。设置时只替换给出的字段；未提供的已保存条件仍保留。成功回执含 `committed=true`、新 `version`、实际 `conditions`、来源时间和每字段过期时间。按新版本调用 `get_session_conditions` 查回。

`end_session_conditions` 必填 `request_id,conversation_id,expected_version`，可选 `fields`（要结束的条件名）；省略时结束全部。成功回执含 `committed=true,ended_fields,ended,version`；没有匹配字段时 `ended=false` 且版本不增长。`version_conflict` 时重新读取，不使用旧版本。无效/空条件、主题方向不一致、到期时间已过返回 `invalid_params`；指定主题不可用为 `not_found`。`next_prompt` 会把本次显式条件放在会话条件之前，决策回执包含实际 `condition_sources,inherited_fields,conflicts`；只在该次调用中忽略某些保存字段时使用 `ignore_session_fields`，不修改保存值。

### 规格 06｜区分确认题族与近似题

`get_prompt_family` 必填 `prompt_id`，可选 `limit`（默认 10，1–30）。结果含当前 `prompt`、`family_ids`、parent_id 收窄组 `parent_family_ids/parent_family`、用户确认的 `confirmed_relations`、`separated_relations`、自动近似 `similar_candidates`、候选总数和 `has_more`。近似候选带相似度/重复标记和理由；这只是建议，不能当作已确认关系。

只有用户明确确认同族或拆分时才调用 `set_prompt_family_relation`。必填 `request_id,prompt_id,related_prompt_id,relation,expected_version`；`relation` 为 `same_family|separate`，`expected_version` 是该两题关系版本（新关系为 0），可选 `reason`（最多 300 字符）。成功返回 `committed=true`、双方 ID、relation、版本和更新时间，随后用 `get_prompt_family` 查回。不能把由 `parent_id` 确认的收窄题族直接拆成独立题；此情形返回 `needs_clarification`，具体对象不可见为 `not_found`，版本变化为 `version_conflict` 并附 `get_prompt_family` 下一调用。取题时，只有用户指定或已确认的题族 ID 才传入 `next_prompt.exclude_family_ids`；只在当前一轮绕过继承条件用 `ignore_session_fields`，不要保存临时排除。

### 规格 07｜从未完成题里选一题

用户说“我只有十分钟，看看有什么没想完”时，若要直接选一题，调用 `pick_unfinished_prompt`：必填 `request_id,conversation_id`；可选 `direction,topic,depth,minutes,exclude_directions,avoid_days,exclude_family_ids,limit`。枚举、长度和范围以生成附录为准；`limit`（1–20）只限制返回的替代项。该调用会记录一次选题 decision。成功回执含主推荐 `prompt`、`decision_id`、`decision`、`alternatives,alternative_count,has_more`；decision 给候选总数、近期排除数量、条件来源、冲突、最近活动、续接点和排序依据。排序先看 `in_progress`、`want_to_write`、到期暂缓、仅有检查点，再看已保存检查点和最近活动。已写完、全新题和未到期暂缓题不进入续接候选。没有候选时返回 `no_candidate`，含 `candidate_count,eligible_before_recent_count,recently_excluded_count,new_prompt_count,future_snoozed_count,effective_conditions,explanation`；只有近期避重造成空池时才提供放宽该条件的 `next_call`，必须先取得用户选择。

用户说“先把还能继续的题列出来”或需要浏览所有候选时，改用只读 `list_unfinished_prompts`，避免为每页产生新的选题记录。必填 `conversation_id`；可选过滤为 `direction,topic,depth,minutes,exclude_directions,avoid_days,exclude_family_ids`，以及 `limit`（默认 20，1–50）和 `offset`（默认 0，0–10,000）。成功 `data` 含 `items,total,offset,next_offset,complete,snapshot_at,effective_conditions,eligible_before_recent_count,recently_excluded_count,new_prompt_count,future_snoozed_count,rank_basis,explanation`。每项含 `prompt,position,priority_rank,state,last_activity,checkpoint_id,continuation_point,rank_reason:{status_priority,saved_checkpoint_first,latest_activity,prompt_id_tiebreak}`。空列表是正常的 `ok` 结果，`items=[]`、`total=0`、`complete=true`；它不写入 recommendation history。续页保留原筛选和 `conversation_id`，只把 `offset` 改为 `next_offset`。`snapshot_at` 是读取时间标记，不是跨页数据库快照；若其间题目状态或活动变化，重新从 offset 0 读取。用户从列表选定题目后，用实际可见的 prompt ID 继续相应只读查询或按明确意图调用 `guide_thought`；不要为了翻页重复调用会记录 decision 的 picker。

### 规格 08｜按用户指定方式追问

`guide_thought` 可选 `question_mode`：`experience|contrast|counterexample|boundary|evidence|narrow`。用户明确要求时传该枚举；否则服务根据当前问法偏好或旧步骤规则选择。成功结果含 `prompt_id,step,phase,question,question_mode,focus,preference_basis,related_history,next_paths,next_call`；一轮只返回一个追问，`focus` 只放本轮明确给出的最少线索，答复本身不会保存。继续到第二步之后若未提供 `focus`，服务返回 `needs_clarification`。

用户要求看已保存追问时调 `get_guide_history`，至少给 `conversation_id` 或 `prompt_id`；可选 `history_scope`（`same_host|all_hosts`）、`limit`（默认 20，1–50）和 `offset`（0–10,000）。结果 `items,total,offset,has_more`；每项含记录 ID、conversation/prompt ID、question_mode、focus、question、step 和时间，不含用户回答；按 limit/offset 翻页。跨 host 读取需 full access；不给任何定位字段是 `invalid_params`，不可见题目为 `not_found`，请求 `all_hosts` 被拒绝时停止。

### 规格 09｜管理追问方式的长期和近期偏好

方式枚举与规格 08 相同。`get_question_preferences` 可选 `direction`、`include_history` 和 `limit`（1–50）；返回全局偏好 `version`、范围内 `items`、按方向解析的 `effective` 值；启用 history 时还返回 `history`。`effective` 每种方式都列强度、scope、方向、来源、过期时间和理由；默认值会明确标成 default。

`set_question_preference` 必填 `request_id,expected_version,mode,scope,strength`；`mode` 为六种问法之一，`scope` 为 `baseline|recent`，`strength` 为 0–2；可选 `direction`（省略表示默认/全局）、`reason`（最多 300 字符）和 `expires_at`。长期 baseline 不得传到期时间；近期 recent 必须传未来时刻的带时区 `expires_at`。先用 `get_question_preferences` 读取全局版本。成功返回 `version,before,after`，然后重新读取确认有效值。`end_question_preference` 必填 `request_id,expected_version,mode`，可选 `direction`，只结束相应 recent 覆盖，返回 `ended,before,after,version`；不会删除长期偏好。

当前 `guide_thought` 的显式 `question_mode` 优先；没有显式方式时近期偏好优先于长期，方向特定值优先于默认值，再按有效强度在非零方式中选择。若所有方式强度为 0，服务返回 `needs_clarification` 并列允许问法。版本冲突先读取新版本；无效 scope/期限为 `invalid_params`，不存在方向为 `not_found`。

## 批次 II：读取命题历史与按事件回顾

以下两项基于实际注册工具 `get_prompt_timeline` 和 `review_prompt_events`。事件账本只从启用后记录真实提交，不回填启用前的状态变更；必须读取返回的 `coverage`，不能把没有历史记录解释成“从未发生”。当前 actor 看不到的题不通过历史接口绕过权限。

### 规格 11｜按题读取状态时间线

用户问“我什么时候开始这题、哪次把它暂缓了？”时调用 `get_prompt_timeline`。`prompt_ids` 必填，数组 1–20 个；ID 不可重复。可选 `from` 与 `through` 是带时区 ISO date-time，前者包含、后者不包含，且 `from < through`。`event_types` 可选，枚举为 `created`、`state_changed`、`snoozed`、`snooze_cleared`、`corrected`、`undone`；`page_size` 默认 20，范围 1–50；`offset` 默认 0，且不得小于 0。

单题调用示例：

```json
{"prompt_ids":["<本轮已定位的prompt_id>"],"page_size":20,"offset":0}
```

成功 `data` 为 `events,total,page_size,offset,next_offset,coverage,sort`，按 `sort:["occurred_at","sequence"]` 排序。事件含 `event_id,prompt_id,event_type,action,step_number,occurred_at,before,after,source,reason,continuation_cards,next_call`；`before` / `after` 分别提供版本、状态和暂缓时间。`reason` 是写入时真实提供的可选操作理由；旧事件或未提供时为 `null`，不得推断。`source` 中的 request、target request 与 conversation ID 仅在同一 host、profile 和授权修订下回显；跨 actor 时这些引用为空。`continuation_cards` 只包含当前 actor 会话中与事件版本相符、且事件时间前已保存的续接点。每条事件的 `next_call` 指向该题的时间线读取；分页用顶层 `next_offset`，保留原筛选，只把 offset 换成返回值。

每个 `coverage` 项给出该题 `available_from`、`first_recorded_event`、`gap_before_available_from`、`older_changes_reconstructible:false` 和解释。历史账本启用前的状态变化不可重建。批次 II 中新建题目并保存题目—素材关系时，同一事务记录 `created` 事件；只给已有题增加、修改或删除关系不产生题目状态事件。重复 ID、日期格式/时区错误或倒置时间窗返回 `invalid_params`；题目不存在或当前 actor 不可见返回 `not_found`。遇错误按当前 host 与 ID 核对，不跨宿主重试。

### 规格 12｜按真实发生日期回顾或比较

`review_prompt_events` 的必填参数为 `periods` 和 `timezone`。`periods` 含 1–2 个对象，每项必填 `from_date`、`through_date`（日期字符串，首尾日期均纳入本地日历区间），可选 `label`；`timezone` 必须是有效 IANA 时区。可选 `direction`、`topic`、`event_types`、`page_size`（默认 20，1–50）和 `offset`（默认 0，非负整数）。`event_types` 使用规格 11 的六个枚举。

比较示例（只示范参数形状，区间应由用户指定）：

```json
{
  "periods":[
    {"label":"前一段","from_date":"2026-10-01","through_date":"2026-10-31"},
    {"label":"后一段","from_date":"2026-11-01","through_date":"2026-11-30"}
  ],
  "timezone":"Asia/Shanghai",
  "page_size":20,
  "offset":0
}
```

成功 `data` 为 `periods,comparison,event_types,filters`。每段返回原日期、时区、换算后的 UTC `from` 与不包含的 `through_exclusive`、`coverage`、`counts`、`formula`、分页 `records,total,page_size,offset,next_offset`。`comparison` 仅在两段输入时为 `true`。统计口径以回执公式为准：`completed_events` 是区间内从非 `written` 转为 `written` 的 state-change/correction 事件；`reopened_events` 是从 `written` 转为其它状态；`completed_prompts_currently_written` 是区间内有完成事件且当前仍为 `written` 的不同题目数；`prompts_with_period_events_currently_written` 是区间内有任意事件且当前为 `written` 的不同题目数；`currently_written_in_scope` 是当前筛选范围内所有仍写完的题，不按发生日期过滤。不要把当前状态计数称为该区间完成数。

`records` 是真实事件视图，可通过事件中的 `next_call` 下钻到相应题目的完整时间线；下一页保留查询条件，只按返回 `next_offset` 调用。`coverage.covers_entire_period=false` 或含 `uncovered_interval` 时，说明未覆盖部分，不推算缺失事件。日期区间倒置/日期无效/IANA 时区无效返回 `invalid_params`；指定主题不存在返回 `not_found`。主题、方向和事件筛选以回执 `filters` 为准。

## 批次 II：材料关系、说法证据与专题进度

### 规格 13｜多条素材拟一条对照题

用户明确点名两条或更多资料时使用只读 `draft_prompt_from_materials`；不能让搜索摘要代替用户选定的资料。`materials` 必填，至少两项且最多 12 项；每项必填 `kind`、`id`、`selection`，可选 `expected_version`、`field`、`start`、`length`。`kind` 为 `inspiration|clipping|resume`；`selection` 为 `full|excerpt|link_title`。`excerpt` 必须带有效素材字段、非负 `start` 和正 `length`（最大 20,000）；`link_title` 只用于剪藏标题；字段必须属于该 kind。重复素材、片段越界、内容不可读或版本变化时，按服务的 `invalid_params`、`needs_clarification`、`forbidden` 或 `version_conflict` 停止，不缩小/扩大内容范围。

必填 `relation_type` 取 `comparison|continuation|counterexample`；可选 `direction`、`topic`、`depth`（`light|medium|deep`）。可读字段按 kind 限定：inspiration 为 `text|source|url|note`，clipping 为 `title|url|published_on|source|note`，resume 为 `title|note|question`。成功 `data` 返回一条尚未保存的 `prompt`、关系类型、按输入顺序的 `materials`（含对象版本、位置、selection、实际内容、来源日期/出处）、`deduplication`（重复/相似标记、候选和计数）及 `save_parameters` 建议。此调用不保存草题。若用户随后明确要求保留，可按建议中的 `tool` 和 `arguments` 另行调用 `save_prompt_material_relations`，补一个新的唯一 `request_id`；提交前仍需核对当前目录 schema、素材/题目版本和用户明确的保存意图。不要把只读草题回执当成写入回执。候选命题按当前 actor 可见范围返回；无权读取字段时停止并说明。

### 规格 14｜保存或移除题目与素材关系

`save_prompt_material_relations` 的必填字段为 `request_id`、`relation_type`、`materials`；题目定位必须且只能选择一条路径：现有题用 `prompt_id` 与 `expected_version`，新题用 `prompt:{text,direction,topic?,depth?,parent_id?}`。`relation_type` 的注册枚举为 `comparison|continuation|counterexample|example|context`。`materials` 需 1–20 项，每项必填 `kind,id,expected_version,selection,role,position`，可选 `expected_relation_version`、`field`、`start`、`length`、`description`。素材 kind 为 `inspiration|clipping|resume`；角色为 `source|example|counterexample|context|contrast|support|opposition|indirect|unverified`；selection 规则与规格 13 相同。用当前题版本和每条素材版本构造写入，已有关系更正时另外传其当前 `expected_relation_version`。题目、素材或关系任一版本变化都不重试旧版本。

成功回执含 `prompt_id`、`prompt_created`、`relation_type`、逐条 `relations`、`before_version`、新的 `version`、`committed`、`changed` 和 `next_call`。每个关系回显 `relation_id`、双方 ID/版本、角色、顺序、selection/范围、创建与更新时刻及关系版本。只有 `committed=true` 才报告写入；按 `next_call` 调 `read_entity_relations` 查回。移除单条关系调用 `remove_prompt_material_relation`，必填唯一 `request_id`、`relation_id` 和该关系当前 `expected_version`；成功返回关系前值、`prompt_version`、`removed=true`、`committed=true`，随后按题目或素材端查关系确认已移除。版本不符为 `version_conflict`；对象不存在/不可见为 `not_found`；重复 ID、字段或组合无效为 `invalid_params`；资料字段权限不足为 `forbidden`。写入接受隐私检查，`privacy_rejected` / `privacy_scope_required` 时停止，不能改写绕过。

`read_entity_relations` 必填 `kind` 与 `object_id`；kind 为 `prompt|inspiration|clipping|resume`。`limit` 默认 20，范围 1–100；`offset` 默认 0，非负。成功返回 `items,total,next_offset`，每项按读取端标 `outgoing` 或 `incoming`，含 relation ID、source/target、relation type、role、position、description、selection/字段范围、源素材版本、更新时间和关系版本。题目端可同时显示旧版 claim-source 标签和新 claim evidence；兼容旧项会标成 `legacy:true` / `version:0`，含义为旧记录只关联来源、未保存具体证据片段。续页用原 kind/object_id 和 `next_offset`。

### 规格 15｜保存并查回说法证据

`write_claim_evidence` 只用于用户明确要求记录的说法和剪藏证据，不代表独立事实核验。必填 `request_id,prompt_id,expected_prompt_version,claims`；`claims` 为 1–15 项，每项必填 `text`（非空、最多 500 字符），可选 `claim_id`、该说法的 `expected_version` 和 `evidence`（0–20 项）。证据项必填 `clipping_id,material_version,support_type`，可选 `evidence_id`、证据项 `expected_version`、`field`、`location`、`excerpt`、`conclusion`。`support_type` 为 `supports|opposes|indirect|unverified`；field 为剪藏的 `title|url|source|published_on|note`。支持、反对或间接证据需明确 `location` 和原文 `excerpt`；`unverified` 可留空。`checked_at` 由服务设置，不由宿主回填。

成功返回 `prompt_id,before_version,version,claims,checked_at,verification,committed,changed,next_call`；`verification` 标为 `recorded_scope_not_independent_fact_check`。逐条 claim 和 evidence 回显各自 ID、内容、source 字段/位置/片段/结论、材料版本、`checked_at`、前值和版本。只有 `committed=true` 才报告记录完成，再按 `next_call` 调 `read_prompt_evidence` 查回。可用 `claim_id` / `evidence_id` 更新既有条目时，同时传对应当前版本；写入前确认题目版本及剪藏版本。缺少 required location/excerpt、空说法或字段不属于剪藏为 `invalid_params`；题目、说法或证据不在范围内为 `not_found`；题目、剪藏、说法或证据被更新时为 `version_conflict`，按错误给出的读取工具重新读取；隐私拒绝按 `privacy_rejected` / `privacy_scope_required` 处理。

`read_prompt_evidence` 必填 `prompt_id`；`limit` 默认 50、范围 1–100，`offset` 默认 0。成功返回 `claims,total,next_offset`；claim 含 `claim_id,text,version,items`，item 含 `evidence_id,material{kind,id,version},support_type,field,location,excerpt,conclusion,checked_at,version`。旧 `remember_claim_sources` 数据以 `legacy:true`、`support_type:"unverified"` 和说明呈现，不追溯生成不存在的引文。分页只按原 prompt ID 和返回 offset 继续。

### 规格 16｜读取完整专题进度

`get_topic_project_progress` 必须提供 `project_id`；可选 `limit` 默认 50、范围 1–100，`offset` 默认 0。它要求当前宿主目录明确为 full access；普通具名宿主返回 `forbidden` 时不能借其它工具拼出全文。专题不存在为 `not_found`。

成功 `data` 为 `project,members,gaps,next_clue,total,next_offset`。`project` 含 ID、标题、版本、更新时间、成员总数和下一 offset；成员页按专题存储顺序列出命题全文/状态/版本、关联关系与说法证据、续接点，或完整素材记录及其关系。`gaps` 列尚未完成状态、未答续接问题和没有可核对证据的说法；旧版来源关联仍视为 `unverified`。`next_clue` 从这些已保存缺口中选一个并返回其 `basis` 与 `next_call`；没有缺口时指向专题首个成员。专题成员有分页，但缺口与下一线索是按全体成员计算的汇总；需要检查所有成员时仍须取完成员页。可按返回 `get_thought_context`、`read_prompt_evidence`、`get_prompt` 或 `get_full_record` 参数继续下钻，并用实际 ID 查回建议依据。

## 规格 17｜隔离宿主验收 runner

`shuxu.mcp25_host_acceptance` 是本机一次性验收 CLI，不是 MCP 工具，也不连接 CuteDuck、QQ 或外部宿主。它为本次运行创建临时工作区、数据库、runtime 目录与回环端口，直接启动本机 stdio MCP 和隔离服务，完成后核对并停止本次服务；只有清理确认成功才删除工作区。它验证当前仓库实现，不代表用户已配置或验收真实客户端。

从项目根目录运行，给每次执行新的非空 `run_id`；需要保存完整报告时传绝对输出路径：

```powershell
py -3.12 -X utf8 -B -m shuxu.mcp25_host_acceptance --run-id "local-check-20261125-01" --output "$env:TEMP\shuxu-host-acceptance-local-check.json"
```

不传 `--output` 时，完整 JSON 报告打印到标准输出。成功退出码为 0 且 `status="passed"`；验收检查失败时退出码为 1 且 `status="failed"`；参数、输出或 runner 外层异常返回退出码 2，并打印 `status,error_code,message`。完整报告包含 `format_version,run_id,started_at,finished_at,status,steps,environment,cleanup,reproduction`，并按运行阶段提供 `host,tool_catalog,named_gateway,fixture,attachment,export,sqlite,checks,last_successful_step,summary`；失败时有 `failure:{phase,error_code,message,last_successful_step}`。逐步记录调用参数、响应摘要/哈希、耗时和失败码；图片与分块正文仅保存摘要，不把大块正文写进报告。具名写入专项在 `named_gateway.named_full.full_access_write` 中汇总 `tool,request_id,prompt_id,before_version,after_version,state,receipt,request_status,sqlite_receipt,sqlite_public_request_key_absent,sqlite_prompt,passed`；同步核对 `steps` 中同一 request ID 的 `named_full_write,named_full_receipt_readback,named_full_prompt_readback`。

该 runner 检查真实 MCP `tools/list` 与注册目录、诊断与合成自检、合成题目保存及版本/回执/SQLite/MCP 查回、完整档案、原图分块 SHA、范围导出和分块校验、真实无效范围错误、失败写回执查询及伪成功拒绝门。报告中的 `checks` 和 `steps` 是此次隔离运行证据；若 `cleanup.service_stopped` 或 `cleanup.workspace_removed` 为 false，不把运行报告标作通过，并按 `failure` 留下的路径和错误处理。不要手动清理仍可能服务使用的临时目录。

本次 batch-III 的最终隔离验收报告实际返回 `status="passed"`、45 步和 13 项检查；本地 stdio、`tools/list` 与目录均为 120 工具，schema hash 相同。具名 full host 的 `set_prompt_state` 实际把合成题从 version 2 更新为 version 3；同一公开 request ID 的 write receipt 与 `get_request_status` 均为 committed，`get_prompt` 和隔离 SQLite 查回 `in_progress` version 3。受限 reader 只能读摘要，完整细节返回 `forbidden`，隐藏的 `get_full_record` 返回 `not_found`。报告还记录本地 stdio 写入回执、4 段原图和 5 段导出校验、无效时间范围错误、失败写入查回与伪成功拒绝。清理结果 `service_stopped=true,workspace_removed=true`。最新报告与独立证据在本次 RUN 的 `evidence/item17-host-acceptance-report-v2.json` 与 `evidence/d_item17_host-acceptance-batch-iii.txt`；报告 SHA-256 `55487d81d183788c715407c331ed45cb8295dccf6a5e49b928850f289a07a7a9`，证据文本 SHA-256 `60471fe64b8b334905a3fb61f748e361e430466a0668e96653289700f310e887`。这不是 CuteDuck/QQ 实聊结果。

外部客户端验收另由用户本人在真实 CuteDuck/QQ 内执行并确认。runner 的 `passed` 不能推导出真实客户端已经接入、读取到同一目录或完成用户侧操作。

## 规格 19｜比较目录版本与迁移影响

`compare_mcp_directories` 是只读工具，必填 `baseline`，可选 `limit`（1–100，默认 100）和非负 `offset`（默认 0）。baseline 可直接使用完整 `shuxu-mcp-tool-catalog/v1` 生成快照，也可使用完整且未筛选的 `get_mcp_directory` 快照。catalog 的 `tool_count` 必须等于 `tools` 长度；directory 必须从 offset 0 开始，`complete=true`，`total_count=tools.length=capabilities.tool_count`。若目录分页过，先取齐所有页并保存同一轮返回的完整工具清单；不能比较截断页或把不同版本的页拼接。

调用形状为 `compare_mcp_directories({"baseline": <完整快照对象>, "limit": 100, "offset": 0})`。正式调用应将完整生成 catalog 文件内容作为 `baseline`，或保存同一具名 host/profile 的完整 `get_mcp_directory` data；不能只提交快照头部或挑选部分工具。catalog 快照没有旧授权身份；比较会报告 `permission_delta.comparable=false`，不能由 schema 相同推断旧宿主仍有相同权限。

结果含 `compatible,compatibility,compatibility_reasons,baseline,current,version_changes,permission_delta,counts,changes,offset,limit,complete,next_offset`。`counts` 统计完整差异，`changes` 是当前页；变更项列 `tool,change,compatibility,schema_changes,permission,migration,baseline_tool,current_tool`。schema 差异按路径记录 before/after/impact；枚举变化还含 `added_values,removed_values`。`migration` 只带 schema 中明确的默认值、`missing_required_fields,new_required_fields,added_optional_fields,reuse_previous_arguments,next_step`，不伪造对象 ID。调用前仍要核对 `permission.currently_callable` 和用户意图；新必填字段缺失时先补齐业务事实，不能直接执行 write。分页只用原 baseline/limit 并设 `offset=next_offset`。

只有完整目录来源于同一 `host_id` 和 `profile` 时才比较权限差值；授权修订会作为版本差异显示。`compatible=true` 表示同一宿主可见范围内没有破坏性协议、目录 schema、工具 schema 或已知可调用权限损失；`false` 表示检测到破坏性变化；`null` 表示权限不可比。权限不可比时 `compatibility` 为 `schema_backward_compatible_permissions_unknown`，不得将“schema 兼容”说成“部署权限兼容”。schema/版本一致也不代表正在运行的实例已经重启，仍需单独核对握手实例和真实目录。

baseline 结构缺失、工具项无 `name/inputSchema` 或重复工具名为 `invalid_baseline`；分页不完整、offset 非零或计数不匹配为 `baseline_incomplete`；未获当前工具授权为 `forbidden`。收到前两类错误时重新保存一份完整单轮快照；`forbidden` 时请求管理员检查当前 host scope，不换身份重试。I→II 已存快照比较的核对结果为 20 个新增工具、0 个删除，8 个工具 schema 变化且 required 集合未变；原快照保持在 RUN 证据中，未覆盖。

## 规格 20｜安全排空并重启服务

本能力由三个具名宿主管理工具组成：

| 工具 | 完整 input 参数 | 结构化结果与用途 |
| --- | --- | --- |
| `restart_service` | 必填 `request_id,operation_id`；可选 `timeout_seconds`，整数 1–3600，运行时默认 120 秒。两个 ID 都是非空 1–200 字符。 | 受理即返回 `operation_id,status:"accepted",attempt,request_id,query_tool:"get_service_restart",retry_with_same_operation_id:true`。此项只表示操作已持久登记。 |
| `get_service_restart` | 必填 `operation_id`（非空 1–200 字符）。 | 查询同一 actor 下的持久操作状态与查回依据，包括 `status,attempt,request_id,old_instance,new_instance,retryable,error,active_work,attempts,completion_receipt`。 |
| `retry_service_restart` | 必填新的 `request_id` 和原 `operation_id`；可选 `timeout_seconds`，整数 1–3600，默认 120 秒。 | 仅对 `status:"failed"` 且 `retryable:true` 的原操作重试；沿用原 `operation_id` 并用新 request ID，再返回 `accepted`。 |

状态会从 `accepted` 进入 `draining`、`stopping`、`starting`、`verifying`，最终为 `completed` 或 `failed`。排空中的 `active_work` 含 `active_dispatches,active_writes,active_actions,jobs`。首调和重试的顶层 MCP `status:"ok"` 或 `data.status:"accepted"` 都不代表新实例已经就绪；用 `get_service_restart({"operation_id":"<原 operation_id>"})` 轮询查回。

只有 `data.status:"completed"` 且 `completion_receipt` 同时满足 `business_receipt_persisted=true`、`health_verified=true`、`handshake_verified=true`，`old_instance.instance_id` 与 `new_instance.instance_id` 不同，并且新实例的 `service_version` 与 handshake 相同，才能报告重启完成。完成回执还含 `operation_id,status,request_id,request_action,old_instance,new_instance,completed_at`。查询 `data.status:"failed"` 时读取 `error:{code,message}`、`retryable` 和 `active_work`；它是持久失败状态，不是一次无法判断的工具超时。

`drain_timeout` 表示排空时限内仍有在途调用/写入/job；操作会进入失败状态，scheduler 恢复正常接收写入。`helper_start_failed`、`old_instance_exit_timeout`、`restart_verification_failed` 和 `request_receipt_missing` 也会写入失败回执。只有失败状态提供 `retryable=true` 且用户仍要求这次重启时，使用同一 `operation_id` 和新 `request_id` 调 `retry_service_restart`；不创建第二个并行 operation。`get_service_restart` 对未知或当前身份不可见 operation 返回 `not_found`；重复创建 ID 返回 `operation_id_conflict`；维护门已被其他 operation 占用时为 `maintenance_in_progress`；非失败/不可重试状态调用 retry 返回 `invalid_state`；当前 BookService 未关联可验证本机实例为 `restart_unavailable`；未认证宿主为 `forbidden`。

## 1.3.0 → 1.4.0 兼容处理与错误字典

1.4.0 保留 MCP protocol 1 和旧 `status/data/error/envelope` 外形；握手顶层字段仍为 `protocol_version`、`service_version`、`instance_id`、`directory_id`、`authorization_revision`、`directory_etag` 和 `tool_directory`。身份与权限摘要在 `get_mcp_directory`，不在握手顶层。目录兼容保留 `tools,count`，并提供身份、能力、类别、工具风险/回执标记和分页字段。当前源注册清单为 120 项，具名宿主只看到权限过滤后的目录；规格 17 的本机验收 runner 是 CLI，不计入 MCP 工具数。

I、II、III 历史快照分别记录 96、116、120 项。它们都可能显示 `service_version="1.4.0"`，所以版本号或 `directory_version` 不能证明工具目录相同。升级时比较实际 `tools/list`、`total_count`、工具名与 inputSchema，再核对 `instance_id`、目录摘要和实际权限；具备完整旧快照时可调用 `compare_mcp_directories` 检查 schema、协议、目录版本和可比较的权限变化。既有 `get_workflow_guide:01–20` 的路由 ID 与 `ROUTES.tools` 含义保持不变；新能力另列在产品规格能力映射中，不改变旧 planner 路由的必调工具。产品编号也不等同于旧路由 ID。

已保存的普通具名 `tool:<name>` scope 不会自动升级为唯一全权限 `tool:*`。规格 18 完整读取工具和规格 16 专题完整视图要求 full actor；普通宿主的历史、材料字段仍按各自范围过滤。规格 17 的 CLI 只在本机临时目录和回环服务上验收当前实现；真实 CuteDuck/QQ 仍需用户侧实际操作。规格 19 提供目录比较与 schema 迁移建议，权限只有同一 host/profile 完整目录才可比。规格 20 的重启工具先受理，再经排空、独立进程启动、健康与握手检查、原请求回执查回后才报告 `completed`。切换服务实例或授权修订后重新握手并读取实际目录；丢弃绑定旧目录/权限的游标和待续状态，按用户原请求重新定位。服务版本号相同也不证明进程已重启或拥有同一目录。

主要错误码和动作如下；工具未列出的错误仍以原始 `error.code`、`message` 和 `details` 为准：

| 错误码 | 场景 | 宿主下一步 |
| --- | --- | --- |
| `forbidden` | 非 full actor 调用完整档案/原图/导出，或普通 host 请求 `all_hosts` | 停止，不换身份；由可信管理员在明确授权后调整，或只使用本 host 可见能力。 |
| `not_found` | 精确档案/主题/图片/导出引用不存在；检查点不可见；更正原回执不可定位 | 核对用户给出的 ID、来源和当前 host 范围；不得跨库寻找替代项。 |
| `invalid_params` | schema 类型/范围、倒置日期、超出图像/导出大小的 offset、错误 cursor 混传 | 按 `details` 和当前 schema 修正已确定的参数；若含义取舍依赖用户，先澄清。 |
| `needs_clarification` | 续接有多个检查点、写作计划目标不唯一、原回执不明确 | 展示服务返回的候选与区分字段，只问用户选择。 |
| `cursor_forbidden` | 游标属于另一 host/profile/revision/scope | 丢弃游标，按当前身份重做同一查询。 |
| `cursor_invalid` / `cursor_expired` / `cursor_stale` | 签名/范围无效、15 分钟过期、数据库安全快照变化 | 丢弃旧页，按原始筛选重新开始完整读取。 |
| `idempotency_conflict` | 已用同一导出 request ID 请求不同范围 | 停止复用；新导出必须使用新的 request ID，并只按用户确定范围执行。 |
| `expired` | 完整导出引用一小时后到期 | 用新 request ID 重新导出用户仍需的范围。 |
| `version_conflict` | 题目版本变化、导出文件校验不符、原更正已撤销或后续状态已改变 | 读当前值/状态并展示差异；不以旧版本覆盖。 |
| `service_unavailable` / `submission_unknown` | 握手、服务或写入结果无法确认 | 查原 request ID；只按 `dispatched`、receipt 和状态查询给结论，不换 ID 猜重做。 |
| `no_candidate` | `next_prompt` / `pick_unfinished_prompt` 取题或续接池为空 | 只展示实际过滤条件和错误 details 提供的可执行下一调用；用户选择前不放宽。只读 `list_unfinished_prompts` 空池是 `ok` 且空 `items`。 |
| `invalid_baseline` | 目录基线缺少工具项/name/schema，或工具名重复 | 从单次完整目录/生成 catalog 重新保存；不手工补造旧 schema。 |
| `baseline_incomplete` | baseline 是截断页、offset 非 0 或计数与 tools/capabilities 不一致 | 按旧目录同一 `directory_id/identity` 收齐分页，再重新比较。 |
| `drain_timeout` | 服务重启等待在途调用/写入/异步作业超时；位于 `get_service_restart.data.error` | 先查同一 operation ID 的 `active_work` 与 `retryable`；不要另开维护操作。 |
| `helper_start_failed` / `old_instance_exit_timeout` | 独立重启进程未启动，或旧实例未按期退出 | 读取状态和完整错误；仅 `retryable=true` 且仍有用户重启指令时，用原 operation ID、新 request ID 调重试。 |
| `restart_verification_failed` / `request_receipt_missing` | 新实例健康/握手或原业务回执查回失败 | 继续查询原 operation ID，按 `error` 与实例状态报告；未有 completed 回执前不宣称成功。 |
| `invalid_state` | 已完成或当前不可重试的维护操作被再次 retry | 读取当前维护状态；不要换 operation ID 重建相同请求。 |
| `operation_id_conflict` | 同一 actor 已使用该 operation ID | 查原 operation ID 的持久状态；新操作需新的唯一 ID 并依据用户明确指令。 |
| `maintenance_in_progress` | 另一个维护操作已经占用写入排空门 | 查询该 actor 的现有 operation；等它成为 `completed` 或 `failed` 后再决定，不并行提交。 |
| `privacy_rejected` / `privacy_scope_required` | 检查点、题目关系、说法证据或问法理由触发隐私边界 | 按原错误停止，不换写法绕过；`privacy_scope_required` details 只列类别、不回显原文。仅在来源端去标识或确认真实非咨询范围后，按用户新指令重新判断。 |

更正 `correct_prompt_status` 必填 `request_id`、`conversation_id`；可选 `source_conversation_id`、`target_request_id`、`prompt_id`、`state`、`snoozed_until` 和 `reason`（1–240 字符），并至少给状态或暂缓时间之一。成功结果中有 `target_request_id`、`original_receipt`、`prompt_id`、`before`、`after`、`correction_receipt`、`undo_available`、`next_call`。只在 `correction_receipt.committed=true` 时报告更正提交；再按 `next_call` 查询当前状态。没有明确原回执时 `needs_clarification`，原回执不在本会话/权限范围为 `not_found`，回执和当前状态冲突为 `version_conflict`。完整字段与参数约束见生成附录。

## 选择接入方式

### 本机可信宿主：标准 stdio

本机 MCP 配置示例见 [`mcp-registration.example.json`](../config/mcp-registration.example.json)。按安装位置调整 `shuxu/mcp_server.py` 的绝对路径。标准 stdio 进程作为 `legacy_stdio` 本机宿主运行，拥有本机调用权限；只用于与书序数据处在同一可信用户边界内的宿主，不要把它转给不受信任的远端 AI。

它会按需启动书序 HTTP 服务。核心服务只监听 `127.0.0.1`，配置文件不需要 API 凭据。MCP 的标准输入和输出仅承载协议消息，诊断写入标准错误。

### 具名外部宿主：显式启用 Gateway

远端宿主使用具名授权和受限目录。部署者先启动书序服务并在本机数据库上登记宿主；再显式启动 Gateway。仅编写本文档或运行标准 stdio 不会创建宿主、开放端口或启用远程连接。

先初始化数据库，再登记宿主。命令中的 ID、profile 和 scope 是示例值；`host create` 会以不回显的提示读取由可信操作者提供的 bearer，至少 32 UTF-8 字节。请通过部署环境的密钥管理注入同一凭据，不要把它写入仓库、普通 MCP 配置、日志或聊天。

以下 `py -m shuxu...` 示例从项目根目录执行，或确保 `shuxu` 包已安装到对应 Python 环境。若设置了 `SHUXU_DATA_DIR`，管理员命令中的数据库路径也要改为该目录下的 `shuxu.sqlite3`。

```powershell
py -3.12 -X utf8 -B -m shuxu.server start
py -3.12 -X utf8 -B -m shuxu.mcp24_admin --database "I:\data\shuxu\data\shuxu.sqlite3" host create --host-id "assistant-example" --profile "reader" --scopes "tool:get_mcp_directory,tool:get_thought_context,tool:search_indexed_library"
```

scope 按最小需要配置。工具 scope 格式为 `tool:<工具名>`；可进一步授予具体资料范围，如 `prompt:<题目ID>`、`material:<资料ID>:<字段>` 和 `handoff:<目标宿主ID>`。本地管理员可用 `host configure` 更新 profile/scope，用 `host revoke` 撤销宿主：

```powershell
py -3.12 -X utf8 -B -m shuxu.mcp24_admin --database "I:\data\shuxu\data\shuxu.sqlite3" host configure --host-id "assistant-example" --profile "reader" --scopes "tool:get_mcp_directory,tool:search_indexed_library"
py -3.12 -X utf8 -B -m shuxu.mcp24_admin --database "I:\data\shuxu\data\shuxu.sqlite3" host revoke --host-id "assistant-example"
```

1.4.0 全权限具名宿主使用唯一 scope `tool:*`，不另设内容 scope；只有当前版本的管理命令接受该 grant，且目录明确报告 `full_access=true` 时才按此方式登记。`profile` 是部署者自选的标识字符串（示例 `operator`），本身不改变授权。可信操作员在完成本地书库准备后可运行：

```powershell
py -3.12 -X utf8 -B -m shuxu.mcp24_admin --database "<当前书序库路径>" host create --host-id "assistant-full" --profile "operator" --scopes "tool:*"
```

CLI 会隐藏提示输入由操作员提供的 bearer，不生成或回显凭据；create 回执包含 `host_id`、`profile`、`scopes`、`authorization_revision`、`enabled`。需要变更已有身份时，使用 `host configure` 和相同 `tool:*` scope；权限变化会提升授权修订并使旧会话状态失效。只有可信且确需全库能力的宿主才应配置此 grant；bearer 由密钥管理注入，不放进普通 MCP 配置、提示词或聊天。命令示例不代表某实例已经登记或接受 `tool:*`。

权限或 profile 变更会提升授权修订号，使旧握手身份、搜索游标、预览绑定和一次性许可失效。具名宿主不能使用 `legacy-stdio` 或 `local-web` 保留 ID。

服务端的 Gateway 默认端口为 `17832`，并要求本机书序服务已运行。只供同机使用时可以绑定回环 IP：

```powershell
py -3.12 -X utf8 -B -m shuxu.mcp24_transport gateway --host 127.0.0.1 --port 17832
```

需要跨机器连接时，绑定部署机器上的具体网卡 IP，并同时提供 TLS 证书和私钥；非回环监听没有 TLS 会被拒绝，TLS 最低为 1.2。防火墙只放行预期的宿主来源。客户端 Gateway URL 必须是 origin，不能附带路径、查询串或凭据；非回环连接必须使用 HTTPS。

在外部 MCP 宿主的进程环境中注入以下值，然后以 stdio relay 启动客户端。尖括号内容须替换为实际部署值；bearer 由密钥管理在运行时注入，不要提交包含真实凭据的 JSON：

```powershell
$env:SHUXU_GATEWAY_URL = "https://<gateway-host>:17832"
$env:SHUXU_GATEWAY_HOST_ID = "assistant-example"
$env:SHUXU_GATEWAY_BEARER = "<由密钥管理在运行时注入>"
py -3.12 -X utf8 -B -m shuxu.mcp24_transport stdio
```

relay 首先执行握手，再以 MCP `tools/list` 返回该具名宿主可见的目录。核心 HTTP 服务仍只绑定回环；Gateway 仅在显式运行时转发具名请求，并使用内部 bridge 凭据连接本机服务。bridge 凭据不会作为远端配置字段返回。

## 握手、目录和结果

握手地址为 `GET /api/mcp24/handshake`。具名请求必须同时提供 `X-Shuxu-Host-ID` 与 `Authorization: Bearer <凭据>`；只提供其一会被拒绝。响应保留 `status/data/error`，并在 `envelope` 和 `data` 中提供 `protocol_version`、`service_version`、`instance_id`、`directory_id`、`authorization_revision`、`directory_etag`；`data.tool_directory` 是该身份过滤后的完整注册定义，含工具签名及目录标注。握手本身不含 `host_id`、`profile` 或 `full_access`；这些身份/权限字段由 `get_mcp_directory` 返回。当前仓库代码定义协议版本 `1`、服务版本 `1.4.0`；连接实际版本、目录摘要和工具清单须以此次服务进程握手为准。目录 ID 不含本机路径。

通过 Gateway 发出的工具请求会固定握手得到的实例 ID。实例改变、版本不兼容或授权修订变化时，先重新握手并检查目录，再继续请求；不要绕过冲突盲目重试。`diagnose_mcp_compatibility` 可核对版本和实例，`get_mcp_directory` 可按名称或说明搜索真实目录。

MCP 调用结果通过 `structuredContent` 返回结构化对象，同时在文本内容中提供同一 JSON。兼容旧宿主的顶层 `status`、`data`、`error` 保留；`envelope` 附加服务身份和回执。`envelope.receipt` 含 `request_id`、`action`、`state`、`committed`、`retry`、`next_action`。只在回执明确提交时报告写入成功；`submission_unknown` 或超时表示结果未确认，使用原 request ID 和原参数查询或重试，不要换 ID 重新提交。

时间戳必须带时区。截图使用工具声明的图片类型和 base64 字节；宿主不传本机路径，也不扫描其他附件。调用计划和自检都是只读；`run_mcp_self_check` 返回合成验证向量，不会写真实业务数据。

## 1.3.0 基线功能调用索引（沿用）

下表保留原有 1.3.0 的 24 项功能索引，用于说明此前接口范围；它与产品规格的 20 项编号不同。本批没有重新逐项验证此表，其余未列入本批的产品功能仍按既有指南和当前实际目录判断。

| 编号 | 调用路由或接口 | 作用 |
| --- | --- | --- |
| 01 | `plan_mcp_calls` | 按旧场景生成有序只读计划，列工具、参数、依赖、缺参和权限。 |
| 02 | `get_mcp_directory`、`tools/list` | 按当前具名宿主权限提供可见工具目录。 |
| 03 | 所有工具的 `status/data/error/envelope` | 保留旧结果字段，并附统一结构化回执。 |
| 04 | `GET /api/mcp24/handshake`、`diagnose_mcp_compatibility` | 协商协议、服务、实例、目录和授权修订。 |
| 05 | `run_mcp_self_check` | 让宿主验证发现、结构化结果、时区、附件字段和歧义策略；只用合成数据。 |
| 06 | `get_request_status` | 按 request ID 和当前 actor 查询已完成/提交、失败或未知状态，不返回原始结果正文。 |
| 07 | `dry_run_actions` | 显示动作、对象引用和当前版本，可创建不超过 5 分钟的绑定预览。 |
| 08 | `continue_pending_call` | 跨轮补参，默认 30 分钟过期；检查 actor、授权修订、参数冲突和版本，只返回一次待确认的精确下一调用。 |
| 09 | `mcp24_admin host create/configure/revoke` | 本机管理员建立最小工具/资料授权，调整或撤销具名宿主。 |
| 10 | `share_excerpt`、`mcp24_admin excerpt grant` | 按精确资料、字段和片段申请一次性许可；管理员可批准最多 5 分钟的确切请求。 |
| 11 | `preview_backup` → `restore_backup`；批量动作使用 `dry_run_actions` | 整库恢复先展示备份指纹并绑定预览；高影响批量删除先核对具体目标和版本。 |
| 12 | `create_handoff`、`receive_handoff` | 向一个已授权宿主交接所选题目和最多五个字段，限时五分钟且只能接收一次。 |
| 13 | `create_export_artifact_reference`、`get_export_artifact_info`、`read_export_artifact_chunk` | 对已完成的本机导出创建 60–3600 秒的宿主绑定引用，再按大小和 SHA-256 分块取回；单块最多 64 KiB，不接受路径。 |
| 14 | `start_attachment_upload`、`put_attachment_upload_chunk`、`get_attachment_upload_status`、`finish_attachment_upload`、`cancel_attachment_upload` | 分块续传用户指定图片；上传期限为 60 秒至 24 小时，校验分段和整图 SHA-256 后才写入截图业务记录，分块上限 64 KiB。 |
| 15 | `mcp24_transport gateway`、`mcp24_transport stdio` | 部署者显式启动受 TLS/具名凭据约束的异机网桥和 stdio relay。默认没有远端宿主。 |
| 16 | `X-Shuxu-Upstream-Message-ID`、`X-Shuxu-Operation-Slot` | 具名写入按上游消息和操作槽去重；同一消息槽配不同动作或参数会冲突，未决请求不会静默重放。 |
| 17 | `get_thought_context` | 按 conversation ID 或题目 ID 读取限预算的写作上下文，不包含私密正文。 |
| 18 | `search_indexed_library`、`set_topic_aliases` | 在本机索引中检索并返回原词、主题别名或近似匹配说明；别名必须由用户明确提供，结果仍受 actor 范围约束。 |
| 19 | `search_indexed_library(cursor=...)` | 续页只传 cursor；游标绑定宿主、授权修订、筛选条件和索引快照，有效期 15 分钟。 |
| 20 | 权限/任务 sidecar 与恢复调度器 | 授权和运行账本独立于业务备份；恢复独占调度并在完成后重建读取索引。 |
| 21 | `submit_async_job`、`get_async_job`、`cancel_async_job`、`resume_async_job` | 只提交备份、导出和批量动作白名单；按 actor 查进度，排队/运行时可请求取消，提交阶段不可取消。 |
| 22 | HTTP `ETag` / `If-None-Match` | HTTP 客户端可为偏好、题目列表、上下文和索引查询使用按 actor 与版本计算的条件缓存。 |
| 23 | `get_request_diagnostics` | 读取当前 actor 的阶段耗时和脱敏错误类别，不保存题文、凭据或请求正文。 |
| 24 | `preview_retention_cleanup`、`apply_retention_cleanup` | 预览 actor 范围内的候选与保护原因；只按用户确认的同一指纹计划清理。 |

## 私密内容与高影响写入

在旧版及普通具名授权下，宿主不能用 `include_private: true` 读取私密正文；它可以返回定位信息。旧版完整读取须由用户明确选择，精确片段按 `share_excerpt` 流程逐项授权。1.4.0 的 `tool:*` 全权限具名授权允许全工具与完整内容访问，但不表示某次用户请求要求读取全库；此行为只有在本机配置和当前目录确实显示该授权后适用，版本号本身不是许可。分享片段时只指定一种范围：`start` 加 `length`、唯一 `selection`，或 `last_sentence: true`。如果本机尚无精确许可，调用会登记一条待批请求；本机管理员用返回的 host ID 与同一 upstream message ID 核对请求后，再运行命令并交互确认：

```powershell
py -3.12 -X utf8 -B -m shuxu.mcp24_admin --database "I:\data\shuxu\data\shuxu.sqlite3" excerpt grant --host-id "assistant-example" --upstream-message-id "<原请求ID>" --ttl-seconds 300
```

许可绑定当前授权修订、对象、字段、片段和参数 hash，只能使用一次；申请本身十分钟过期，许可最多五分钟且不会晚于申请过期时间。

整库恢复先调用 `preview_backup` 并展示备份 ID、指纹和恢复影响，用户明确确认后才调用 `restore_backup`，且必须传回预览中的原指纹。多项高影响删除先用 `dry_run_actions` 展示目标和版本；设置 `bind_preview: true` 后，精确动作、当前版本、宿主和授权修订都必须匹配，预览最多五分钟有效。任何对象或授权变化都应重新预览；预览工具不会修改业务记录。

单次写入使用稳定的 `request_id`，重试时保留相同参数。远端宿主还可传 `X-Shuxu-Upstream-Message-ID`（最多 200 字符）及 `X-Shuxu-Operation-Slot`（最多 128 字符）区分上游消息中的写操作。同一消息槽的相同操作可重放既有回执；同一槽参数或动作不同会返回冲突；原请求仍未决时返回未知状态，不能当作失败后换 ID 再写。

## 查询续页、作业和保留

索引搜索返回 `match_explanations`，近似命中会明确标为 approximate；需要更多结果时只传原返回的 `cursor`，不要改 query 或筛选。游标在授权修订/范围改变、资料更正或删除、快照安全条件变化及 15 分钟过期后失效。遇到 `cursor_stale`、`cursor_expired` 或 `cursor_forbidden`，按当前权限重新执行原查询。

异步作业只接收 `create_backup`、`export_thought_bundle`、`apply_actions`。`cancel_async_job` 对排队作业会取消，对运行作业会请求在检查点取消；已进入提交阶段返回 `cannot_cancel_committing`。进程重启后未完成任务标为 interrupted，必须经 `resume_async_job` 明确恢复，不会自动重放。任务状态及诊断按 actor 隔离；诊断只含阶段、耗时、状态和脱敏错误类别。

保留清理支持 `temporary_references`、`selection_history`、`undo_records`、`write_receipts`、`diagnostics`，可设统一天数或按类别覆盖（1–36500 天）。预览列出候选数量和受保护原因；当前/活动会话、未决请求和活动作业受到保护。确认后按同一计划指纹应用：写入回执可裁剪正文但保留已提交事实，任务可清理结果但保留状态，过期附件分段可清除内容但保留安全元信息。不要跳过预览、变更指纹或自动确认。

## 默认边界

- 默认安装只使用标准本机 stdio 与回环 HTTP；项目未替用户配置 CuteDuck 或其他真实外部 AI 宿主。
- 异机接入必须由部署者建立具名宿主、注入实际 bearer，并显式运行 Gateway；非回环连接必须使用 HTTPS/TLS，并限制网络来源。
- 不向 AI 提供本机数据库路径、bridge token 或不必要的私密字段。导出文件通过限时 artifact ID 取回，不把本机路径当作公网下载地址。
- 用户取消任务后停止后续调用；未确认的写入按原 request ID 查询，不以新 ID 猜测重做。

<!-- BEGIN GENERATED MCP TOOL SCHEMAS -->
<!-- Generated from shuxu.book.TOOL_SPECS; do not edit this section by hand. -->
## Registered tool schemas

Registry snapshot: service `1.4.0`, protocol `1`, 120 registered tools and 20 route scenarios.
This is the complete registered source definition set. Compare it with the running instance handshake and actual `tools/list`; a named host's list is filtered by that host's grants.
MCP currently registers input schemas here; it does not publish result schemas. A schema keyword `default` is included if registered; runtime defaults not encoded in schema are documented with the corresponding handler behavior. Result and error behavior is documented in the relevant guide sections.
Regenerate this section after changing registered tools with `py -3.12 -B docs/generate_mcp_api_reference.py --update-guide`; this reads the in-memory registry and does not call a running service.

### Legacy workflow routes

| Scenario | User intent | Tools | Guidance |
| --- | --- | --- | --- |
| 01 | 刚才那题、上一题 | `resolve_reference`, `bind_current_prompt` | 优先解析当前会话已绑定的命题；有歧义时给候选，确认后才写。 |
| 02 | 接着上次的思考 | `save_thought_checkpoint`, `continue_thought` | 明确保存当前线索和未答问题；跨天按来源会话、命题、日期或关键词找回，歧义时先选定一条。 |
| 03 | 一句话做几件事 | `apply_actions` | 按顺序提交白名单动作，允许引用前一步创建的 ID。任一步失败整组回滚。 |
| 04 | 撤销刚才的记录操作 | `undo_action` | 按原回执撤销；目标后来已被改过时返回冲突。 |
| 05 | 从暂缓里捞回一题 | `pick_due_prompt` | 到期后读取暂缓题；若需要恢复已保存的线索，再按实际返回的续接卡 ID 调 continue_thought。 |
| 06 | 逐轮围绕同一题发问 | `guide_thought` | 首轮用 mode=start；用户回答后再用 mode=continue，每轮只问一个问题。 |
| 07 | 五分钟、轻一点、避开工作 | `next_prompt` | 将本轮明确的分钟数、深度和排除方向传给 next_prompt；临时条件不保存为长期偏好。 |
| 08 | 近期避免重复 | `next_prompt` | 按实际近期选题历史避开重复；若无候选，解释筛选结果并询问是否放宽，不自行放宽。 |
| 09 | 用已有资料拟题 | `search_library`, `draft_from_material` | 先用搜索定位资料，按精确 ID 和用户明确授权拟一条草题；没有明确保存要求时不写入题库。 |
| 10 | 为什么选这一题 | `explain_prompt` | 以 decision_id 读取实际筛选、权重、反馈或素材来源。 |
| 11 | 找我以前存过的东西 | `search_library` | 按关键词及可选类型、方向或主题一次查所有资料。 |
| 12 | 原样存我说的几句话 | `capture_quotes` | 给出已获准的原文片段和来源标识，批量保存并返回关联 ID。 |
| 13 | 把当前聊天图片和批注存入本机 | `save_chat_screenshot` | 仅传用户选定附件的字节及类型，截图内容不出库到 MCP。 |
| 14 | 同一链接已经收过就追加批注 | `merge_clipping` | 按规范化原始链接查重，同源追加，不覆盖既有批注。 |
| 15 | 核对哪些说法有出处 | `audit_sources`, `remember_claim_sources` | 按说法和剪藏明确记录出处，区分有关来源与已经核实。 |
| 16 | 把这些材料整理成专题 | `search_library`, `save_topic_project`, `get_topic_project` | 先找资料或用用户指定 ID，按给定次序保存专题清单。 |
| 17 | 回顾这段时间的主题 | `review_topics` | 以实际活动、写作状态和保存内容统计，同时交代统计口径。 |
| 18 | 打包为 Markdown | `export_thought_bundle` | 只导出明确选中的资料，返回本机路径和文件凭据。 |
| 19 | 备份或恢复 | `create_backup`, `preview_backup`, `restore_backup` | 创建后返回备份 ID；恢复前展示预览，再用预览哈希提交。 |
| 20 | 只给 AI 这几句私密内容 | `share_excerpt` | 精确指定资料及字段和片段范围；本轮明确授权后仅返回指定片段。 |

### Product-spec capability mapping

These product numbers are independent of the legacy workflow IDs above. Item 17 is a local CLI runner and does not appear in MCP tools/list. The map records the documented path; a running host may expose only a permission-filtered subset.

| Product item | Registered MCP tools in the documented path | Local runner |
| --- | --- | --- |
| 01 | `get_mcp_directory`, `diagnose_mcp_compatibility` | — |
| 02 | `plan_mcp_calls`, `get_workflow_guide` | — |
| 03 | `save_thought_checkpoint`, `continue_thought`, `guide_thought` | — |
| 04 | `next_prompt` | — |
| 05 | `get_session_conditions`, `set_session_conditions`, `end_session_conditions`, `next_prompt` | — |
| 06 | `get_prompt_family`, `set_prompt_family_relation`, `next_prompt` | — |
| 07 | `pick_unfinished_prompt`, `list_unfinished_prompts` | — |
| 08 | `guide_thought`, `get_guide_history` | — |
| 09 | `set_question_preference`, `get_question_preferences`, `end_question_preference`, `guide_thought` | — |
| 10 | `correct_prompt_status`, `get_prompt` | — |
| 11 | `get_prompt_timeline` | — |
| 12 | `review_prompt_events` | — |
| 13 | `draft_prompt_from_materials` | — |
| 14 | `save_prompt_material_relations`, `remove_prompt_material_relation`, `read_entity_relations` | — |
| 15 | `write_claim_evidence`, `read_prompt_evidence` | — |
| 16 | `get_topic_project_progress` | — |
| 17 | — | `py -3.12 -X utf8 -B -m shuxu.mcp25_host_acceptance` |
| 18 | `get_full_record`, `list_full_records`, `get_attachment_chunk`, `export_full_library`, `get_full_export_info`, `read_full_export_chunk` | — |
| 19 | `compare_mcp_directories` | — |
| 20 | `restart_service`, `get_service_restart`, `retry_service_restart` | — |

### Full input schemas

#### `pick_prompt`

按明确范围和已确认偏好取一题。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "depth": {
      "type": "string",
      "enum": [
        "light",
        "medium",
        "deep"
      ]
    },
    "current_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_prompt`

按精确 ID 读取一条命题；仅在用户本轮明确授权对应私密片段时设置 include_private=true。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "include_private": {
      "type": "boolean",
      "description": "仅在用户本轮明确授权查看这条私密命题时设为 true。"
    },
    "detail": {
      "type": "string",
      "enum": [
        "summary",
        "full"
      ]
    }
  },
  "additionalProperties": false,
  "required": [
    "prompt_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `narrow_prompt`

从同题族中取一条更具体的命题；提供 conversation_id 时遵守该会话的有效选题条件。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "depth": {
      "type": "string",
      "enum": [
        "light",
        "medium",
        "deep"
      ]
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "prompt_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_angles`

按需返回一到两个命题切入点。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `draft_prompt`

把本轮明确给出的主题或线索拟成一条本机草题；仅在用户要求时另行保存。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "depth": {
      "type": "string",
      "enum": [
        "light",
        "medium",
        "deep"
      ]
    },
    "focus": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    }
  },
  "additionalProperties": false,
  "required": [
    "direction"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `check_prompt`

仅核对命题是否重复或近似，不返回旧题内容。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "text": {
      "type": "string",
      "minLength": 1,
      "maxLength": 1000
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "depth": {
      "type": "string",
      "enum": [
        "light",
        "medium",
        "deep"
      ]
    },
    "exclude_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "text"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `save_prompt`

保存一条命题；首次状态和暂缓可与保存同事务提交。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "text": {
      "type": "string",
      "minLength": 1,
      "maxLength": 1000
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "depth": {
      "type": "string",
      "enum": [
        "light",
        "medium",
        "deep"
      ]
    },
    "state": {
      "type": "string",
      "enum": [
        "want_to_write",
        "in_progress",
        "written"
      ]
    },
    "snoozed_until": {
      "type": "string",
      "format": "date-time",
      "minLength": 1
    },
    "parent_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "text",
    "direction"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `list_prompts`

按范围分页查看命题记录。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "state": {
      "type": "string",
      "enum": [
        "want_to_write",
        "in_progress",
        "written"
      ]
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `set_prompt_state`

更正命题的写作状态。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "state": {
      "type": [
        "string",
        "null"
      ],
      "enum": [
        "want_to_write",
        "in_progress",
        "written",
        null
      ]
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "reason": {
      "type": "string",
      "minLength": 1,
      "maxLength": 240
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "prompt_id",
    "state",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `snooze_prompt`

将命题暂缓到指定时间。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "snoozed_until": {
      "type": "string",
      "format": "date-time",
      "minLength": 1
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "reason": {
      "type": "string",
      "minLength": 1,
      "maxLength": 240
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "prompt_id",
    "snoozed_until",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `clear_snooze`

清除命题的暂缓期限。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "reason": {
      "type": "string",
      "minLength": 1,
      "maxLength": 240
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "prompt_id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_preferences`

查看方向和主题的长期与近期偏好。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_preference_reason`

仅在用户本轮明确询问时，按方向或主题和作用期查询调整理由。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "scope": {
      "type": "string",
      "enum": [
        "baseline",
        "recent"
      ]
    }
  },
  "additionalProperties": false,
  "required": [
    "scope"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `add_topic`

在指定方向下新增一个小主题。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "name": {
      "type": "string",
      "minLength": 1,
      "maxLength": 80
    },
    "parent_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "weight": {
      "type": "number",
      "minimum": 0
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "direction",
    "name",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `update_topic`

更正小主题名称、父主题或启用状态。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "topic_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "name": {
      "type": "string",
      "minLength": 1,
      "maxLength": 80
    },
    "parent_id": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "active": {
      "type": "boolean"
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "topic_id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `set_direction_weight`

设置方向的长期或近期相对权重。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "weight": {
      "type": "number",
      "minimum": 0
    },
    "scope": {
      "type": "string",
      "enum": [
        "baseline",
        "recent"
      ]
    },
    "expires_at": {
      "type": "string",
      "format": "date-time",
      "minLength": 1
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "reason": {
      "type": "string",
      "minLength": 1,
      "maxLength": 240
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "direction",
    "weight",
    "scope",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `set_topic_weight`

设置小主题的长期或近期相对权重。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "topic_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "weight": {
      "type": "number",
      "minimum": 0
    },
    "scope": {
      "type": "string",
      "enum": [
        "baseline",
        "recent"
      ]
    },
    "expires_at": {
      "type": "string",
      "format": "date-time",
      "minLength": 1
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "reason": {
      "type": "string",
      "minLength": 1,
      "maxLength": 240
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "topic_id",
    "weight",
    "scope",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `reset_weights`

将指定范围的权重恢复为默认值并清除近期覆盖。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "scope": {
      "type": "string",
      "enum": [
        "all",
        "directions",
        "topics"
      ]
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `end_preference`

结束方向或主题的近期权重调整。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "scope": {
      "type": "string",
      "enum": [
        "direction",
        "topic"
      ]
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "scope",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `record_feedback`

保存一条明确范围的反馈，影响该范围内的选题顺序，不改变权重。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "scope": {
      "type": "string",
      "enum": [
        "prompt",
        "direction",
        "topic"
      ]
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "feedback": {
      "type": "string",
      "enum": [
        "helpful",
        "not_helpful",
        "too_broad",
        "too_narrow",
        "not_relevant",
        "too_heavy",
        "too_academic"
      ]
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "scope",
    "feedback"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `list_feedback`

按本轮明确指定的命题、方向或主题，查看该范围最近的反馈。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 30
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `add_inspiration`

仅在用户主动要求时保存灵感。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": [
        "string",
        "null"
      ],
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics",
        null
      ]
    },
    "topic": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "kind": {
      "type": "string",
      "enum": [
        "thought",
        "quote",
        "link",
        "screenshot"
      ]
    },
    "text": {
      "type": "string",
      "maxLength": 500
    },
    "source": {
      "type": "string",
      "maxLength": 160
    },
    "url": {
      "type": "string",
      "maxLength": 2048
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "kind"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `search_inspirations`

按本轮指定线索查找灵感。MCP 查询须提供关键词、主题、命题或方向。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "maxLength": 160
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_inspiration`

按精确 ID 查看灵感。MCP 仅在本轮获准读取该条文字时设置 include_private。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "include_private": {
      "type": "boolean",
      "description": "仅在用户本轮明确授权查看这条私密命题时设为 true。"
    }
  },
  "additionalProperties": false,
  "required": [
    "id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `update_inspiration`

按明确 ID 和当前版本修改灵感。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "direction": {
      "type": [
        "string",
        "null"
      ],
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics",
        null
      ]
    },
    "topic": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "kind": {
      "type": "string",
      "enum": [
        "thought",
        "quote",
        "link",
        "screenshot"
      ]
    },
    "text": {
      "type": "string",
      "maxLength": 500
    },
    "source": {
      "type": "string",
      "maxLength": 160
    },
    "url": {
      "type": "string",
      "maxLength": 2048
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `remove_inspiration`

仅在用户要求时按 ID 和当前版本删除灵感。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `save_clipping`

仅在用户主动要求时保存社会话题剪藏。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": [
        "string",
        "null"
      ],
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics",
        null
      ]
    },
    "topic": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "title": {
      "type": "string",
      "maxLength": 160
    },
    "url": {
      "type": "string",
      "maxLength": 2048
    },
    "published_on": {
      "type": "string",
      "maxLength": 10
    },
    "source": {
      "type": "string",
      "maxLength": 160
    },
    "note": {
      "type": "string",
      "maxLength": 500
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "title",
    "url",
    "published_on",
    "source"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `list_clippings`

按本轮指定线索查找社会话题剪藏。MCP 查询须提供关键词、主题、命题或方向。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "maxLength": 160
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_clipping`

按精确 ID 查看社会话题剪藏。MCP 仅在本轮获准读取该条文字时设置 include_private。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "include_private": {
      "type": "boolean",
      "description": "仅在用户本轮明确授权查看这条私密命题时设为 true。"
    }
  },
  "additionalProperties": false,
  "required": [
    "id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `update_clipping`

按明确 ID 和当前版本修改社会话题剪藏。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "direction": {
      "type": [
        "string",
        "null"
      ],
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics",
        null
      ]
    },
    "topic": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "title": {
      "type": "string",
      "maxLength": 160
    },
    "url": {
      "type": "string",
      "maxLength": 2048
    },
    "published_on": {
      "type": "string",
      "maxLength": 10
    },
    "source": {
      "type": "string",
      "maxLength": 160
    },
    "note": {
      "type": "string",
      "maxLength": 500
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `remove_clipping`

仅在用户要求时按 ID 和当前版本删除社会话题剪藏。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `save_resume_card`

仅在用户主动要求时保存思考续接卡。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": [
        "string",
        "null"
      ],
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics",
        null
      ]
    },
    "topic": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "title": {
      "type": "string",
      "maxLength": 160
    },
    "note": {
      "type": "string",
      "maxLength": 500
    },
    "question": {
      "type": "string",
      "maxLength": 500
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "title",
    "note",
    "question"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `list_resume_cards`

按本轮指定线索查找思考续接卡。MCP 查询须提供关键词、主题、命题或方向。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "maxLength": 160
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_resume_card`

按精确 ID 查看思考续接卡。MCP 仅在本轮获准读取该条文字时设置 include_private。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "include_private": {
      "type": "boolean",
      "description": "仅在用户本轮明确授权查看这条私密命题时设为 true。"
    }
  },
  "additionalProperties": false,
  "required": [
    "id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `update_resume_card`

按明确 ID 和当前版本修改思考续接卡。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "direction": {
      "type": [
        "string",
        "null"
      ],
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics",
        null
      ]
    },
    "topic": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": [
        "string",
        "null"
      ],
      "minLength": 1,
      "maxLength": 200
    },
    "title": {
      "type": "string",
      "maxLength": 160
    },
    "note": {
      "type": "string",
      "maxLength": 500
    },
    "question": {
      "type": "string",
      "maxLength": 500
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `clear_resume_card`

仅在用户要求时按 ID 和当前版本删除思考续接卡。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_workflow_guide`

取得 20 项对话场景的工具路由、状态与宿主调用约定。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "scenario": {
      "type": "string",
      "enum": [
        "01",
        "02",
        "03",
        "04",
        "05",
        "06",
        "07",
        "08",
        "09",
        "10",
        "11",
        "12",
        "13",
        "14",
        "15",
        "16",
        "17",
        "18",
        "19",
        "20"
      ]
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `resolve_reference`

只读定位当前会话的这题、上一题或符合线索的历史题。歧义时返回候选与追问。

Route scenarios: 01

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "reference": {
      "type": "string",
      "enum": [
        "current",
        "previous"
      ]
    },
    "query": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "conversation_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `bind_current_prompt`

用户明确点名或已选择一道现有题时，绑定到当前会话；保存动作随后可引用它。

Route scenarios: 01

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "conversation_id",
    "request_id",
    "prompt_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `continue_thought`

恢复此前续接卡或已明确保存的思路检查点；可按题、旧会话、日期和线索查找。

Route scenarios: 02

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "card_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "checkpoint_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "source_conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "query": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "created_after": {
      "type": "string",
      "minLength": 1,
      "format": "date-time"
    },
    "created_before": {
      "type": "string",
      "minLength": 1,
      "format": "date-time"
    },
    "history_scope": {
      "type": "string",
      "enum": [
        "same_host",
        "all_hosts"
      ]
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "include_private": {
      "type": "boolean",
      "description": "仅在用户本轮明确授权查看这条私密命题时设为 true。"
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `save_thought_checkpoint`

明确保存当前思路的续接点，记录题目版本、来源会话、线索、未答问题和进度。

Route scenarios: 02

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "cue": {
      "type": "string",
      "minLength": 1,
      "maxLength": 500
    },
    "unanswered_question": {
      "type": "string",
      "minLength": 1,
      "maxLength": 500
    },
    "progress": {
      "type": "string",
      "minLength": 1,
      "maxLength": 500
    },
    "guide_step": {
      "type": "integer",
      "minimum": 0,
      "maximum": 3
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "conversation_id",
    "prompt_id",
    "expected_version",
    "cue",
    "unanswered_question",
    "progress",
    "guide_step"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `apply_actions`

用户一次提出多项已明确的业务写入时原子执行。步骤可使用当前题或前序创建的题。

Route scenarios: 03

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "actions": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "tool": {
            "type": "string",
            "enum": [
              "save_prompt",
              "set_prompt_state",
              "snooze_prompt",
              "clear_snooze",
              "add_inspiration",
              "update_inspiration",
              "remove_inspiration",
              "save_clipping",
              "update_clipping",
              "remove_clipping",
              "save_resume_card",
              "update_resume_card",
              "clear_resume_card",
              "add_topic",
              "update_topic",
              "set_direction_weight",
              "set_topic_weight",
              "end_preference",
              "reset_weights",
              "record_feedback"
            ]
          },
          "arguments": {
            "type": "object"
          },
          "use_current_prompt": {
            "type": "boolean"
          },
          "prompt_from_step": {
            "type": "integer",
            "minimum": 1,
            "maximum": 12
          }
        },
        "required": [
          "tool",
          "arguments"
        ],
        "additionalProperties": false
      },
      "minItems": 1,
      "maxItems": 12
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "actions"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `undo_action`

用户明确要求撤销时，按目标回执或当前会话最近可撤销动作处理。

Route scenarios: 04

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "target_request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `pick_due_prompt`

从当前已到期、未写、未重新暂缓的题中挑一题，并返回续接卡引用。

Route scenarios: 05

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "conversation_id",
    "request_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `guide_thought`

按指定或有效问法偏好逐步追问；每次返回一个问题，只保存用户明确给出的线索，不保存回答。

Route scenarios: 06

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "focus": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "mode": {
      "type": "string",
      "enum": [
        "start",
        "continue"
      ]
    },
    "question_mode": {
      "type": "string",
      "enum": [
        "experience",
        "contrast",
        "counterexample",
        "boundary",
        "evidence",
        "narrow"
      ]
    },
    "resume_checkpoint_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "history_scope": {
      "type": "string",
      "enum": [
        "same_host",
        "all_hosts"
      ]
    }
  },
  "additionalProperties": false,
  "required": [
    "conversation_id",
    "request_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `next_prompt`

推荐日常取题入口；一次一题，按单次条件、会话条件、长期偏好顺序应用，并可排除确认题族。

Route scenarios: 07, 08

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "depth": {
      "type": "string",
      "enum": [
        "light",
        "medium",
        "deep"
      ]
    },
    "minutes": {
      "type": "integer",
      "minimum": 1,
      "maximum": 240
    },
    "exclude_directions": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "self_exploration",
          "consultation_reflection",
          "social_topics"
        ]
      },
      "minItems": 1,
      "maxItems": 3
    },
    "avoid_days": {
      "type": "integer",
      "minimum": 0,
      "maximum": 90
    },
    "exclude_family_ids": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "minItems": 1,
      "maxItems": 20
    },
    "ignore_session_fields": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "direction",
          "topic",
          "depth",
          "minutes",
          "exclude_directions",
          "avoid_days"
        ]
      },
      "minItems": 1,
      "maxItems": 6
    }
  },
  "additionalProperties": false,
  "required": [
    "conversation_id",
    "request_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `draft_from_material`

用户已指定并授权一条资料时，据该资料拟一条草题。引用可核对的剪藏来源，不保存草题。

Route scenarios: 09

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "kind": {
      "type": "string",
      "enum": [
        "inspiration",
        "clipping",
        "resume"
      ]
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "depth": {
      "type": "string",
      "enum": [
        "light",
        "medium",
        "deep"
      ]
    },
    "include_private": {
      "type": "boolean",
      "description": "仅在用户本轮明确授权查看这条私密命题时设为 true。"
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "conversation_id",
    "kind",
    "id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `explain_prompt`

按真实 decision_id 读取实际出题依据；也可查当前会话最后一次选题。

Route scenarios: 10

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "decision_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "conversation_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `search_library`

按本轮关键词联合查命题、灵感、剪藏、续接卡。私密资料只给定位信息。

Route scenarios: 09, 11, 16

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "kinds": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "prompt",
          "inspiration",
          "clipping",
          "resume"
        ]
      },
      "minItems": 1,
      "maxItems": 4
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 12
    }
  },
  "additionalProperties": false,
  "required": [
    "query"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `capture_quotes`

按用户明确指定的逐字原话和来源批量收集灵感，返回每条回执。

Route scenarios: 12

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "quotes": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1,
        "maxLength": 500
      },
      "minItems": 1,
      "maxItems": 12
    },
    "source": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "quotes",
    "source"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `save_chat_screenshot`

接收用户明确选中的单张宿主附件，传类型及仅该图的 base64，本机保存；不接受路径。

Route scenarios: 13

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "image_type": {
      "type": "string",
      "enum": [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp"
      ]
    },
    "image_data": {
      "type": "string",
      "minLength": 1,
      "maxLength": 6990508
    },
    "note": {
      "type": "string",
      "minLength": 1,
      "maxLength": 500
    },
    "source": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "image_type",
    "image_data"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `merge_clipping`

同一来源链接去重；存在时只追加当前批注，新建时须给出处和日期。

Route scenarios: 14

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "url": {
      "type": "string",
      "minLength": 1,
      "maxLength": 2048
    },
    "title": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "source": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "published_on": {
      "type": "string",
      "minLength": 1,
      "maxLength": 10
    },
    "note": {
      "type": "string",
      "minLength": 1,
      "maxLength": 500
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "url"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `audit_sources`

按当前说法和剪藏记录给出处、日期、链接和缺口；不把关联当作事实已核实。

Route scenarios: 15

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "claims": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300
          },
          "clipping_ids": {
            "type": "array",
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 200
            },
            "minItems": 1,
            "maxItems": 8
          }
        },
        "required": [
          "text"
        ],
        "additionalProperties": false
      },
      "minItems": 1,
      "maxItems": 15
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `remember_claim_sources`

按用户明确指令，将说法与已选剪藏来源关联到一道题。

Route scenarios: 15

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "claims": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300
          },
          "clipping_ids": {
            "type": "array",
            "items": {
              "type": "string",
              "minLength": 1,
              "maxLength": 200
            },
            "minItems": 1,
            "maxItems": 8
          }
        },
        "required": [
          "text"
        ],
        "additionalProperties": false
      },
      "minItems": 1,
      "maxItems": 15
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "prompt_id",
    "claims"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `save_topic_project`

按给定顺序保存专题中的命题和素材引用。更新时须提供专题 ID 和当前版本。

Route scenarios: 16

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "title": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "kind": {
            "type": "string",
            "enum": [
              "prompt",
              "inspiration",
              "clipping",
              "resume"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          }
        },
        "required": [
          "kind",
          "id"
        ],
        "additionalProperties": false
      },
      "minItems": 1,
      "maxItems": 20
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "title",
    "items"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_topic_project`

按专题 ID 或精确标题读回有序引用。

Route scenarios: 16

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "title": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `review_topics`

汇总最近一段时间的选题、写过和未完成记录，输出可解释的计数。

Route scenarios: 17

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "days": {
      "type": "integer",
      "minimum": 1,
      "maximum": 366
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "days"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `export_thought_bundle`

把用户明确点名的资料导出为本机 Markdown；私密内容仅按每条精确片段授权导出。

Route scenarios: 18

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "title": {
      "type": "string",
      "minLength": 1,
      "maxLength": 120
    },
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "kind": {
            "type": "string",
            "enum": [
              "prompt",
              "inspiration",
              "clipping",
              "resume"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          }
        },
        "required": [
          "kind",
          "id"
        ],
        "additionalProperties": false
      },
      "minItems": 1,
      "maxItems": 20
    },
    "authorized_excerpts": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "kind": {
            "type": "string",
            "enum": [
              "prompt",
              "inspiration",
              "clipping",
              "resume"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "field": {
            "type": "string",
            "enum": [
              "title",
              "text",
              "note",
              "question",
              "source"
            ]
          },
          "start": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000
          },
          "length": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500
          }
        },
        "required": [
          "kind",
          "id",
          "field",
          "start",
          "length"
        ],
        "additionalProperties": false
      },
      "minItems": 1,
      "maxItems": 20
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "title",
    "items"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `create_backup`

用户要求备份时，将数据库与截图保存到固定本机备份目录。

Route scenarios: 19

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `preview_backup`

只读预览固定目录的一份备份，返回内容统计和恢复所需指纹。可按最近一份查询。

Route scenarios: 19

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "backup_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `restore_backup`

用户在看过备份预览后明确要求恢复时，给出备份 ID、原指纹和确认。恢复前保留当前库安全备份。

Route scenarios: 19

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "backup_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "fingerprint": {
      "type": "string",
      "minLength": 64,
      "maxLength": 64
    },
    "confirmed": {
      "type": "boolean"
    },
    "preview_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "backup_id",
    "fingerprint",
    "confirmed"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `share_excerpt`

仅在用户本轮明确授权时，按精确资料、字段和片段范围返回该片段。具名宿主必须提供 request_id 并先获本机一次性许可。

Route scenarios: 20

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "kind": {
      "type": "string",
      "enum": [
        "prompt",
        "inspiration",
        "clipping",
        "resume"
      ]
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "field": {
      "type": "string",
      "enum": [
        "title",
        "text",
        "note",
        "question",
        "source"
      ]
    },
    "start": {
      "type": "integer",
      "minimum": 0,
      "maximum": 1000
    },
    "length": {
      "type": "integer",
      "minimum": 1,
      "maximum": 500
    },
    "selection": {
      "type": "string",
      "minLength": 1,
      "maxLength": 500
    },
    "last_sentence": {
      "type": "boolean"
    },
    "authorized": {
      "type": "boolean"
    }
  },
  "additionalProperties": false,
  "required": [
    "kind",
    "id",
    "field",
    "authorized"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `plan_mcp_calls`

按现有场景或结构化意图生成只读调用计划；定位目标、核对版本和参数，不执行工具。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "scenario": {
      "type": "string",
      "enum": [
        "01",
        "02",
        "03",
        "04",
        "05",
        "06",
        "07",
        "08",
        "09",
        "10",
        "11",
        "12",
        "13",
        "14",
        "15",
        "16",
        "17",
        "18",
        "19",
        "20"
      ]
    },
    "arguments_by_tool": {
      "type": "object",
      "properties": {}
    },
    "intent": {
      "type": "string",
      "enum": [
        "get_prompt",
        "set_prompt_state",
        "snooze_prompt"
      ]
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "reference": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "keywords": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1,
        "maxLength": 80
      },
      "minItems": 1,
      "maxItems": 8
    },
    "state": {
      "type": "string",
      "enum": [
        "want_to_write",
        "in_progress",
        "written"
      ]
    },
    "time_text": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "timezone": {
      "type": "string",
      "minLength": 1,
      "maxLength": 64
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_mcp_directory`

查询当前宿主 profile 与 allowed scopes 可见的真实 MCP 工具目录。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `compare_mcp_directories`

将宿主保存的完整目录快照与当前可见目录比较，返回结构化 schema、权限和迁移变化。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "baseline": {
      "type": "object",
      "properties": {
        "catalog_format": {
          "type": "string",
          "minLength": 1,
          "maxLength": 100
        },
        "directory_id": {
          "type": "string",
          "minLength": 1,
          "maxLength": 128
        },
        "protocol_version": {
          "type": "integer",
          "minimum": 1,
          "maximum": 100
        },
        "service_version": {
          "type": "string",
          "minLength": 1,
          "maxLength": 40
        },
        "directory_version": {
          "type": "string",
          "minLength": 1,
          "maxLength": 40
        },
        "directory_schema_version": {
          "type": "integer",
          "minimum": 1,
          "maximum": 100
        },
        "tool_count": {
          "type": "integer",
          "minimum": 0,
          "maximum": 10000
        },
        "total_count": {
          "type": "integer",
          "minimum": 0,
          "maximum": 10000
        },
        "offset": {
          "type": "integer",
          "minimum": 0
        },
        "complete": {
          "type": "boolean"
        },
        "capabilities": {
          "type": "object",
          "properties": {
            "tool_count": {
              "type": "integer",
              "minimum": 0,
              "maximum": 10000
            }
          },
          "additionalProperties": true
        },
        "identity": {
          "type": "object",
          "properties": {
            "host_id": {
              "type": "string",
              "minLength": 1,
              "maxLength": 128
            },
            "profile": {
              "type": "string",
              "minLength": 1,
              "maxLength": 128
            },
            "authorization_revision": {
              "type": "integer",
              "minimum": 0
            }
          },
          "additionalProperties": true
        },
        "tools": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": true
          }
        }
      },
      "required": [
        "tools"
      ],
      "additionalProperties": true
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    }
  },
  "additionalProperties": false,
  "required": [
    "baseline"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `diagnose_mcp_compatibility`

连接本机 loopback 握手并比对宿主预期协议/服务版本。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "expected_protocol_version": {
      "type": "integer",
      "minimum": 1,
      "maximum": 10
    },
    "expected_service_version": {
      "type": "string",
      "minLength": 1,
      "maxLength": 40
    },
    "expected_instance_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_directory_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "expected_protocol_version",
    "expected_service_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `run_mcp_self_check`

返回不触碰业务数据的合成校验向量，供 MCP 宿主核对发现、结构化回执、时区、附件和歧义策略。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_request_status`

按 request_id 查询当前 actor 的提交、失败或未知状态，不返回原始回执正文。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `dry_run_actions`

只读预览一个已授权的精确工具动作、目标和当前版本；可为后续高影响操作创建短时绑定预览。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "action": {
      "type": "string",
      "minLength": 1,
      "maxLength": 128
    },
    "arguments": {
      "type": "object",
      "properties": {}
    },
    "bind_preview": {
      "type": "boolean"
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "action",
    "arguments"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `continue_pending_call`

跨轮补齐一个已绑定的工具参数；检查版本、过期和权限，确认后只返回该精确工具的一次调用建议。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "tool": {
      "type": "string",
      "minLength": 1,
      "maxLength": 128
    },
    "pending_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "arguments": {
      "type": "object",
      "properties": {}
    },
    "confirmed": {
      "type": "boolean"
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `create_handoff`

Create a five-minute handoff for one selected prompt and up to five selected clue fields.

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "target_host_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 128
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "clues": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "kind": {
            "type": "string",
            "enum": [
              "clipping",
              "inspiration",
              "resume"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "field": {
            "type": "string",
            "minLength": 1,
            "maxLength": 32
          }
        },
        "required": [
          "kind",
          "id",
          "field"
        ],
        "additionalProperties": false
      },
      "maxItems": 5
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "target_host_id",
    "prompt_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `receive_handoff`

Receive only the prompt and clue fields selected for this host in a live handoff.

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "handoff_id": {
      "type": "string",
      "minLength": 32,
      "maxLength": 128
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "handoff_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `create_export_artifact_reference`

为已完成的本机 Markdown 导出回执创建限时、按宿主绑定的文件引用；不接受文件路径。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "export_request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "source_channel": {
      "type": "string",
      "enum": [
        "local",
        "mcp"
      ]
    },
    "expires_in_seconds": {
      "type": "integer",
      "minimum": 60,
      "maximum": 3600
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "export_request_id",
    "expires_in_seconds"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_export_artifact_info`

查看当前宿主限时导出文件引用的大小、SHA-256 与分块上限。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "artifact_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "artifact_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `read_export_artifact_chunk`

按字节偏移读取本机导出 Markdown 的一个有界 base64 分块。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "artifact_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    },
    "max_bytes": {
      "type": "integer",
      "minimum": 1,
      "maximum": 65536
    }
  },
  "additionalProperties": false,
  "required": [
    "artifact_id",
    "offset",
    "max_bytes"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `start_attachment_upload`

为用户明确指定的图片建立按宿主绑定的续传会话；声明图片类型、大小、SHA-256 和有效期。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "image_type": {
      "type": "string",
      "enum": [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp"
      ]
    },
    "size_bytes": {
      "type": "integer",
      "minimum": 1,
      "maximum": 5242880
    },
    "sha256": {
      "type": "string",
      "minLength": 64,
      "maxLength": 64,
      "pattern": "^[0-9a-fA-F]{64}$"
    },
    "expires_in_seconds": {
      "type": "integer",
      "minimum": 60,
      "maximum": 86400
    },
    "note": {
      "type": "string",
      "maxLength": 500
    },
    "source": {
      "type": "string",
      "maxLength": 160
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "image_type",
    "size_bytes",
    "sha256",
    "expires_in_seconds"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `put_attachment_upload_chunk`

按固定的零起始分段编号续传图片；同编号同 SHA 幂等，不同内容冲突。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "upload_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "part_number": {
      "type": "integer",
      "minimum": 0
    },
    "part_sha256": {
      "type": "string",
      "minLength": 64,
      "maxLength": 64,
      "pattern": "^[0-9a-fA-F]{64}$"
    },
    "chunk_base64": {
      "type": "string",
      "minLength": 1,
      "maxLength": 87384
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "upload_id",
    "part_number",
    "part_sha256",
    "chunk_base64"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_attachment_upload_status`

查询当前宿主的上传状态、已收分段和待传分段，不返回已传图片内容。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "upload_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "upload_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `finish_attachment_upload`

校验完整图片的大小、SHA-256 和类型后，复用原截图 SQLite 保存流程。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "upload_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "upload_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `cancel_attachment_upload`

取消当前宿主未完成的截图上传并清除已传分段内容。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "upload_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "upload_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_thought_context`

一次读取当前会话或指定题的公开题文、写作状态、续接卡和素材引用及有效偏好；可选简要或详细，始终遵守逐项私密授权。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "detail": {
      "type": "string",
      "enum": [
        "summary",
        "detail",
        "full"
      ]
    },
    "budget": {
      "type": "integer",
      "minimum": 256,
      "maximum": 6000,
      "description": "题文文本预算，单位为字符。"
    },
    "page_size": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "cursor": {
      "type": "string",
      "minLength": 1,
      "maxLength": 16384
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `search_indexed_library`

在本机增量中文索引中检索题文、摘句、标题和主题；说明真实命中字段与原词、主题别名或近似表述。游标续页绑定宿主、范围、授权和索引快照。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "cursor": {
      "type": "string",
      "minLength": 1,
      "maxLength": 16384
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "kinds": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "prompt",
          "inspiration",
          "clipping",
          "resume",
          "topic",
          "checkpoint",
          "claim_source",
          "topic_project",
          "relation"
        ]
      },
      "minItems": 1,
      "maxItems": 9
    },
    "page_size": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `set_topic_aliases`

为一条现有主题设置用户明确给出的别名；空列表清除。只接受具名授权，不自动生成同义词。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "topic_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "aliases": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1,
        "maxLength": 80
      },
      "minItems": 0,
      "maxItems": 32
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "topic_id",
    "aliases"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `submit_async_job`

明确提交一个受限的备份、导出或批量操作，返回可查询的任务 ID。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "action": {
      "type": "string",
      "enum": [
        "apply_actions",
        "create_backup",
        "export_thought_bundle"
      ]
    },
    "arguments": {
      "type": "object"
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "action",
    "arguments"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_async_job`

读取当前宿主提交的异步任务状态和结果。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "job_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "job_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `cancel_async_job`

在提交阶段请求取消；已进入提交的任务会明确拒绝取消。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "job_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "job_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `resume_async_job`

明确继续一个未自动重放的排队或中断任务。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "job_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "job_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_request_diagnostics`

按 request_id 查询本宿主的阶段耗时和脱敏错误摘要。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `restart_service`

关闭新写入并等待在途请求与已入列写入完成，接受一次由独立宿主进程执行的正常重启。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "operation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "timeout_seconds": {
      "type": "integer",
      "minimum": 1,
      "maximum": 3600
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "operation_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_service_restart`

按 operation_id 读取宿主维护状态、排空阻塞、重启实例核验和持久回执。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "operation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "operation_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `retry_service_restart`

在可重试失败后以相同 operation_id 重新排空并启动宿主。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "operation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "timeout_seconds": {
      "type": "integer",
      "minimum": 1,
      "maximum": 3600
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "operation_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `preview_retention_cleanup`

按指定的保留期限预览当前宿主可清理的数据与保护原因。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "retention_days": {
      "type": "integer",
      "minimum": 1,
      "maximum": 36500
    },
    "retention_overrides": {
      "type": "object",
      "properties": {
        "temporary_references": {
          "type": "integer",
          "minimum": 1,
          "maximum": 36500
        },
        "selection_history": {
          "type": "integer",
          "minimum": 1,
          "maximum": 36500
        },
        "undo_records": {
          "type": "integer",
          "minimum": 1,
          "maximum": 36500
        },
        "write_receipts": {
          "type": "integer",
          "minimum": 1,
          "maximum": 36500
        },
        "diagnostics": {
          "type": "integer",
          "minimum": 1,
          "maximum": 36500
        }
      },
      "additionalProperties": false
    }
  },
  "additionalProperties": false,
  "required": [
    "conversation_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `apply_retention_cleanup`

核对 preview 指纹并按用户明确确认的计划清理。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "plan_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "fingerprint": {
      "type": "string",
      "minLength": 64,
      "maxLength": 64
    },
    "confirmed": {
      "type": "boolean"
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "plan_id",
    "fingerprint",
    "confirmed"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `correct_prompt_status`

按原 request_id 或当前会话最近一项可核对的命题状态操作，原子更正状态和暂缓时间。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "source_conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "target_request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "state": {
      "type": "string",
      "enum": [
        "want_to_write",
        "in_progress",
        "written"
      ]
    },
    "snoozed_until": {
      "type": [
        "string",
        "null"
      ],
      "format": "date-time",
      "minLength": 1
    },
    "reason": {
      "type": "string",
      "minLength": 1,
      "maxLength": 240
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "conversation_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_prompt_timeline`

读取一题或多题已记录的真实状态、暂缓、更正与撤销事件；返回覆盖起点、版本、来源和可查回引用。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "prompt_ids": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "minItems": 1,
      "maxItems": 20
    },
    "from": {
      "type": "string",
      "format": "date-time"
    },
    "through": {
      "type": "string",
      "format": "date-time"
    },
    "event_types": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "created",
          "state_changed",
          "snoozed",
          "snooze_cleared",
          "corrected",
          "undone"
        ]
      },
      "minItems": 1,
      "maxItems": 6
    },
    "page_size": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    }
  },
  "additionalProperties": false,
  "required": [
    "prompt_ids"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `review_prompt_events`

按实际事件时间回顾或比较一至两个本地日历区间，区分完成事件、重开事件和当前完成态。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "periods": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "label": {
            "type": "string",
            "minLength": 1,
            "maxLength": 40
          },
          "from_date": {
            "type": "string",
            "format": "date"
          },
          "through_date": {
            "type": "string",
            "format": "date"
          }
        },
        "required": [
          "from_date",
          "through_date"
        ],
        "additionalProperties": false
      },
      "minItems": 1,
      "maxItems": 2
    },
    "timezone": {
      "type": "string",
      "minLength": 1,
      "maxLength": 64
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "event_types": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "created",
          "state_changed",
          "snoozed",
          "snooze_cleared",
          "corrected",
          "undone"
        ]
      },
      "minItems": 1,
      "maxItems": 6
    },
    "page_size": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "offset": {
      "type": "integer",
      "minimum": 0
    }
  },
  "additionalProperties": false,
  "required": [
    "periods",
    "timezone"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_full_record`

读取一条完整命题、材料或主题档案及其真实字段、关联和附件校验信息。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "kind": {
      "type": "string",
      "enum": [
        "prompt",
        "inspiration",
        "clipping",
        "resume",
        "topic"
      ]
    },
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "kind",
    "id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `list_full_records`

按稳定的 kind、ID 顺序分页读取完整档案；游标绑定当前宿主、授权修订和数据库快照。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "kind": {
      "type": "string",
      "enum": [
        "all",
        "prompt",
        "inspiration",
        "clipping",
        "resume",
        "topic"
      ]
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "state": {
      "type": "string",
      "enum": [
        "want_to_write",
        "in_progress",
        "written"
      ]
    },
    "created_from": {
      "type": "string",
      "format": "date-time"
    },
    "created_to": {
      "type": "string",
      "format": "date-time"
    },
    "page_size": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100
    },
    "cursor": {
      "type": "string",
      "minLength": 1,
      "maxLength": 16384
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_attachment_chunk`

按有界 base64 分块读取一张已保存原图，返回完整文件和分块校验值。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "offset": {
      "type": "integer",
      "minimum": 0,
      "maximum": 2147483647
    },
    "max_bytes": {
      "type": "integer",
      "minimum": 1,
      "maximum": 65536
    }
  },
  "additionalProperties": false,
  "required": [
    "id",
    "offset",
    "max_bytes"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `export_full_library`

按指定范围导出完整 JSONL 档案和原图；返回按宿主授权修订绑定的一小时文件引用。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "kind": {
      "type": "string",
      "enum": [
        "all",
        "prompt",
        "inspiration",
        "clipping",
        "resume",
        "topic"
      ]
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "created_from": {
      "type": "string",
      "format": "date-time"
    },
    "created_to": {
      "type": "string",
      "format": "date-time"
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_full_export_info`

核对完整导出文件的格式、大小、SHA-256、记录数和有效期。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "export_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "export_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `read_full_export_chunk`

按字节偏移读取完整 JSONL 导出的有界 base64 分块。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "export_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "offset": {
      "type": "integer",
      "minimum": 0,
      "maximum": 2147483647
    },
    "max_bytes": {
      "type": "integer",
      "minimum": 1,
      "maximum": 65536
    }
  },
  "additionalProperties": false,
  "required": [
    "export_id",
    "offset",
    "max_bytes"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_session_conditions`

读取当前会话有效的选题条件、来源轮次和到期时间。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    }
  },
  "additionalProperties": false,
  "required": [
    "conversation_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `set_session_conditions`

在指定会话中保存选题条件；本次 next_prompt 明确传入的值优先覆盖。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 0
    },
    "conditions": {
      "type": "object",
      "properties": {
        "direction": {
          "type": "string",
          "enum": [
            "self_exploration",
            "consultation_reflection",
            "social_topics"
          ]
        },
        "topic": {
          "type": "string",
          "minLength": 1,
          "maxLength": 200
        },
        "depth": {
          "type": "string",
          "enum": [
            "light",
            "medium",
            "deep"
          ]
        },
        "minutes": {
          "type": "integer",
          "minimum": 1,
          "maximum": 240
        },
        "exclude_directions": {
          "type": "array",
          "items": {
            "type": "string",
            "enum": [
              "self_exploration",
              "consultation_reflection",
              "social_topics"
            ]
          },
          "minItems": 1,
          "maxItems": 3
        },
        "avoid_days": {
          "type": "integer",
          "minimum": 0,
          "maximum": 90
        }
      },
      "additionalProperties": false
    },
    "expires_at": {
      "type": "string",
      "minLength": 1,
      "format": "date-time"
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "conversation_id",
    "expected_version",
    "conditions"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `end_session_conditions`

结束当前会话的全部或指定选题条件。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 0
    },
    "fields": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "direction",
          "topic",
          "depth",
          "minutes",
          "exclude_directions",
          "avoid_days"
        ]
      },
      "minItems": 1,
      "maxItems": 6
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "conversation_id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_prompt_family`

读取题目的已确认题族，并列出可核对的近似题候选。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 30
    }
  },
  "additionalProperties": false,
  "required": [
    "prompt_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `set_prompt_family_relation`

确认两题同族或将自动近似项拆开；保留原 parent_id 收窄关系。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "related_prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "relation": {
      "type": "string",
      "enum": [
        "same_family",
        "separate"
      ]
    },
    "expected_version": {
      "type": "integer",
      "minimum": 0
    },
    "reason": {
      "type": "string",
      "minLength": 1,
      "maxLength": 300
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "prompt_id",
    "related_prompt_id",
    "relation",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `pick_unfinished_prompt`

从可继续的未完成题、到期暂缓题和有续接点的题中按实际活动排序选一题。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "depth": {
      "type": "string",
      "enum": [
        "light",
        "medium",
        "deep"
      ]
    },
    "minutes": {
      "type": "integer",
      "minimum": 1,
      "maximum": 240
    },
    "exclude_directions": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "self_exploration",
          "consultation_reflection",
          "social_topics"
        ]
      },
      "minItems": 1,
      "maxItems": 3
    },
    "avoid_days": {
      "type": "integer",
      "minimum": 0,
      "maximum": 90
    },
    "exclude_family_ids": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "minItems": 1,
      "maxItems": 20
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 20
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "conversation_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `list_unfinished_prompts`

按与未完成题推荐相同的过滤和排序分页读取候选，不记录新的推荐。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "depth": {
      "type": "string",
      "enum": [
        "light",
        "medium",
        "deep"
      ]
    },
    "minutes": {
      "type": "integer",
      "minimum": 1,
      "maximum": 240
    },
    "exclude_directions": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": [
          "self_exploration",
          "consultation_reflection",
          "social_topics"
        ]
      },
      "minItems": 1,
      "maxItems": 3
    },
    "avoid_days": {
      "type": "integer",
      "minimum": 0,
      "maximum": 90
    },
    "exclude_family_ids": {
      "type": "array",
      "items": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200
      },
      "minItems": 1,
      "maxItems": 20
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "offset": {
      "type": "integer",
      "minimum": 0,
      "maximum": 10000
    }
  },
  "additionalProperties": false,
  "required": [
    "conversation_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_guide_history`

按题目或会话分页读取已保存的追问方式、用户明确线索和问题，不含用户回答。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "conversation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "history_scope": {
      "type": "string",
      "enum": [
        "same_host",
        "all_hosts"
      ]
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    },
    "offset": {
      "type": "integer",
      "minimum": 0,
      "maximum": 10000
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `set_question_preference`

设置长期或近期追问方式偏好，沿用全局偏好版本；scope=recent 必须提供 expires_at，scope=baseline 不可提供 expires_at。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "mode": {
      "type": "string",
      "enum": [
        "experience",
        "contrast",
        "counterexample",
        "boundary",
        "evidence",
        "narrow"
      ]
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "scope": {
      "type": "string",
      "enum": [
        "baseline",
        "recent"
      ]
    },
    "strength": {
      "type": "number",
      "minimum": 0,
      "maximum": 2
    },
    "expires_at": {
      "type": "string",
      "minLength": 1,
      "format": "date-time"
    },
    "reason": {
      "type": "string",
      "minLength": 1,
      "maxLength": 300
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "expected_version",
    "mode",
    "scope",
    "strength"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `get_question_preferences`

读取问法偏好的长期值、近期值、生效强度和变更历史。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "include_history": {
      "type": "boolean"
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50
    }
  },
  "additionalProperties": false
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `end_question_preference`

结束指定方向或默认范围内某种问法的近期偏好。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "mode": {
      "type": "string",
      "enum": [
        "experience",
        "contrast",
        "counterexample",
        "boundary",
        "evidence",
        "narrow"
      ]
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "expected_version",
    "mode"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `draft_prompt_from_materials`

按给定顺序读取多条指定素材的全文、精确片段或剪藏标题，返回一条未保存的对照、承接或反例草题及可回查出处。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "materials": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "kind": {
            "type": "string",
            "enum": [
              "inspiration",
              "clipping",
              "resume"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "selection": {
            "type": "string",
            "enum": [
              "full",
              "excerpt",
              "link_title"
            ]
          },
          "field": {
            "type": "string",
            "enum": [
              "note",
              "published_on",
              "question",
              "source",
              "text",
              "title",
              "url"
            ]
          },
          "start": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000
          },
          "length": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20000
          },
          "expected_version": {
            "type": "integer",
            "minimum": 1
          }
        },
        "required": [
          "kind",
          "id",
          "selection"
        ],
        "additionalProperties": false
      },
      "minItems": 1,
      "maxItems": 12
    },
    "relation_type": {
      "type": "string",
      "enum": [
        "comparison",
        "continuation",
        "counterexample"
      ]
    },
    "direction": {
      "type": "string",
      "enum": [
        "self_exploration",
        "consultation_reflection",
        "social_topics"
      ]
    },
    "topic": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "depth": {
      "type": "string",
      "enum": [
        "light",
        "medium",
        "deep"
      ]
    }
  },
  "additionalProperties": false,
  "required": [
    "materials",
    "relation_type"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `save_prompt_material_relations`

保存新草题或给现有题增加有序的 typed 素材关系；题目与关系在同一事务提交。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    },
    "prompt": {
      "type": "object",
      "properties": {
        "text": {
          "type": "string",
          "minLength": 1,
          "maxLength": 1000
        },
        "direction": {
          "type": "string",
          "enum": [
            "self_exploration",
            "consultation_reflection",
            "social_topics"
          ]
        },
        "topic": {
          "type": "string",
          "minLength": 1,
          "maxLength": 200
        },
        "depth": {
          "type": "string",
          "enum": [
            "light",
            "medium",
            "deep"
          ]
        },
        "parent_id": {
          "type": "string",
          "minLength": 1,
          "maxLength": 200
        }
      },
      "required": [
        "text",
        "direction"
      ],
      "additionalProperties": false
    },
    "relation_type": {
      "type": "string",
      "enum": [
        "comparison",
        "continuation",
        "counterexample",
        "example",
        "context"
      ]
    },
    "materials": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "kind": {
            "type": "string",
            "enum": [
              "inspiration",
              "clipping",
              "resume"
            ]
          },
          "id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "expected_version": {
            "type": "integer",
            "minimum": 1
          },
          "expected_relation_version": {
            "type": "integer",
            "minimum": 1
          },
          "selection": {
            "type": "string",
            "enum": [
              "full",
              "excerpt",
              "link_title"
            ]
          },
          "field": {
            "type": "string",
            "enum": [
              "note",
              "published_on",
              "question",
              "source",
              "text",
              "title",
              "url"
            ]
          },
          "start": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000
          },
          "length": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20000
          },
          "role": {
            "type": "string",
            "enum": [
              "source",
              "example",
              "counterexample",
              "context",
              "contrast",
              "support",
              "opposition",
              "indirect",
              "unverified"
            ]
          },
          "position": {
            "type": "integer",
            "minimum": 0,
            "maximum": 1000000
          },
          "description": {
            "type": "string",
            "maxLength": 500
          }
        },
        "required": [
          "kind",
          "id",
          "expected_version",
          "selection",
          "role",
          "position"
        ],
        "additionalProperties": false
      },
      "minItems": 1,
      "maxItems": 20
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "relation_type",
    "materials"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `remove_prompt_material_relation`

按关系版本移除一条题目与素材之间的关联。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "relation_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_version": {
      "type": "integer",
      "minimum": 1
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "relation_id",
    "expected_version"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `read_entity_relations`

按题目或素材 ID 正向、反向分页读取 typed 关联及旧版来源标签。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "kind": {
      "type": "string",
      "enum": [
        "prompt",
        "inspiration",
        "clipping",
        "resume"
      ]
    },
    "object_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100
    },
    "offset": {
      "type": "integer",
      "minimum": 0,
      "maximum": 2147483647
    }
  },
  "additionalProperties": false,
  "required": [
    "kind",
    "object_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `write_claim_evidence`

按具体说法记录剪藏的支持、反对、间接或待核对范围、位置、引文及结论。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "request_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "expected_prompt_version": {
      "type": "integer",
      "minimum": 1
    },
    "claims": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "claim_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200
          },
          "expected_version": {
            "type": "integer",
            "minimum": 1
          },
          "text": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500
          },
          "evidence": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "evidence_id": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 200
                },
                "expected_version": {
                  "type": "integer",
                  "minimum": 1
                },
                "clipping_id": {
                  "type": "string",
                  "minLength": 1,
                  "maxLength": 200
                },
                "material_version": {
                  "type": "integer",
                  "minimum": 1
                },
                "field": {
                  "type": "string",
                  "enum": [
                    "title",
                    "url",
                    "source",
                    "published_on",
                    "note"
                  ]
                },
                "support_type": {
                  "type": "string",
                  "enum": [
                    "supports",
                    "opposes",
                    "indirect",
                    "unverified"
                  ]
                },
                "location": {
                  "type": "string",
                  "maxLength": 200
                },
                "excerpt": {
                  "type": "string",
                  "maxLength": 1000
                },
                "conclusion": {
                  "type": "string",
                  "maxLength": 500
                }
              },
              "required": [
                "clipping_id",
                "material_version",
                "support_type"
              ],
              "additionalProperties": false
            },
            "minItems": 0,
            "maxItems": 20
          }
        },
        "required": [
          "text"
        ],
        "additionalProperties": false
      },
      "minItems": 1,
      "maxItems": 15
    }
  },
  "additionalProperties": false,
  "required": [
    "request_id",
    "prompt_id",
    "expected_prompt_version",
    "claims"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": false,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false
}
```

#### `read_prompt_evidence`

按题目分页读取各说法的证据类型、引用位置、片段、结论和核对时间；旧版关联会标为待核对。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "prompt_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100
    },
    "offset": {
      "type": "integer",
      "minimum": 0,
      "maximum": 2147483647
    }
  },
  "additionalProperties": false,
  "required": [
    "prompt_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

#### `get_topic_project_progress`

分页读取专题全部成员、关系、续接问题、待核对说法和基于已保存依据的下一条线索。

Route scenarios: none declared in `mcp_workflows.ROUTES`

`inputSchema` as registered:

```json
{
  "type": "object",
  "properties": {
    "project_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 100
    },
    "offset": {
      "type": "integer",
      "minimum": 0,
      "maximum": 2147483647
    }
  },
  "additionalProperties": false,
  "required": [
    "project_id"
  ]
}
```

MCP registration annotations:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": false,
  "openWorldHint": false
}
```

<!-- END GENERATED MCP TOOL SCHEMAS -->
