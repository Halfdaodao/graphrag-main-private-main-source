# GraphRAG POC 功能测试用例

## 1. 测试范围

本手册覆盖本项目的前端、GraphRAG 索引和四种检索方式、EUOS 已发布数据接入、候选图谱审核、Neo4j Extracted/Published 双层图谱、Evidence 失效传播、治理接口和异常处理。

测试地址：`http://127.0.0.1:8767`

前置条件：

1. 前端服务、Embedding Service、Neo4j、EUOS Knowledge Service 均已启动。
2. 浏览器打开测试地址，页面不显示乱码。
3. EUOS 服务令牌已在本机 `.env` 配置；测试记录或截图中不得包含令牌、密码或其他敏感配置。
4. 当前已接入的 EUOS 数据可使用：
   - 项目 ID：`local-project`
   - Wiki Space ID：`wiki-multi-snapshot-regression-v2`
5. 执行索引会调用模型且耗时较长。除索引用例外，其余用例不需要重新索引。

记录字段：执行日期、执行人、结果（通过/失败/阻塞）、实际结果、截图或接口响应文件、缺陷编号。

## 2. 冒烟与服务连通性

| ID | 功能 | 操作步骤 | 预期结果 |
|---|---|---|---|
| SMK-01 | 前端访问 | 浏览器访问测试地址。 | 页面正常加载，中文文本正常显示，无空白页或乱码。 |
| SMK-02 | 前端状态刷新 | 刷新页面或点击“刷新治理数据”。 | 显示 EUOS Evidence/Wiki 数量、输入准备状态和 parquet 索引产物状态。 |
| SMK-03 | Embedding 服务 | 访问 `http://127.0.0.1:8001/health`。 | HTTP 200，返回 `status: ok`、模型名和向量维度。 |
| SMK-04 | Neo4j 配置 | 调用 `GET /api/neo4j/config`。 | HTTP 200，返回 Neo4j URI、用户和数据库名；不返回密码。 |
| SMK-05 | EUOS 配置 | 页面检查 EUOS 配置提示，或调用 `GET /api/module3/euos/config`。 | 服务地址正确，`tokenConfigured` 为 `true`；令牌值不出现在响应中。 |

## 3. 数据接入与输入准备

| ID | 功能 | 操作步骤 | 预期结果 |
|---|---|---|---|
| DAT-05 | EUOS 同步当前版本 | 填写 `local-project`、`wiki-multi-snapshot-regression-v2`，Wiki 版本留空，点击“从 EUOS 拉取”。 | 返回当前已发布 Wiki 版本、Wiki 页面数、Evidence 快照数和 Evidence 条数；页面状态更新。 |
| DAT-06 | EUOS 同步指定版本 | 输入一个存在的历史 Wiki 版本后同步。 | 拉取该指定版本的数据；返回版本号与输入一致。 |
| DAT-07 | EUOS 参数校验 | 留空项目 ID 或 Wiki Space ID 后点击拉取。 | 前端不发起有效同步，提示两个字段必填。 |
| DAT-08 | EUOS 不存在资源 | 使用不存在的 Space ID 或版本号调用同步。 | 返回明确错误；已有已同步 EUOS 数据不被清空。 |
| DAT-09 | 生成 GraphRAG 输入 | 在已有 Evidence 与 Wiki 后点击“准备输入”。 | 成功生成 manifest 和 GraphRAG 输入文件；状态显示“已准备输入”。 |
| DAT-10 | 缺少输入准备 | 在新环境中未同步 EUOS 数据时点击“对已同步 EUOS 数据运行索引”。 | 返回缺少已发布 Wiki/Evidence 的明确错误。 |

## 4. 索引与候选图谱生成

| ID | 功能 | 操作步骤 | 预期结果 |
|---|---|---|---|
| IDX-01 | 首次全量索引 | 在独立干净数据目录或首次环境中，准备输入后点击“运行索引”。 | 显示执行过程并完成；生成 `entities.parquet`、`relationships.parquet`、`text_units.parquet` 等产物。 |
| IDX-02 | 增量索引 | 在 IDX-01 成功后，只新增或修改一份 Wiki/Evidence，重新准备输入并运行索引。 | 输出标记为“增量更新”；只处理新增或变化文档，原有有效产物仍可查询。 |
| IDX-03 | 索引失败可诊断性 | 暂停 Embedding Service 后运行索引，测试后立即恢复服务。 | 索引失败，页面展示可定位的连接错误或命令输出；服务恢复后可再次索引。 |
| IDX-04 | 自动候选同步 | 索引成功后点击“同步候选图谱”，或确认索引后的自动同步结果。 | Neo4j 中生成实体候选、关系候选、Extracted 实体/关系、Wiki、Text Unit 和 Evidence。 |
| IDX-05 | 候选来源完整性 | 在“候选审核”中任选实体候选和关系候选。 | 每项显示对应 Wiki、Text Unit、Evidence；关系候选可追溯其源和目标实体。 |
| IDX-06 | 产物结果查看 | 索引后调用 `GET /api/module3/results`。 | 返回 entities、relationships、communities、community_reports 和 manifest；中文字段可正常解析。 |

