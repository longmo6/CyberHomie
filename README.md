# CyberHomie

QQ 群数字群友 AI —— 一个长期潜伏在 QQ 群中的拟人化群成员。

不是助手型机器人，而是像真实群友一样出没、聊天、记忆、建立关系。

## 核心特性

### 随机出没模式
- 每天随机活跃若干次，每次 3-10 分钟
- 活跃期内主动参与群聊讨论
- 非活跃时间只回复 @-mention
- 被 @ 后进入 3 分钟高活跃状态，参与度逐渐衰减

### LLM 驱动决策
- 消息缓冲后批量交给 LLM 决定回复哪些
- LLM 区分消息对象：不插嘴别人之间的对话
- 参与度影响回复倾向：高活跃时积极，低活跃时沉默
- 根据回复字数模拟打字延迟

### 长期记忆系统
- **用户记忆** (`data/memory/<qq_id>.md`)：每人独立档案，记录性格、兴趣、口头禅、昵称等
- **群记忆** (`data/group_memory/<group_id>.md`)：群氛围、核心人物、互称方式、群梗
- 活跃期结束时自动用 LLM 总结更新
- 记忆文件可直接手动查看和编辑

### 人格系统
- 人格配置文件 `config/personality.yaml`
- 温和软糯的性格，不攻击不骂人
- 被质疑是 AI 时装傻
- 口语化表达，去助手化

### 私聊支持
- 私聊 always 秒回，无延迟
- 私聊记忆与群聊互通（同一 user_id）
- 私聊更随意，像跟熟人单独聊天

### 引用回复
- 仅在被 @ 时引用回复
- 其他情况直接发送，不引用

## 项目结构

```
CyberHomie/
├── main.py                      # 入口，FastAPI + WebSocket + 终端指令
├── config.py                    # 配置（pydantic-settings）
├── config/personality.yaml      # 人格配置
├── .env                         # 环境变量（不入 git）
├── core/
│   ├── napcat.py                # NapCat HTTP API 客户端
│   ├── event_handler.py         # OneBot 11 事件解析
│   └── scheduler.py             # 后台定时任务
├── memory/
│   ├── database.py              # SQLite 数据库
│   ├── user_memory.py           # 用户数据 CRUD
│   ├── group_memory.py          # 群聊记录
│   ├── user_file_memory.py      # 用户长期记忆文件
│   ├── group_file_memory.py     # 群长期记忆文件
│   └── relationship.py          # 关系图谱
├── personality/
│   └── persona.py               # 人格系统提示词生成
├── humanizer/
│   └── humanizer.py             # 消息缓冲 + 参与度 + 出没调度
├── llm/
│   └── mimo.py                  # LLM 客户端（OpenAI 兼容）
└── utils/
    └── logger.py                # 日志
```

## 快速开始

### 1. 安装依赖

```bash
conda activate MIKU01
pip install -r requirements.txt
```

### 2. 配置 .env

复制 `.env.example` 为 `.env`，填入：

```env
BOT_QQ_ID=你的机器人QQ号
TARGET_GROUP_ID=目标群号
MIMO_API_KEY=你的API密钥
NAPCAT_PATH=NapCat启动器路径（可选，自动启动）
```

### 3. 配置 NapCat

- HTTP 服务器：端口 3000
- WebSocket 客户端：`ws://127.0.0.1:8765/onebot/ws`
- 消息格式：Array

### 4. 启动

```bash
python main.py
```

## 终端指令

运行时可直接输入：

| 指令 | 说明 |
|------|------|
| `help` | 查看所有指令 |
| `users` | 列出所有用户 |
| `status` | 查看当前状态（参与度、缓冲区） |
| `user <qq_id>` | 查看用户详情 |
| `memory <qq_id>` | 查看用户记忆文件 |
| `group` | 查看群记忆文件 |
| `edit <qq_id> <字段> <值>` | 编辑用户字段 |
| `history <qq_id>` | 查看聊天记录 |
| `sessions` | 查看群记忆数据库 |
| `summarize` | 立即总结所有用户 + 群记忆 |
| `summarize <qq_id>` | 总结指定用户 |
| `summarize group` | 总结群记忆 |

## 回复逻辑

```
消息进来
  ├── @-mention → 参与度=100，立即交给 LLM 决策
  └── 普通消息 → 加入缓冲区
                  ↓
         每 10 秒 或 每 3 条消息评估一次
                  ↓
         参与度 > 70% → LLM 提示"积极聊天"
         参与度 30-70% → LLM 提示"只回有意思的"
         参与度 < 30% → LLM 提示"基本不聊了"
                  ↓
         LLM 决定回复哪些、回复什么、是否引用
```

参与度从 100 衰减到 0 约 3 分钟，bot 回复时 +20 保持对话连贯。

## 配置说明

### .env

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `BOT_QQ_ID` | 机器人 QQ 号 | - |
| `TARGET_GROUP_ID` | 目标群号 | - |
| `MIMO_API_KEY` | LLM API 密钥 | - |
| `MIMO_BASE_URL` | API 地址 | `https://api.xiaomimimo.com/v1` |
| `MIMO_MODEL` | 模型名 | `mimo-v2.5` |
| `BASE_REPLY_PROBABILITY` | 活跃期基础回复概率 | `0.15` |
| `SESSION_GAP_MIN/MAX` | 出没间隔（分钟） | `20-90` |
| `SESSION_DURATION_MIN/MAX` | 每次活跃时长（分钟） | `3-10` |
| `ACTIVE_HOUR_START/END` | 活跃时段 | `10-2`（凌晨2点到上午10点安静） |
| `NAPCAT_PATH` | NapCat 启动器路径 | 空（手动启动） |

### config/personality.yaml

直接编辑即可修改人格，重启生效：
- `name` - 昵称
- `traits` - 性格特征
- `style_rules` - 说话风格
- `persona_description` - 人设描述
- `forbidden_patterns` - 禁词
- `private_chat_extra` - 私聊附加提示

## 技术栈

- Python 3.9+
- FastAPI + Uvicorn
- NapCat (OneBot 11)
- aiosqlite
- OpenAI 兼容 API (MiMo)
- APScheduler

## License

MIT
