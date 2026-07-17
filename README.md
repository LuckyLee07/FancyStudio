# 唐诗绘卷 · AI 插图批量生产 SOP

一个面向内容编辑、美术指导和生产运营的本地工作台，用于把《唐诗三百首》从结构化内容推进为可追溯、可审核、可交付的插图资产。

产品主线不是“选一首诗立即出图”，而是：

```text
内容校验
→ AI 需求策划
→ 内容编辑审批
→ 叙事 / 意境 / 象征三方向策划
→ 美术指导审批
→ 批次排产与图像生成
→ 自动质检
→ 人工审片与返工
→ 终审、导出与归档
```

## 当前已实现

- 深色高信息密度生产后台；
- 生产总览、阶段漏斗、今日待办和诗词生产表；
- 诗词全链路详情：从内容版本、需求和方向一路追溯到任务、候选、返工、终审、导出与审计；
- 8 个一级工作区：总览、AI 指令、需求板、方向板、队列、审片、成品、资源；
- 五类当前岗位视图，按内容编辑、美术指导、AI 操作员、制片人与系统管理员收敛主操作，并将所选岗位原样交给服务端角色门禁；
- SQLite 生产数据层和可重复 Schema Migration；
- 诗词、内容版本、指令版本、需求卡、方向方案与审计事件；
- AI 指令草稿、角色化发布、历史退役与需求版本冻结；
- 指令版本克隆、与当前发布版逐字段比较，以及带原因的草稿作废；
- Art Bible v1：统一管理色彩、线条、人物比例、空间、材质、文字禁区、历史边界与风格发布政策；
- StylePack v1：语义化版本、发布说明、Art Bible 绑定、视觉特征、人物规范、适用题材、风险、正反例和生成参数；
- Style Lab：从 12 首固定基准诗中选择至少 5 首，每首固定生成 4 张小样，记录成本与样本完整性；
- 风格发布硬门禁：批次完成、样本齐全、风格匹配分和跑题率同时达标后才允许激活；基准图与正式审片、返工、交付链路隔离；
- StylePackVersion 草稿 / 基准测试 / 激活 / 受限 / 退役状态、发布门禁与批次冻结；
- Provider 安全状态页：模型能力、并发、超时、重试和配置状态；
- 7 日生产日报：生成、决策、返工、终审、任务成功率、成本和每日趋势；
- 异常中心聚合失败、阻塞、QC、预算、导出与卡住任务，并带筛选跳转；
- 批量生成结构化需求卡；
- RequirementCard v1 JSON Schema、严格结构校验、一次确定性修复和低置信度人工复核标记；
- 需求生成按 ContentVersion、InstructionVersion、Schema 和生成器版本生成输入指纹，同版本结果可缓存且命中后仍复验；
- 需求生成运行记录保留原始输出、规范化输出、校验明细与错误码；单诗失败不拖垮整批，并进入总览异常、需求板和单诗详情；
- 需求卡版本修订、字段锁定、未锁字段重算、批量批准和带原因退回；
- 每首诗生成叙事型、意境型、象征型三个差异方向；
- DirectionProposal v1 JSON Schema：视觉命题、场景、构图、文字安全区和事实分层均为生产必填字段；
- 三方向以原子集合生成和校验，任意两方向在主体、景别、视觉叙事三轴中至少两项不同，失败时不写入半套结果；
- 诗文事实、合理演绎与创意表达分层保存；事实引用必须能在当前 ContentVersion 中定位；
- 方向生成按 Requirement、Schema 和生成器版本缓存，保留原始输出、一次修复、差异报告、错误码与恢复记录；
- 方向审批门禁：至少一个方向通过后才进入“待排产”；
- 方向版本化编辑、复制、停用与生产后硬锁，历史批次不受新版本影响；
- 方向重生成默认保留锁定字段；一旦已有生产任务，服务端阻止重写方向并要求从候选图走返工链路；
- 方向多选、批量批准和带原因退回，服务端逐条执行并分别保留审计记录；
- Batch、Task、Attempt、UsageRecord 持久化生产模型；
- 批次成本预估、项目预算软提醒与启动 / 重试硬停止；
- 批次创建器、任务明细、失败分类和生产队列；
- 任务明细服务端分页，并支持状态、批次、诗词、作者、错误码和错误文本筛选；
- 批次启动、暂停、继续、取消未开始任务与仅重试失败项；
- 请求幂等键、并发上限、自动重试与重启后结果未知保护；
- Provider 连续失败熔断、同 Provider 批次暂停、自动冷却和结构化异常日志；
- Demo / OpenAI Provider 通过同一持久 Worker 执行并回写候选；
- ProductionImage、QCResult、人工覆盖、审片决策和返工单生产模型；
- 文件完整性、尺寸、比例、SVG 文字和 PNG / SVG 相似指纹质检；
- 硬失败隔离、软风险提示、人工 QC 覆盖和按诗候选路由；
- 按诗分组审片、大图、2–4 张 A/B 对比与键盘快捷决策；
- 结构化审片理由和“保持 / 修改 / 禁止”返工单；
- 返工自动进入高优先级持久队列，并回写母子图谱系与代次；
- 内容终审与美术终审双门禁，且按角色限制可执行的终审类型；
- FinalAsset 版本管理：每首诗只有一个当前交付版，历史版本完整保留；
- 成品库按诗词、作者和风格检索，并展示终审结论、文件规格与版本历史；
- 可重复、不可覆盖的纯图导出包，以及带 SHA-256 的完整 `manifest.json`；
- Manifest 可追溯诗词、需求、方向、Prompt、任务、批次、QC 与双终审；
- Provider-aware 六段式 Prompt 编译、冻结快照、来源版本与 SHA-256 追溯；
- SQLite 与资产目录的原子备份、完整性校验、恢复工具和在线健康检查；
- 旧原型的图像生成、父子衍生、评审、五项质检和本地图库继续可用；
- 无密钥 Demo Renderer 与 OpenAI 图像生成 / 编辑适配器；
- 生产数据重启持久化，以及阶段变更的操作者和前后值审计。