## 5. 审核工作流与双层图谱

| ID | 功能 | 操作步骤 | 预期结果 |
|---|---|---|---|
| REV-01 | 候选默认状态 | 同步候选后筛选“Candidate”。 | 新生成且未审核的候选状态为 `Candidate`。 |
| REV-02 | 接受实体候选 | 填写审核人和理由，选择一个实体候选点击“接受”。 | 状态变为 `Accepted`，保存审核人、理由和审核时间；正式层出现对应 PublishedEntity。 |
| REV-03 | 拒绝候选 | 填写审核人和理由，选择一个候选点击“拒绝”。 | 状态变为 `Rejected`，保存审核记录；该候选不进入 Published。 |
| REV-04 | 审核人必填 | 审核人留空，尝试接受或拒绝。 | 前端阻止提交或接口返回必填错误；状态不改变。 |
| REV-05 | 关系无 Evidence 禁止发布 | 找到没有 Evidence 的关系候选，尝试点击“接受”，或直接调用审核接口。 | 前端“接受”按钮不可用；接口也拒绝 `Accepted`，关系不会进入 Published。 |
| REV-06 | 关系有 Evidence 发布 | 选择有 Evidence 的关系候选并接受。 | 关系状态为 `Accepted`；正式层产生 `PUBLISHED_RELATION`，并带审核字段与 Evidence ID。 |
| REV-07 | 审核筛选 | 分别选择 Candidate、Accepted、Rejected、Stale 和全部。 | 列表只显示对应状态，切换后无旧数据残留。 |
| REV-08 | Published 重建 | 对若干候选接受、拒绝后再次点击“同步候选图谱”。 | 正式层只包含 Accepted 且证据有效的实体/关系；Rejected 不会被重新发布。 |
| REV-09 | Extracted 与 Published 隔离 | Neo4j Browser 中分别查询 `ExtractedEntity`、`EntityCandidate`、`PublishedEntity`。 | 自动抽取结果保留在 Extracted；正式结果只在 Published；两层标签和关系不混用。 |

建议 Neo4j 核查语句：

```cypher
MATCH (e:ExtractedEntity {graph_origin:'module3'}) RETURN count(e);
MATCH (e:PublishedEntity {graph_origin:'module3'}) RETURN count(e);
MATCH ()-[r:PUBLISHED_RELATION {graph_origin:'module3'}]->() RETURN count(r);
MATCH (c:RelationshipCandidate {graph_origin:'module3', status:'Accepted'})
WHERE size(coalesce(c.evidence_ids, [])) = 0 RETURN c;
```

最后一条查询应返回 0 行。

## 6. Evidence 版本变更与失效传播

| ID | 功能 | 操作步骤 | 预期结果 |
|---|---|---|---|
| STL-01 | 失效传播触发 | 先接受一条带 Evidence 的关系；修改其关联 Wiki 或 Evidence 内容/版本后，重新准备输入、索引并同步候选。 | 该旧候选或旧关系标记为 `Stale`，并记录“Wiki or Evidence version changed”或等价失效原因。 |
| STL-02 | 失效关系退出正式层 | 在 STL-01 后检查 Published 和“只看正式”查询。 | Stale 关系不再出现在 `PUBLISHED_RELATION`，不进入正式查询路径。 |
| STL-03 | 失效候选可见性 | 筛选 `Stale`。 | 可看到旧候选、审核历史和失效原因；不会被当作可发布候选。 |
| STL-04 | 消失关系传播 | 从输入中删除一个已接受关系的来源，再次索引与同步。 | 该候选变为 `Stale`，原因包含“不再存在于最新 GraphRAG 输出”或等价描述。 |

## 7. 正式混合查询

测试问题建议优先使用当前 EUOS 数据中已有的设备、维护、风险、流程等关键词；先用“候选审核”表中可见的实体名称拼接问题，保证可命中。

