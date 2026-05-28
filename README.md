# CyberHomie

一个高度拟人化的 QQ 数字群友。

不是聊天机器人，不是 AI 助手。CyberHomie 是一个会长期潜伏在你的 QQ 群里、拥有独立人格和记忆、会随机出没参与聊天的"老群友"。群聊和私聊都能自然融入。

## 核心理念

传统聊天机器人的问题：每条消息都秒回、语气像客服、没有记忆、永远在线。CyberHomie 通过三个核心机制解决这些问题，最大化真人感。

### 1. 随机出没

真人不会 24 小时在线。CyberHomie 每天会随机"上线"几次，每次活跃 3-10 分钟，间隔 20-90 分钟。活跃期间像正常群友一样参与聊天，其余时间只回复被 @ 的消息。

被 @ 后进入 3 分钟高活跃状态，参与度随时间逐渐衰减，模拟"聊着聊着就懒得回了"的真实行为。

### 2. 记忆驱动的长期关系

CyberHomie 为每个互动过的人维护独立的长期记忆档案，记录性格、兴趣、口头禅、情绪倾向、和谁关系好等。群聊和私聊的记忆互通——同一个 QQ 号在不同场景下的印象会合并。

每个群也有独立的群记忆，记录群氛围、核心人物、互称方式、群梗等。

记忆不是聊天记录的堆砌，而是定期由 LLM 总结为简洁的条目档案，存储在可直接查看和编辑的 Markdown 文件中：

```
data/memory/1911576972.md    ← 用户记忆
data/group_memory/149392146.md  ← 群记忆
```

### 3. LLM 驱动的消息决策

不做概率随机回复。消息先缓冲收集，每 10 秒或每 3 条消息批量交给 LLM，由 LLM 判断哪些值得回复。LLM 能区分哪些消息是发给自己的、哪些是别人之间的对话，避免强行插嘴。

参与度影响 LLM 的决策倾向：高活跃时积极回复，低活跃时选择性参与，完全空闲时保持沉默。

## 其他特性

- **人格可配置**：`config/personality.yaml` 直接编辑性格、说话风格、禁词，重启生效
- **私聊秒回**：私聊无延迟，记忆与群聊互通
- **引用回复控制**：仅在被 @ 时引用，其他情况直接发送
- **打字延迟**：根据回复字数模拟 1-5 秒的打字时间
- **去助手化**：口语化表达、偶尔敷衍、被质疑时装傻

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置
cp .env.example .env
# 编辑 .env 填入 BOT_QQ_ID、TARGET_GROUP_ID、MIMO_API_KEY

# 3. 启动（会自动拉起 NapCat，如果配置了 NAPCAT_PATH）
python main.py
```

NapCat 配置：HTTP 服务器端口 3000，WebSocket 客户端地址 `ws://127.0.0.1:8765/onebot/ws`，消息格式 Array。

## 终端指令

运行时直接输入：

| 指令 | 说明 |
|------|------|
| `help` | 查看所有指令 |
| `users` | 列出所有用户 |
| `status` | 当前状态（参与度、缓冲区） |
| `user <qq_id>` | 用户详情 |
| `memory <qq_id>` | 用户记忆文件 |
| `group` | 群记忆文件 |
| `edit <qq_id> <字段> <值>` | 编辑用户字段 |
| `history <qq_id>` | 聊天记录 |
| `summarize` | 立即总结所有记忆 |
| `summarize <qq_id>` | 总结指定用户 |
| `summarize group` | 总结群记忆 |

## 项目结构

```
main.py                      入口 + WebSocket + 终端指令
config.py                    环境配置
config/personality.yaml      人格配置
core/
  napcat.py                  NapCat API 客户端
  event_handler.py           OneBot 11 事件解析
  scheduler.py               定时任务（群聊摘要、用户画像）
memory/
  database.py                SQLite 数据库
  user_memory.py / group_memory.py   结构化记忆 CRUD
  user_file_memory.py        用户长期记忆文件
  group_file_memory.py       群长期记忆文件
  relationship.py            关系图谱
personality/persona.py       人格系统提示词
humanizer/humanizer.py       消息缓冲 + 参与度 + 出没调度
llm/mimo.py                  LLM 客户端
```

## 配置

### .env

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `BOT_QQ_ID` | 机器人 QQ 号 | - |
| `TARGET_GROUP_ID` | 目标群号 | - |
| `MIMO_API_KEY` | LLM API 密钥 | - |
| `MIMO_BASE_URL` | API 地址 | `https://api.xiaomimimo.com/v1` |
| `MIMO_MODEL` | 模型名 | `mimo-v2.5` |
| `ACTIVE_HOUR_START/END` | 活跃时段 | `10-2` |
| `SESSION_GAP_MIN/MAX` | 出没间隔（分钟） | `20-90` |
| `SESSION_DURATION_MIN/MAX` | 每次活跃时长（分钟） | `3-10` |
| `NAPCAT_PATH` | NapCat 启动器路径（空则手动启动） | - |

### config/personality.yaml

直接编辑修改人格，重启生效。包含性格特征、说话风格、人设描述、禁词等。

## 技术栈

Python 3.9+ / FastAPI / NapCat (OneBot 11) / aiosqlite / OpenAI 兼容 API / APScheduler

## License

MIT