详细范围与迭代计划见：

- [产品设计方案](./唐诗三百首_AI插图生成器_产品设计方案.md)
- [TODO 迭代开发文档](./唐诗插图批量生产_SOP系统_TODO迭代开发文档.md)

## 立即运行

项目当前只依赖 Python 标准库：

```bash
python3 server.py
```

浏览器访问：

```text
http://localhost:8000
```

首次启动会：

1. 创建 `data/studio.db`；
2. 执行数据库迁移；
3. 导入当前 12 首基准诗、12 首风格测试集和 6 个基线风格包；
4. 发布 Art Bible v1 与全局创作指令 v1；
5. 保留旧原型的演示候选图库。

## 开启真实图像生成

```bash
export OPENAI_API_KEY="你的 API 密钥"
python3 server.py
```

可选配置：

```bash
export OPENAI_IMAGE_MODEL="gpt-image-2"
export OPENAI_IMAGE_QUALITY="medium"
```

也可以固定运行模式：

```bash
AI_PROVIDER=demo python3 server.py
AI_PROVIDER=openai python3 server.py
```

真实生成会产生 API 费用。接口失败会保留错误，不会静默切换到演示图。

## 数据结构

```text
FancyStudio/
├─ server.py                  HTTP API、旧图像生成链路与静态文件服务
├─ sop_store.py               SQLite 生产领域、门禁、队列、预算与审计
├─ prompt_compiler.py         六段式 Prompt 编译、Provider 模板、来源版本与哈希
├─ requirement_schema.py      RequirementCard v1 校验、一次修复与置信度规则
├─ direction_schema.py        DirectionProposal v1、事实分层与三方向差异门禁
├─ style_schema.py            Art Bible v1 / StylePack v1 合同与发布字段校验
├─ schemas/
│  ├─ requirement-card.schema.json  需求卡 JSON Schema
│  ├─ direction-proposal.schema.json 画面方向 JSON Schema
│  ├─ art-bible.schema.json         全局视觉圣经 JSON Schema
│  └─ style-pack.schema.json        风格包 JSON Schema
├─ qc_engine.py               离线技术质检、格式解析与相似指纹
├─ backup_service.py          数据库与资产备份、校验和安全恢复
├─ backup_tool.py             离线备份 / 列表 / 校验 / 恢复命令
├─ public/
│  ├─ index.html              SOP 生产工作台
│  ├─ app.js                  批量选择、审批、看板与评审交互
│  └─ styles.css              深色后台视觉与响应式布局
├─ data/
│  ├─ studio.db               新生产流程主数据
│  ├─ poems.json              当前基准诗种子数据
│  ├─ benchmark_poems.json    12 首风格测试集与误读 / 历史风险标签
│  ├─ art_bible.json          Art Bible v1 种子数据
│  ├─ styles.json             当前风格包种子数据
│  ├─ state.json              旧生成任务与图片数据，待迁入 SQLite
│  ├─ generated/              本地生成图片
│  ├─ exports/                不可覆盖的交付包与 Manifest
│  └─ backups/                数据库和资产备份包
└─ tests/
   ├─ test_sop_store.py       状态、门禁、批次、预算、恢复与审计
   ├─ test_qc_engine.py       文件、规格、文字和相似指纹质检
   ├─ test_performance.py     300 首分页与 1000 任务批次性能门禁
   ├─ test_prompt_compiler.py 确定性编译、来源版本与快照哈希
   ├─ test_requirement_schema.py Schema、修复、缓存、隔离失败与恢复
   ├─ test_direction_schema.py 三方向合同、差异门禁、原子写入与恢复
   ├─ test_style_schema.py    Art Bible / StylePack 合同与语义版本校验
   ├─ test_frontend_contract.py 页面与脚本契约
   └─ test_server.py          HTTP API、生成、编辑、评审与完整流程
```