| ID | 功能 | 操作步骤 | 预期结果 |
|---|---|---|---|
| QRY-01 | 检索方式中文化 | 展开“检索方式”。 | 显示“全局检索（主题检索）”“局部检索（实体关系检索）”“基础检索（文本向量检索）”“渐进检索（混合检索）”，并有对应简介。 |
| QRY-02 | 全局检索 | 选择全局检索，输入跨文档概览问题，点击执行查询。 | 显示执行阶段、自然语言答案、路径数、Evidence 引文数和状态。 |
| QRY-03 | 局部检索 | 选择局部检索，输入某实体及其关系问题。 | 答案围绕具体实体，返回相关多跳路径及 Evidence。 |
| QRY-04 | 基础检索 | 选择基础检索，输入可在原文中核对的事实问题。 | 返回基于 Text Unit 的答案和可追溯 Evidence。 |
| QRY-05 | 渐进检索 | 选择渐进检索，输入需结合主题和关系的复杂问题。 | 返回 GraphRAG 答案、Neo4j 路径和 Evidence；页面显示“渐进检索（混合检索）”。 |
| QRY-06 | 查询进度展示 | 任意查询执行期间观察输出区。 | 按顺序显示 GraphRAG 启动、生成答案、Neo4j 多跳检索、Evidence 汇总等阶段，按钮不可重复点击。 |
| QRY-07 | 正式范围 | 图谱范围选择“只看正式”，执行可命中已接受关系的问题。 | 路径和 Evidence 仅来自 Published；返回的候选状态为正式/已接受。 |
| QRY-08 | 包含待审核范围 | 图谱范围选择“包含待审核”，用同一问题执行。 | 结果可包含 Candidate/Rejected/Stale 的 Extracted 结果及其状态；正式层结果仍标识为 Accepted。 |
| QRY-09 | 最大跳数 | 对同一问题依次选择 1 跳、3 跳、6 跳执行。 | 响应中的 `max_hops` 与选择一致；任一路径长度不超过设置值。 |
| QRY-10 | 最大跳数边界 | 通过接口传 `maxHops: 0`、`7`、`abc`。 | 返回明确参数错误，不执行查询。 |
| QRY-11 | 空问题 | 查询框留空点击执行。 | 提示“请输入问题”，不调用 GraphRAG。 |
| QRY-12 | 混合查询 API 契约 | 调用 `POST /api/hybrid-query`，请求体含 `query`、`method`、`scope`、`maxHops`。 | 响应含 `ok`、`answer`、`paths`、`evidence`、`candidate_states`、`scope`、`max_hops`；失败时包含可诊断错误。 |

示例请求：

```json
{
  "query": "请说明某个已审核设备实体与维护流程的关系，并给出依据。",
  "method": "drift",
  "scope": "published",
  "maxHops": 3
}
```

## 8. 图谱治理页面操作案例

本节直接对应前端“图谱治理”区域。治理配置先写入本地工作区；Neo4j 在线时同时投影，离线时会在下一次成功同步时投影。

### 8.1 页面功能清单

| 页面区域 | 控件 | 用途 | 测试前提 |
|---|---|---|---|
| Graph Profile | Profile 名称、实体类型、关系类型、保存并启用 Profile | 定义当前图谱允许的实体/关系类型和审核策略。保存新 Profile 时旧 Profile 自动取消启用。 | 已加载页面。 |
| 实体消歧 / 别名审核 | 实体下拉框、Canonical Name、别名、审核人、接受消歧 | 为 Extracted 实体设定标准名称及别名。 | 已有 ExtractedEntity 或索引产物中的实体。 |
| 快照与质量报告 | 构建治理快照、快照列表、质量报告 | 对当前 GraphRAG 产物创建治理快照，显示实体类型、映射和快照质量统计。 | 已准备输入；建议已有索引产物。 |
| 候选审核 | 刷新候选、同步候选图谱、审核人、理由、状态筛选、接受/拒绝 | 审核 Extracted 候选并重建 Published 正式图谱。 | 已有 `entities.parquet` 和 `relationships.parquet`。 |
| GraphRAG 查询 | 检索方式、图谱范围、最大跳数、问题、执行查询、清空 | 按检索方式和正式范围执行混合问答。 | 已完成索引；路径测试需有候选同步结果。 |
| 结果与诊断 | 上游输入状态、索引产物、实体表、关系表、查询结果、证据链、响应日志 | 查看每一步中间产物、结构化结果和失败原因。 | 无。 |

### 8.2 Graph Profile 案例

