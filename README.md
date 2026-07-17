# 唐诗绘卷 · AI 插图工作台

一个可以在本机直接运行的唐诗插图 SOP 工作台。它把“生成图片”扩展为一条可追踪、可评审、可交付的创作流程：

- 10 首结构化唐诗与诗意画面策划；
- 项目化管理诗词范围、交付用途、统一画幅与风格基线；
- 6 套原创美术风格、固定色板、材质规范与参考样图；
- “探索方向 / 收敛迭代”双模式，收敛任务必须指定父候选、保留项与修改项；
- 本地异步生成任务与进度展示；
- 候选入选、淘汰、反馈标签、评审备注和父子版本链；
- 诗意、史实、系列一致性、画面完整、排版安全五项成品质检；
- 原图保存和带诗文排版的 PNG 诗画卡导出；
- 无需密钥的离线演示生成；
- 配置 `OPENAI_API_KEY` 后调用 OpenAI 图像接口真实生成；收敛模式会把父候选作为高保真图片输入；
- 通过 `0.0.0.0` 启动供同一局域网内其他设备访问。

## 立即运行

项目只依赖 Python 标准库，不需要安装 npm 或 Python 包。

```bash
python3 server.py
```

然后访问：

```text
http://localhost:8000
```

首次启动会自动创建一个“唐诗十首 · 视觉基线”项目和不同风格的演示插图。之后从“创作台”按以下 SOP 推进：

1. 定义项目边界；
2. 用代表诗建立视觉基线；
3. 探索差异化构图；
4. 选择父候选进行定向收敛；
5. 完成五项质检并导出诗画卡。

## 开启真实 AI 生成

先在本机终端设置 API 密钥，不要把密钥写入代码或提交到 Git：

```bash
export OPENAI_API_KEY="你的 API 密钥"
python3 server.py
```

服务检测到密钥后会自动从 `demo` 切换到 `openai`。探索任务调用图像生成接口，收敛任务调用图像编辑接口并上传选中的 PNG、JPEG 或 WebP 父候选。默认模型和质量可以调整：

```bash
export OPENAI_IMAGE_MODEL="gpt-image-2"
export OPENAI_IMAGE_QUALITY="medium"
python3 server.py
```

也可以显式指定运行模式：

```bash
AI_PROVIDER=demo python3 server.py
AI_PROVIDER=openai python3 server.py
```

真实生成可能产生 API 费用。接口失败时，失败原因会保留在“生成任务”面板中；系统不会静默改用演示图。

## 局域网访问

监听全部本地网卡：

```bash
python3 server.py --host 0.0.0.0 --port 8000
```

查询 Mac 的 Wi-Fi 局域网 IP：

```bash
ipconfig getifaddr en0
```

如果返回例如 `192.168.1.23`，同一 Wi-Fi 下的手机或平板可以访问：

```text
http://192.168.1.23:8000
```

如果无法访问，请检查 macOS 防火墙是否允许 Python 接收入站连接。不要把本服务直接暴露到公网；当前版本没有账号、权限和速率限制。

## 数据与文件

```text
FancyStudio/
├─ server.py                 本地 API、任务执行与静态文件服务
├─ public/
│  ├─ index.html             产品界面
│  ├─ styles.css             视觉与响应式布局
│  └─ app.js                 项目、SOP、评审、任务轮询与导出
├─ data/
│  ├─ poems.json             诗词与画面策划
│  ├─ styles.json            风格包
│  ├─ state.json             本地项目、任务、版本链与评审索引（运行后生成）
│  └─ generated/             生成的 SVG / PNG 文件
└─ tests/
   └─ test_server.py         本地完整链路测试
```

删除 `data/state.json` 和 `data/generated/` 内的文件即可重置本地作品库；下次启动会重新建立演示画廊。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试会在临时目录启动本地 HTTP 服务，验证项目创建、探索与收敛约束、父子版本链、评审状态、五项质检门槛、异步生成，以及真实生成/编辑接口的请求适配，不会访问外部网络或产生费用。

## 当前边界

- 演示模式使用本地 SVG 渲染器验证产品流程，不代表最终 AI 画质；
- 真实生成需要可用的 OpenAI API 账户、密钥和网络；
- 当前是单机单用户 SOP 产品，没有登录、团队权限或公网部署安全能力；
- 历史合理性约束会进入提示词，但最终成品仍需人工审阅。