当前仍处于旧原型兼容期：

- 生产阶段、需求、方向、Batch、Task、Attempt、UsageRecord、ProductionImage、QC、审片 / 返工、FinalAsset 和 ExportPackage 均以 SQLite 为准；
- 图片二进制元数据与旧单图任务暂时继续从 `state.json` 读取；
- 后续仅需迁移旧单图任务和旧图库元数据，最终移除双轨状态。

## 备份与恢复

服务运行时可在“资源”页创建并校验备份。离线也可以使用：

```bash
python3 backup_tool.py create
python3 backup_tool.py list
python3 backup_tool.py verify data/backups/<备份目录>
python3 backup_tool.py restore data/backups/<备份目录> --target <空目录>
```

恢复命令只允许写入空目标目录，避免覆盖现有生产数据。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：

- SQLite 初始化与重启恢复；
- 需求生成、修订、批准和退回；
- RequirementCard Schema、一次修复、缓存命中、批量部分失败、异常下钻和恢复；
- 需求批量决策、锁字段重算和旧方向失效；
- 三方向生成与审批门禁；
- DirectionProposal Schema、事实分层、三方向两两差异、缓存、原子失败和恢复；
- 方向修订、复制、停用、字段锁和生产冻结；
- 批次预估、持久任务、Attempt、预算硬停和暂停 / 恢复；
- 服务中断后的结果未知保护与显式确认重试；
- 批次 API 到 Worker 产图回写的端到端流程；
- 技术 QC、重复隔离、人工覆盖和候选路由；
- 结构化审片、自动返工任务与二代子图谱系；
- 内容 / 美术双终审、FinalAsset 唯一当前版本与历史版本；
- 重复导出不覆盖、Manifest 全链路与文件校验和；
- 数据库 / 资产备份、校验和空目录恢复；
- 指令 / 风格不可变版本、发布门禁及批次冻结回归；
- Art Bible / StylePack 合同、12 首基准集、每诗 4 张样本、评分阈值和风格发布门禁；
- 风格基准图与正式审片、返工、成品及生产日报隔离；
- 指令克隆、差异审阅和带原因草稿作废；
- 六段式 Prompt 编译快照、哈希和 Manifest 贯通；
- 300 首数据、1000 任务批次、分页、总览与报表性能门禁；
- 审计记录；
- 新 SOP HTTP API；
- 旧生成 / 编辑适配器和评审质量门槛；
- 页面导航和 JavaScript 元素契约。

测试不会访问外部图像接口，也不会产生费用。

## 当前边界

- 当前种子数据为 12 首基准诗，尚未导入完整 300 首生产数据；
- Requirement 与 Direction 当前使用可测试的结构化本地策划器，真实文本模型适配将在后续接入；
- 风格基准流程已强制样本数量与人工评分门槛；语义风格匹配、跑题和构图多样性仍需视觉模型与人工标注集校准；
- 当前本地 QC 已覆盖可证明的技术检查；栅格文字 / 品牌识别以及语义、历史、美术模型仍需后续视觉模型和人工标注集校准；
- 当前只支持纯图导出，诗卡、课程配图和多规格派生仍待实现；
- 当前为单机单用户版本；岗位切换用于本地 SOP 分工演示和服务端门禁联调，不是身份认证，也不具备公网部署所需的登录、完整权限和速率限制；
- 历史合理性仍需内容编辑或顾问人工终审。
