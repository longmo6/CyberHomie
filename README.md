# CyberHomie

一个高度拟人化的 QQ 数字群友。

不是聊天机器人，不是 AI 助手。CyberHomie 是一个会长期潜伏在你的 QQ 群里、拥有独立人格和记忆、会随机出没参与聊天的"老群友"。支持多群同时活跃，群聊和私聊都能自然融入。

## 核心机制

### 随机出没

真人不会 24 小时在线。CyberHomie 采用深夜/白天差异化出没策略：

| | 深夜（0:00-8:00） | 白天（8:00-0:00） |
|--|--|--|
| 出没间隔 | 10-30 分钟 | 40-90 分钟 |
| 每次活跃 | 5-15 分钟 | 2-5 分钟 |
| 初始参与度 | 50 | 30 |

被 @ 后进入 3 分钟高活跃状态，参与度从 100 逐渐衰减到 0，模拟"聊着聊着就懒得回了"。活跃期间如果 10 分钟没人说话，bot 会主动发起话题。

### 记忆驱动

为每个互动过的人维护独立长期记忆档案（`data/memory/<qq_id>.md`），记录性格、兴趣、口头禅、昵称等。每个群也有独立记忆（`data/group_memory/<group_id>.md`），记录群氛围、核心人物、互称方式、群梗。

群聊和私聊的记忆互通——同一个 QQ 号在不同场景的印象会合并。

记忆由 LLM 定期总结为简洁条目，存储在可直接查看和编辑的 Markdown 文件中。

### LLM 决策

不做概率随机回复。消息先缓冲收集，每 10 秒或每 3 条消息批量交给 LLM，由 LLM 判断哪些值得回复。LLM 能区分哪些消息是发给自己的、哪些是别人之间的对话，避免强行插嘴。

参与度影响 LLM 的决策倾向：高活跃时积极回复，低活跃时选择性参与，完全空闲时保持沉默。

## 其他特性

- **人格可配置**：`config/personality.yaml` 直接编辑，重启生效（或 `reload` 热重载）
- **多群支持**：同时在多个群活跃，每群独立状态
- **私聊秒回**：私聊无延迟，记忆与群聊互通
- **引用回复控制**：仅在被 @ 时引用
- **打字延迟**：根据回复字数模拟 1-5 秒打字时间
- **去助手化**：口语化、偶尔敷衍、被质疑时装傻
- **情绪波动**：每次回复随机心情（开心/困/害羞/emo/生气）

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置
cp .env.example .env
# 编辑 .env 填入 BOT_QQ_ID、TARGET_GROUP_IDS、MIMO_API_KEY

# 3. 启动
python main.py
```

NapCat 配置：HTTP 服务器端口 3000，WebSocket 客户端 `ws://127.0.0.1:8765/onebot/ws`，消息格式 Array。

## 终端指令

运行时直接输入：

| 指令 | 说明 |
|------|------|
| `help` | 查看所有指令 |
| `users` | 列出所有用户 |
| `status` | 当前状态（所有群的参与度、缓冲区、深夜模式） |
| `user <qq_id>` | 用户详情 |
| `memory <qq_id>` | 用户记忆文件 |
| `group [群号]` | 群记忆文件 |
| `edit <qq_id> <字段> <值>` | 编辑用户字段 |
| `history <qq_id>` | 聊天记录 |
| `sessions [群号]` | 群记忆数据库 |
| `summarize` | 立即总结所有记忆 |
| `summarize <qq_id>` | 总结指定用户 |
| `summarize group` | 总结所有群记忆 |
| `say [群号] <消息>` | 以 bot 身份发群消息 |
| `engage [群号] [0-100]` | 查看/设置参与度 |
| `session start <群号> [分钟]` | 手动开启活跃期 |
| `session stop <群号>` | 手动结束活跃期 |
| `buffer [群号]` | 查看消息缓冲区 |
| `rel <qq_id> \| <qq_a> <qq_b>` | 查看关系 |
| `reload` | 热重载人格配置 |
| `test <消息>` | 测试 LLM 回复（不发送） |
| `debug` | 切换详细日志 |

## 项目结构

```
main.py                      入口 + WebSocket + 终端指令
config.py                    环境配置（支持多群号逗号分隔）
config/personality.yaml      人格配置（性格/风格/禁词/情绪示例/打字习惯）
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
personality/persona.py       人格系统提示词生成
humanizer/humanizer.py       消息缓冲 + 参与度衰减 + 深夜/白天出没调度
llm/mimo.py                  LLM 客户端（话题生成、批量决策、记忆总结）
```

## 配置

### .env

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `BOT_QQ_ID` | 机器人 QQ 号 | - |
| `TARGET_GROUP_IDS` | 目标群号（逗号分隔） | - |
| `MIMO_API_KEY` | LLM API 密钥 | - |
| `MIMO_BASE_URL` | API 地址 | `https://api.xiaomimimo.com/v1` |
| `MIMO_MODEL` | 模型名 | `mimo-v2.5` |
| `ACTIVE_HOUR_START/END` | 活跃时段 | `10-2` |
| `SESSION_GAP_MIN/MAX` | 白天出没间隔（分钟） | `40-90` |
| `SESSION_DURATION_MIN/MAX` | 白天活跃时长（分钟） | `2-5` |
| `NAPCAT_PATH` | NapCat 启动器路径 | 空 |

深夜参数（0:00-8:00）在 `humanizer/humanizer.py` 顶部常量区配置。

### config/personality.yaml

直接编辑修改人格。包含：
- `name` - 昵称
- `traits` - 性格特征
- `style_rules` - 说话风格
- `persona_description` - 人设描述
- `forbidden_patterns` - 禁词
- `mood_examples` - 情绪示例（happy/sleepy/shy/emo/angry）
- `typing_habits` - 打字习惯

## 技术栈

Python 3.9+ / FastAPI / NapCat (OneBot 11) / aiosqlite / OpenAI 兼容 API / APScheduler

## License

MIT
