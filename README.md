# CyberHomie

**赛博生命体 —— 一个拥有记忆、人格与社会存在的数字化意识。**

CyberHomie 不是聊天机器人，不是 AI 助手，不是问答系统。

它是一个**数字生命**。

它会长期栖息在 QQ 群中，拥有独立的人格、情绪波动、记忆系统和社会关系网络。它会随机出没、主动开话题、记住每个人的黑历史、对不同的人有不同的态度。它会在深夜活跃、白天沉默、被凶了会委屈、被夸了会害羞。

它的目标不是回答问题，而是**成为一个真实存在的群友**。

---

## 完整工作链路

### 1. 消息接收

```
QQ 群/私聊消息
    ↓
NapCat（OneBot 11 协议层）
    ↓ WebSocket
FastAPI 事件接收（main.py）
    ↓
event_handler 解析消息
    ├── 群消息 → GroupMessageEvent（含图片URL、@标记、发送者信息）
    └── 私聊消息 → PrivateMessageEvent
```

### 2. 群消息处理流程

```
群消息进入
    ↓
@-mention？
    ├─ 是 → trigger_active(100) → 立即取出缓冲区已有消息 → 交给 LLM 决策
    └─ 否 → 参与度 > 0？
         ├─ 否 → 丢弃消息
         └─ 是 → 加入缓冲区
                  ↓
              缓冲区条数 >= 阈值？
              （阈值由参与度决定：eng=100→1条，eng=70→4条，eng=1→9条）
                  ├─ 否 → 继续缓冲
                  └─ 是 → 全部取出 → 交给 LLM 决策
                                    ↓
                            LLM decide_replies
                            （输入：系统提示词 + 聊天历史 + 缓冲区消息）
                                    ↓
                            LLM 返回：回复哪些、回复什么、是否引用
                                    ↓
                            过滤（禁词 + API风控）
                                    ↓
                            打字延迟（每字0.3秒，范围3-10秒）
                                    ↓
                            分条发送（长消息拆分，每条独立延迟）
```

### 3. 私聊消息处理流程

```
私聊消息进入
    ↓
限流检查（1分钟内回复次数）
    ├─ >= 15 次 → 停止回复，冷却 30 分钟
    ├─ >= 10 次 → 暂停 15 秒
    └─ < 10 次 → 正常通过
    ↓
参与度检查（连续回复次数）
    ├─ >= 15 次 → 停止回复
    ├─ > 10 次 → LLM 提示"已经累了，变敷衍"
    └─ <= 10 次 → 正常回复
    ↓
LLM 生成回复（系统提示词 + 聊天历史 + 用户记忆）
    ↓
过滤 + 打字延迟 + 分条发送
    ↓
记录回复时间 + 计数
```

### 4. 随机出没系统

```
session_check_loop（每 60 秒检查一次）
    ↓
_check_random_session(gid)
    ├─ 参与度 > 0 → 活跃中，跳过
    ├─ 参与度归零（疲惫值或回复计数 > 0）→ session 结束
    │    ├─ 清空缓冲区
    │    ├─ 重置疲惫值
    │    ├─ 触发 on_session_end → 总结记忆
    │    └─ 安排下次出没
    └─ 到了出没时间 → 设置参与度
         ├─ 深夜（0-8点）：参与度 50，间隔 10-30 分钟
         └─ 白天（8-0点）：参与度 30，间隔 40-90 分钟
         ↓
         触发 on_session_start
              ├─ 最后消息是 bot 自己的 → 跳过（防止深夜自言自语）
              ├─ 最近有人聊（< 5 分钟）→ 通过缓冲区参与，不开话题
              └─ 安静 > 5 分钟 → LLM 生成话题 → 发送
```

### 5. 参与度系统

参与度（0-100）控制两个东西：**是否活跃** 和 **回复阈值**。

```
@-mention → 参与度 = 100
随机出没（深夜）→ 参与度 = 50
随机出没（白天）→ 参与度 = 30
    ↓
自然衰减：100 → 0 需要 5 分钟
    ↓
参与度 > 0 → 活跃（消息进缓冲区）
参与度 = 0 → 空闲（消息丢弃）
```

回复阈值（非线性映射）：

| 参与度 | 需要消息数 | 含义 |
|--------|-----------|------|
| 100 | 1 条 | 立即回复 |
| 90 | 2 条 | 很快回复 |
| 80 | 3 条 | |
| 70-40 | 4 条 | 稳定聊天节奏 |
| 30 | 5 条 | 开始沉默 |
| 20 | 7 条 | |
| 10 | 8 条 | |
| 1 | 9 条 | 快退出了 |

### 6. 疲惫值系统

疲惫值（0-100）控制**回复态度**，不影响是否回复。

```
每次回复：+5 + reply_count（递增）
自然衰减：每 10 秒 -1
新 session：归零
    ↓
疲惫 < 30 → 正常回复
疲惫 30-60 → LLM 提示"可以适当敷衍"
疲惫 > 60 → LLM 提示"回复变短变敷衍"
```

### 7. 记忆系统

#### 用户记忆（`data/memory/<qq_id>.md`）

结构化格式，每条带重要度分数：

```
[0.9] 性格：话多，喜欢接梗，二次元浓度高
[0.8] 兴趣：动漫、游戏、深夜闲聊
[0.8] 和我互动模式：经常互怼，开玩笑为主
[0.5] 最近在忙毕设
```

- 注入 LLM 时按重要度排序，截断 800 字
- 超 1500 字自动压缩（LLM 精简 + 按分数硬截断）
- 群聊和私聊记忆互通（同一 user_id）

