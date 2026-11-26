# Feature Spec: v0.2c 审计式前缀稳定化热修（`cacherift repair`）

> 本迭代遵守 `specs/00-constitution.md`。`repair` 是全工具里唯一会产出「修改过的请求体」的命令，边界比其他命令更窄。

## 范围界定

- **新增命令** `cacherift repair`：对 JSONL 捕获里能被白名单规则证明语义等价的前缀缺陷，产出修复后的 JSONL 与一份完整的字节审计。默认 dry-run，不写任何文件。
- **本期不做**：把修复回写进任何真实的 Agent 代码或配置；重放修复后的请求（由维护者用 `probe` / `proxy` 自行验证）；白名单之外的一切改写。

## 核心行为规格

1. **白名单规则，按名开关**（`--rule`，可重复；默认 `all`）。白名单之外没有「通用修复」通道：
   1. `canonicalize_tool_key_order`：当前轮 `tools[i]` 与父轮同一位置的工具「仅键序不同」时，用**父轮的对象**替换当前轮的，即对齐回父节点既有的键序。不按字母重排——那会破坏已经建立的缓存前缀。
   2. `stabilize_tool_array_order`：把当前轮 `tools` 恢复成父轮的既有顺序，当前轮新增的工具一律挪到 `tools` 末尾。既有工具的匹配依据是「内容相同（不计键序）」或「工具名相同」；父轮有而当前轮没有的工具视为已删除，不凭空恢复。被修改过内容但工具名相同的工具留在原位，内容保持当前轮的。
   3. `migrate_gpt62_top_level_instructions`：方言为 `openai_responses` 且能力表声明单元顺序为 `tools → input → instructions`（即顶层 `instructions` 不参与跨轮缓存）时，把顶层 `$.instructions` 迁为 `$.input[0]` 的一条 `developer` 消息并移除顶层字段。触发条件取自能力表而不是写死模型名。**迁移只沿血缘一致地进行**：仅当本记录是血缘的根（没有父轮），或者父轮的顶层 `instructions` 与之完全相同（跨轮静态）**且父轮自身已迁移**时才迁移。`input` 不是数组，或请求带 `previous_response_id`（服务端续接上下文，本地没有需要对齐的前缀）时不迁移。
   4. `normalize_anthropic_breakpoints`：方言为 `anthropic_messages` 且存在匹配的父轮时，`messages` 里只有**最后一条带断点的消息**保留 `cache_control`，更早消息上的断点全部移除（过期中间标记）。`tools` 与 `system` 上的断点不动。
2. **修复后的记录是投影，不是线上字节**：被修复的记录以 `captured_body_bytes=None` + 修复后的 `parsed_body` 写出，重新分析时坐标空间为 `PROJECTED`。未被修复的记录原样输出，保留其原始字节。
3. **逐轮传播**：子轮的修复以父轮**修复后的**请求体为基准，保证整条血缘一致。
4. **硬安全阀**：以下情况整条记录拒绝自动修复，只在审计里记 `applied=false` 与原因，不报错退出：
   - `lineage.status` 为 `AMBIGUOUS`（C4：父轮无法确定，任何「对齐父轮」的动作都不可信）→ `ambiguous_lineage`；
   - 能力表 `support_level` 不是 `full`（`usage_only` 方言与未登记模型）→ `unsupported_capability`；
   - 父轮持有同一段静态顶层 `instructions`、却因为上面两条未被迁移 → 子轮同样不迁移，记 `parent_not_repaired`。否则子轮的 developer 消息会成为一个排在父轮既有前缀之前的新单元，正是本命令要消除的那种断裂。顶层 `instructions` 在血缘中途才首次出现时同理：那是一个新单元而不是一次搬迁，规则不碰它，诊断器照常报告。
   - 审计里拒绝条目的粒度：`AMBIGUOUS` 的记录每条都记一条拒绝——没有可信的父轮，无从判断它是否本可修复；能力不足的记录只在「某个已启用的规则本可适用」时才记，免得一份 `usage_only` 方言的抓包里每条健康记录都变成一条噪音。
5. **自由文本永不改写**：没有任何规则读取、匹配或替换提示词文本。`MUTATED_EXISTING` 的文本保持当前轮原样——既不回退成父轮的文本，也不做任何正则替换。
6. **审计**（每条被修复或被拒绝的记录一条，零正文）：
   - 记录号、行号、血缘状态与父轮、能力等级、`applied`、`blocked_reason`；
   - 整条请求体修复前后的 SHA-256 与字节数，并标明坐标系：修复前为 `wire_raw`（有原始字节时）或 `projected`，修复后恒为 `projected`；
   - 每处改动的规则名、JSON 路径、节点修复前后的 SHA-256 与字节数（均为 `projected`），以及一句不含正文的说明。
7. **命令行**：`cacherift repair <capture_path> [--rule all|<name>...] [--apply --output repaired.jsonl] [--audit-out audit.json] [--format table|json]`。
   - 默认 dry-run：不写任何文件，`--output` 不被接受（退出 `2`）。
   - `--apply` 必须与 `--output` 同时出现；`--audit-out` 在 dry-run 与 `--apply` 下都按给定路径写审计。
   - 目标文件已存在时拒绝覆盖（退出 `2`）。
   - 退出码：`0` 完成（dry-run 或已写出），`2` 配置错误（找不到捕获、捕获无法解析、未知规则名、参数组合不对）。被安全阀拒绝的记录不影响退出码，它们在输出与审计里如实列出。

## 强制验收矩阵（Counterexamples R1–R5）

- **R1**：默认 dry-run 零写盘；`--apply` 与 `--output` 缺一不可；不覆盖已存在的文件。
- **R2**：`AMBIGUOUS` 血缘的记录即使存在可修复的缺陷也严格拒改；`usage_only` 方言与未登记模型同样拒改。
- **R3**：修复后的 JSONL 复跑 `analyze_records` + `evaluate_diagnoses`，对应断链归零，且不引入任何新的 `MUTATED_EXISTING`；未修复的记录逐字节不变。
- **R4**：自由文本的修改不被任何规则触碰，与正则、日期、路径字面量无关。
- **R5**：审计记录完整的修复前后哈希，可由输入与输出记录重新计算核对；审计内容不含提示词正文。