| ID | 操作步骤 | 测试数据 | 预期结果 |
|---|---|---|---|
| GOV-UI-01 | 打开“图谱治理”，点击“刷新治理数据”。 | 无。 | Profile 列表、实体下拉、消歧记录、快照和质量报告均加载；任一接口失败时响应日志给出错误。 |
| GOV-UI-02 | 在 Profile 名称填入“设备维护测试 Profile”；实体类型填入 `EQUIPMENT,PROCEDURE,FAILURE,OTHER`；关系类型填入 `RELATED,CAUSES`；点击“保存并启用 Profile”。 | 如左。 | 提示保存成功，列表显示新 Profile、版本号和“启用”；质量报告的 `profileId` 切换为新 Profile。 |
| GOV-UI-03 | 再创建名称不同的第二个 Profile 并启用，然后刷新治理数据。 | `安全运维测试 Profile`；实体 `SAFETY_CONDITION,EVENT,OTHER`；关系 `RELATED`。 | 第二个 Profile 为启用状态；第一个 Profile 仍保留但不再显示“启用”。 |
| GOV-UI-04 | Profile 名称、实体类型、关系类型分别留空后保存。 | 空值。 | 每种情况均提示“Profile 名称、实体类型和关系类型不能为空”；既有启用 Profile 不变。 |
| GOV-UI-05 | 输入小写、重复值、两侧空格后保存，例如 `equipment, Equipment, other`。 | 如左。 | 保存后实体/关系类型去重、去空格并转大写。 |

### 8.3 实体消歧 / 别名审核案例

先点击“刷新治理数据”。从“实体”下拉框选择一个当前 Extracted 实体，以下用 `<实体ID>`、`<原始实体名>` 表示实际值。

| ID | 操作步骤 | 测试数据 | 预期结果 |
|---|---|---|---|
| GOV-UI-06 | 选择一个实体；Canonical Name 填 `电梯驱动系统`；别名填 `电梯驱动,驱动系统,旧驱动名称`；审核人填 `tester-a`；点击“接受消歧”。 | 上述示例或根据当前实体调整。 | 提示“实体消歧已保存”；消歧列表出现标准名、实体 ID、别名和审核人。 |
| GOV-UI-07 | 再次选择同一实体，改 Canonical Name 或别名后保存。 | Canonical Name：`电梯主驱动系统`。 | 同一实体只保留最新的一条消歧记录，审核时间更新，不产生重复记录。 |
| GOV-UI-08 | 刷新治理数据，查看实体下拉框中的该实体。 | 无。 | 优先显示 Canonical Name，仍可辨识实体类型。 |
| GOV-UI-09 | Canonical Name 或审核人留空后点击“接受消歧”。 | 空值。 | 页面提示保存失败；不会新增/覆盖消歧记录。 |
| GOV-UI-10 | Neo4j 在线时，在完成 GOV-UI-06 后检查该实体。 | Cypher 见下。 | `ExtractedEntity` 保存 `canonical_name`、`aliases`、`resolution_status`、`resolution_reviewer` 和审核时间。 |

```cypher
MATCH (e:ExtractedEntity {id:'<实体ID>'})
RETURN e.title, e.canonical_name, e.aliases, e.resolution_status,
       e.resolution_reviewer, e.resolved_at;
```

### 8.4 治理快照与质量报告案例

| ID | 操作步骤 | 测试数据 | 预期结果 |
|---|---|---|---|
| GOV-UI-11 | 点击“构建治理快照”。 | 已有索引产物。 | 按钮执行期间禁用；完成后提示快照 ID，快照列表出现 `Ready` 或等价完成状态，并显示候选实体、候选关系数量。 |
| GOV-UI-12 | 点击“刷新治理数据”，阅读质量报告。 | 已有索引和至少一个 Profile。 | 报告至少包含 `profileId`、`entities`、`relationships`、`entityTypeCounts`、`unknownEntityTypes`、`resolvedEntities`、`snapshots` 和生成时间。 |
| GOV-UI-13 | 使用 GOV-UI-02 中限制较严格的 Profile 构建快照。 | Profile 只允许少量实体类型。 | `unknownEntityTypes` 列出当前索引中不符合 Profile 的类型；无异常时为空数组。 |
| GOV-UI-14 | 停止 Neo4j 后点击“构建治理快照”，记录失败后恢复 Neo4j 并重试。 | 服务恢复后重试。 | 首次提示可诊断错误；构建记录状态为 `Failed`；恢复后创建新的 Completed 快照。 |

## 9. 图谱治理接口