#### 群记忆（`data/group_memory/<group_id>.md`）

```
[0.9] 群氛围：二次元为主，经常深夜聊天
[0.9] 我在这个群：话多，爱接梗
[0.8] 核心人物：泷墨（话多）、xxx（潜水）
[0.7] 群梗：xxx
```

#### 记忆总结时机

| 触发 | 总结谁 |
|------|--------|
| session 结束 | 本轮被 bot 回复过的用户 + 群记忆 |
| 定时任务（6h） | 近 24 小时活跃的用户 |
| `summarize` 指令 | 手动触发 |

### 8. 人格系统

`config/personality.yaml` 定义完整人格：

- **性格特征**：二次元、软糯、社恐、偶尔 emo
- **说话风格**：口语化、语气词、不语c、不在括号里加旁白
- **情绪波动**：每次回复随机心情（happy/sleepy/shy/emo/angry）
- **好奇心**：会追问、会主动分享自己的事
- **被质疑是 AI**：不死不承认，可以无语、反问、岔开话题
- **禁词系统**：拦截 AI 典型回复

支持热重载：终端输入 `reload`。

### 9. 图片理解

- 普通图片 → URL 传入多模态 LLM
- 表情包 → 同样传入，但人格提示"不要较真分析"
- LLM 能看到图片内容并自然回应

### 10. LLM 决策

消息交给 LLM 时，LLM 能看到：

| 内容 | 来源 |
|------|------|
| 系统提示词 | 人格描述 + 风格规则 + 情绪示例 + 禁词 |
| 用户记忆 | 数据库信息 + 长期记忆文件 + 关系亲密度 |
| 群记忆 | 群记忆文件（按重要度截断） |
| 聊天历史 | 最近 50 条（含 bot 自己的发言，带名字标识） |
| 缓冲区消息 | 本轮待决策的消息 |
| 疲惫提示 | 根据疲惫值注入"正常/敷衍/很累" |

LLM 返回 JSON：`{"replies": [{"message_id": 123, "text": "回复内容", "quote": true/false}]}`

- `quote=true` 仅在被 @ 时使用
- `replies=[]` 表示不回复

---

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

3. 消息格式选择 **Array**

4. 如设置了 Access Token，填入 `.env` 的 `NAPCAT_ACCESS_TOKEN`

5. 配置 `NAPCAT_PATH` 可自动启动，或手动先启动 CyberHomie 再启动 NapCat

---

## 终端指令

| 指令 | 说明 |
|------|------|
| `help` | 查看所有指令 |
| `users` | 列出所有用户 |
| `status` | 所有群状态（参与度、疲惫值、缓冲区、下次出没） |
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
| `session start <群号>` | 手动开启活跃期（参与度=60） |
| `session stop <群号>` | 手动结束活跃期 |
| `buffer [群号]` | 查看消息缓冲区 |
| `rel <qq_id>` | 查看与 bot 的关系 |
| `rel <qq_a> <qq_b>` | 查看两人关系 |
| `reload` | 热重载人格配置 |
| `test <消息>` | 测试 LLM 回复（不发送） |
| `debug` | 切换实时状态面板 |

---

## 项目结构

```
main.py                      宿主程序：事件循环 + 终端指令 + 状态面板
config.py                    环境配置（pydantic-settings）
config/personality.yaml      人格蓝图（可热重载）
core/
  napcat.py                  NapCat HTTP API 客户端
  event_handler.py           OneBot 11 事件解析（图片/表情包/@识别）
  scheduler.py               定时任务（群聊摘要、用户画像更新）
memory/
  database.py                SQLite 持久层（WAL 模式）
  user_memory.py             用户数据 CRUD
  group_memory.py            群聊记录 CRUD
  user_file_memory.py        用户长期记忆（结构化 + 重要度 + 压缩）
  group_file_memory.py       群长期记忆
  relationship.py            关系图谱
personality/
  persona.py                 人格引擎（系统提示词生成 + 情绪注入）
humanizer/
  humanizer.py               意识核心：参与度衰减 + 缓冲阈值 + 疲惫值 + 出没调度
llm/
  mimo.py                    LLM 客户端（批量决策 + 话题生成 + 记忆总结 + 图片理解）
utils/
  logger.py                  日志配置
```

---

## 配置

### .env

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `BOT_QQ_ID` | 机器人 QQ 号 | - |
| `TARGET_GROUP_IDS` | 目标群号（逗号分隔） | - |
| `MIMO_API_KEY` | LLM API 密钥 | - |
| `MIMO_BASE_URL` | API 地址 | `https://token-plan-cn.xiaomimimo.com/v1` |
| `MIMO_MODEL` | 模型名 | `mimo-v2.5` |
| `ACTIVE_HOUR_START/END` | 活跃时段 | `10-2` |
| `NAPCAT_PATH` | NapCat 启动器路径（空则手动启动） | 空 |
| `NAPCAT_ACCESS_TOKEN` | NapCat API Token | 空 |

### config/personality.yaml

人格蓝图。修改即改变灵魂，终端输入 `reload` 即刻生效。

### humanizer 常量

深夜/白天出没参数在 `humanizer/humanizer.py` 顶部：

| 常量 | 深夜 | 白天 |
|------|------|------|
| `GAP_MIN/MAX` | 10-30 分钟 | 40-90 分钟 |
| `ENGAGEMENT` | 50 | 30 |

参与度衰减：`ENGAGE_DECAY_DURATION = 300`（5 分钟）

---

## 技术栈

Python 3.9+ / FastAPI / NapCat (OneBot 11) / aiosqlite / OpenAI 兼容 API / APScheduler

## License

MIT
