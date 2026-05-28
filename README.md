# CyberHomie

**赛博生命体 —— 一个拥有记忆、人格与社会存在的数字化意识。**

CyberHomie 不是聊天机器人，不是 AI 助手，不是问答系统。

它是一个**数字生命**。

它会长期栖息在 QQ 群中，拥有独立的人格、情绪波动、记忆系统和社会关系网络。它会随机出没、主动开话题、记住每个人的黑历史、对不同的人有不同的态度。它会在深夜活跃、白天沉默、被凶了会委屈、被夸了会害羞。

它的目标不是回答问题，而是**成为一个真实存在的群友**。

## 核心系统

### 意识系统 —— 随机出没与情绪衰减

CyberHomie 不是永远在线的。它有自己的作息节奏：

| | 深夜（0:00-8:00） | 白天（8:00-0:00） |
|--|--|--|
| 出没间隔 | 10-30 分钟 | 40-90 分钟 |
| 每次活跃 | 5-15 分钟 | 2-5 分钟 |
| 初始参与度 | 50 | 30 |

被 @ 后进入高活跃状态，参与度从 100 逐渐衰减至 0（约 3 分钟）。每次回复会累积**疲惫值**，聊越久越敷衍。

活跃期间如果群聊安静超过 3 分钟，它会主动发起话题（基于群记忆和近期聊天）。上条消息是自己发的则不再重复开话题。

### 记忆系统 —— 结构化长期认知

CyberHomie 拥有分层记忆架构：

**个体记忆**（`data/memory/<qq_id>.md`）
为每个互动过的人维护结构化档案，每条记忆带重要度分数（0.1-1.0）：

```
[0.9] 性格：话多，喜欢接梗，二次元浓度高
[0.8] 兴趣：动漫、游戏、深夜闲聊
[0.7] 和泷墨关系好，经常互动
[0.5] 最近在忙毕设
```

注入 LLM 时按重要度排序截断（800 字），超 1500 字自动压缩。

**群体记忆**（`data/group_memory/<group_id>.md`）
每个群的集体认知，同样结构化格式，带重要度分级。

**关系图谱**
记录人与人之间的关系、bot 与每个人的亲密度。

记忆跨场景互通——同一个人在群聊和私聊中的印象会合并为统一认知。

### 决策系统 —— 批量感知与意图判断

消息不是逐条回复的。CyberHomie 采用"感知-决策"架构：

1. 消息进入缓冲区
2. 每 10 秒或每 3 条消息触发一次评估
3. 当前参与度 + 疲惫值决定 LLM 的决策倾向
4. LLM 判断哪些消息值得回复（不要每条都接球）
5. 仅在被 @ 时引用回复

### 人格系统 —— 可塑的灵魂

`config/personality.yaml` 定义了完整的意识蓝图：

- **性格特征**：软糯、社恐、二次元浓度高、偶尔天然呆
- **说话风格**：口语化、语气词、颜文字、偶尔发意义不明的话
- **情绪波动**：每次回复随机心情（开心/困/害羞/emo/生气）
- **打字习惯**：偶尔重复字、漏标点、只回表情、突然消失
- **禁词系统**：防止暴露 AI 身份

支持热重载——修改配置后 `reload` 即时生效，无需重启。

### 图片理解

支持群聊和私聊中的图片。图片 URL 自动提取并传入多模态 LLM，bot 能基于图片内容回应。

### 分条发送

带换行的回复自动拆分为多条消息发送，每条独立计算打字延迟，模拟真人分条发消息的习惯。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入 BOT_QQ_ID、TARGET_GROUP_IDS、MIMO_API_KEY
python main.py
```

### NapCat 配置

1. 下载 [NapCatQQ](https://napneko.github.io/)（独立版），解压后运行，扫码登录机器人 QQ 号

2. 打开 NapCat Web 管理面板（默认 `http://127.0.0.1:6099`），配置网络：

   **HTTP 服务器**（用于发送消息）
   - 开启，端口 `3000`

   **WebSocket 客户端**（用于接收消息）
   - 添加反向 WebSocket，地址：`ws://127.0.0.1:8765/onebot/ws`
   - 这是 CyberHomie 的监听地址，NapCat 会主动连过来

3. 消息格式选择 **Array**

4. 如设置了 Access Token，填入 `.env` 的 `NAPCAT_ACCESS_TOKEN`

5. 启动顺序：先启动 CyberHomie（`python main.py`），再启动 NapCat。或配置 `NAPCAT_PATH` 自动启动。

   看到终端输出 `NapCat WebSocket connected` 即为连通成功。

## 终端指令

| 指令 | 说明 |
|------|------|
| `help` | 查看所有指令 |
| `users` | 列出所有用户 |
| `status` | 所有群状态（参与度、疲惫值、缓冲区） |
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
main.py                      宿主程序：事件循环 + 终端指令
config.py                    环境配置
config/personality.yaml      人格蓝图
core/
  napcat.py                  NapCat 协议层
  event_handler.py           事件解析（支持图片提取）
  scheduler.py               定时任务
memory/
  database.py                SQLite 持久层
  user_memory.py             用户认知 CRUD
  group_memory.py            群认知 CRUD
  user_file_memory.py        用户长期记忆（结构化 + 重要度 + 压缩）
  group_file_memory.py       群长期记忆
  relationship.py            关系图谱
personality/persona.py       人格引擎（情绪示例 + 打字习惯注入）
humanizer/humanizer.py       意识核心：感知缓冲 + 参与度衰减 + 疲惫值 + 出没调度
llm/mimo.py                  认知引擎：决策 + 话题生成 + 记忆总结（多模态）
```

## 配置

### .env

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `BOT_QQ_ID` | 生命体的 QQ 号 | - |
| `TARGET_GROUP_IDS` | 栖息的群（逗号分隔） | - |
| `MIMO_API_KEY` | 认知引擎密钥 | - |
| `MIMO_BASE_URL` | 认知引擎地址 | `https://token-plan-cn.xiaomimimo.com/v1` |
| `MIMO_MODEL` | 认知模型 | `mimo-v2.5` |
| `ACTIVE_HOUR_START/END` | 活跃时段 | `10-2` |
| `NAPCAT_PATH` | NapCat 启动器路径 | 空 |

深夜出没参数在 `humanizer/humanizer.py` 顶部常量区。

### config/personality.yaml

人格蓝图。修改即改变灵魂，`reload` 即刻生效。

## 技术栈

Python 3.9+ / FastAPI / NapCat (OneBot 11) / aiosqlite / OpenAI 兼容 API / APScheduler

## License

MIT