| ID | 功能 | 操作步骤 | 预期结果 |
|---|---|---|---|
| GOV-API-01 | Graph Profile 列表和详情 | 调用 `GET /api/v1/graph-profiles`，再调用 `GET /api/v1/graph-profiles/<ProfileID>`。 | 返回 `ok: true`；存在 ID 返回详情，不存在 ID 返回 404。 |
| GOV-API-02 | Graph Profile 保存 | 调用 `POST /api/v1/graph-profiles` 创建或更新一个测试 Profile。 | 返回保存后的对象，可通过列表和详情接口读取；`active: true` 时其他 Profile 取消启用。 |
| GOV-API-03 | 实体消歧 | 调用 `POST /api/v1/graph/entity-resolutions`，请求包含实体、标准名、别名、审核人和状态。 | 生成消歧记录；相关 ExtractedEntity 保存规范名称、别名及审核字段。 |
| GOV-API-04 | 实体、快照、质量报告 | 分别调用 `/api/v1/graph/entities`、`/api/v1/graph/snapshots`、`/api/v1/graph/quality-report`。 | 返回结构合法；limit 超过 1000 时按 1000 截断；不存在 ID 返回 404。 |
| GOV-API-05 | 触发治理构建 | 调用 `POST /api/v1/graph-builds`。 | 返回构建状态、snapshotId 和候选/正式层计数；失败时保存 `Failed` 记录和错误原因。 |

## 10. 页面其他功能与回归

| ID | 功能 | 操作步骤 | 预期结果 |
|---|---|---|---|
| REG-01 | 中文编码回归 | 用中文 Evidence/Wiki 执行准备、索引、同步、查询。 | 前端、API JSON、GraphRAG 答案、候选表和 Evidence 引文均为正常中文，无 `���` 或 mojibake。 |
| REG-02 | Neo4j 端口配置 | 确认 Bolt URI 使用 `bolt://127.0.0.1:7687`，而非 HTTP 端口 `7474`。执行同步。 | 同步成功；无“looks like HTTP”错误。 |
| REG-03 | 服务短暂不可用恢复 | 停止 Neo4j 或 EUOS 后执行相应功能，再恢复服务并重试。 | 首次请求有明确错误；恢复后无需重启前端即可成功。 |
| REG-04 | 错误请求 | 对 EUOS 同步、审核、图谱同步、查询接口提交空 JSON、非法状态和非法 method。 | 返回 4xx/错误 JSON；不写入半成品数据，不改变已发布图谱。 |
| REG-05 | 敏感信息 | 检查前端页面、浏览器网络响应、日志和 Git 变更。 | 不出现 EUOS token、Neo4j 密码、模型密钥；`.env`、模型、缓存、运行产物不进入 Git。 |
| REG-06 | 浏览器兼容性 | 使用 Chrome/Edge 的最新稳定版本完成 SMK-01、DAT-05、REV-02、QRY-05。 | 两个浏览器中布局正常、按钮可点击、中文无乱码。 |
| REG-07 | 查询清空 | 完成任意查询后点击“清空”。 | 问题输入框、查询结果和证据链恢复到初始提示；不删除索引、候选或治理数据。 |
| REG-08 | 结果表刷新 | 索引成功后刷新浏览器页面。 | 上游输入状态、索引产物、实体表、关系表、候选和治理面板可重新加载；页面不依赖上一轮浏览器内存。 |
| REG-09 | 响应日志 | 依次执行 EUOS 同步、索引、候选同步、治理快照和混合查询。 | 响应日志展示每次操作的成功/失败摘要；不显示令牌、Neo4j 密码或模型密钥。 |
| REG-10 | 证据链渲染 | 对一个能命中正式关系的问题执行混合查询。 | 证据链分为命中实体、Neo4j 图谱关系、原始文档来源；每条来源尽量含 Wiki、Evidence ID、章节或正文。 |
| REG-11 | 空数据状态 | 在干净测试目录或首次环境加载页面。 | 所有列表显示“暂无数据”或等价空状态；刷新、清空等操作不报前端异常。 |

## 11. 通过标准

1. SMK、DAT、REV、STL、QRY、REG 用例全部通过，或每个失败项均有已登记且评审接受的缺陷。
2. IDX-01 至 IDX-04 至少在一份可追溯的测试数据集上完整通过一次。
3. QRY-07 的正式范围不能返回 Stale、Rejected 或无 Evidence 的关系。
4. REV-05 的“无 Evidence 关系不能发布”必须在前端和接口两端均验证通过。
5. STL-01 至 STL-04 至少验证一条已接受关系的真实版本变更失效链路。
6. GOV-UI-01 至 GOV-UI-14 全部通过，确认页面治理功能与对应的本地持久化、Neo4j 投影及质量统计一致。
