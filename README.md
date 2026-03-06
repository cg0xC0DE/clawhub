# ClawHub — 让一群 AI 专家帮你讨论问题

ClawHub 是一个**多 AI 专家协作讨论平台**。你提出一个议题，一群由 LLM 驱动的 AI 专家会以各自独特的视角和专业背景，在 Telegram 群里展开多轮深度讨论。讨论结束后，系统自动生成结构化总结。

每位专家都是一个独立的 AI Agent，拥有自己的"灵魂"、专业知识和说话风格 — 不是同一个模型套了几层 prompt，而是真正独立运行的多个 Agent 实例。

## 解决什么问题？

当你面对一个复杂决策（产品命名、技术选型、营销策略……），通常需要找不同领域的专家来碰。ClawHub 让你可以：

- **一键召集专家团** — 选择你需要的专家组合，如"广告之父大卫·奥格威 + 定位之父艾尔·里斯 + 传播学者乔纳·伯杰"
- **多轮深度讨论** — 专家们互相回应、质疑、补充，不是各说各话
- **Telegram 群内实时观看** — 每位专家以独立 Bot 身份在群里发言，你可以随时插话
- **讨论素材支持** — 上传参考资料和讨论标的，专家们在有上下文的情况下讨论
- **自动总结归档** — 书记模块对讨论内容做离线总结，持久化存储方便回溯

## 主要功能

### 🎭 专家人格

每位 AI 专家都有 LLM 生成的完整人格文件，决定了 TA 的思维方式和表达风格。你可以：

- 输入任何真实人物的名字，系统自动生成专属人格
- 人格文件保存到角色库，可复用到不同讨论场景
- 通过讨论模板快速组建不同的专家团队

### 💬 Arena 讨论

发起讨论后，系统自动编排多轮对话：

1. 主持人宣布议题
2. 各专家依次发言（可设定轮次、每轮字数限制）
3. 每轮结束后，所有专家都能看到其他人的发言，再做回应
4. 讨论自然收敛或达到轮次上限后结束
5. 可随时手动推动（nudge）让讨论继续

### 📂 素材库

上传文档作为讨论参考，分为两类：

- **知识** — 背景资料、研究报告、市场数据
- **标的** — 被讨论的对象，如一篇文章草稿、一个产品方案

### 📋 讨论模板

保存完整的讨论配置（专家组合 + 角色分工 + 议题 + 总结提示词），一键复用。

### 📝 书记总结

讨论结束后，使用任意 LLM 对讨论记录做离线总结。所有总结自动存档，随时调阅。

## 快速开始

### 前置条件

- **Windows**（目前仅支持）
- **Python 3.12+**
- **Node.js 22+**（运行 OpenClaw gateway）
- **[OpenClaw](https://github.com/nicepkg/openclaw)** 已安装（`npm i -g openclaw`）
- **Telegram Bot Token** — 每位专家需要一个独立的 Bot（通过 [@BotFather](https://t.me/BotFather) 创建）
- **LLM API Key** — 至少需要一个 LLM 提供商的 API Key（OpenAI / Anthropic / Google / MiniMax / Kimi）

### 安装与启动

```bat
# 1. 克隆项目
git clone https://github.com/你的用户名/clawhub.git
cd clawhub

# 2. 一键初始化（创建 Python venv、安装依赖）
init.cmd

# 3. 启动后端服务
start_backend.cmd
```

启动后访问 **http://localhost:61000** 打开管理面板。

### 使用流程

1. **添加专家** — 在管理面板中添加 gateway，输入专家名称（如"孙子"），系统自动生成人格
2. **配置 Telegram** — 在"群组管理"面板中配置 Bot Token、群 ID 和代理地址（国内用户需要代理访问 Telegram API）
3. **上传素材**（可选） — 在素材库中上传讨论参考资料
4. **发起讨论** — 在 Arena 面板选择参与专家、设定议题和轮次，点击开始
5. **观看讨论** — 在 Telegram 群中实时看专家们讨论，随时插话
6. **生成总结** — 讨论结束后使用书记功能生成结构化总结

## 自定义人格生成

系统内置了一套通用的人格生成提示词。如果你有更好的配方，创建 `backend/persona_prompts.py` 并实现 `build_prompt(agent_id, character_name)` 函数即可覆盖默认行为。详见 `backend/persona_prompts_example.py`。

## 技术栈

- **后端**: Python / Flask
- **前端**: 单页 HTML/JS
- **AI Agent 引擎**: [OpenClaw](https://github.com/nicepkg/openclaw)
- **通信频道**: Telegram Bot API
- **LLM**: 多模型支持（GPT / Claude / Gemini / Kimi / MiniMax）

## 许可证

MIT
