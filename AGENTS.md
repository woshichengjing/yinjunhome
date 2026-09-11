# AGENTS.md — 香槟国际智能家居状态机控制器

> 给所有 AI 编程助手（Codex / Claude Code / 等）的项目约束说明。
> **你的职责边界：只改代码、commit、push。不部署、不重启服务、不碰 HA。**

---

## 1. 项目是什么

一套跑在 Ubuntu 宿主机 `openclaw-vm` 上的家居自动化系统，核心是**房间状态机 + 温控引擎**，通过 Home Assistant REST API（`http://10.90.1.19:8123`）读取传感器、控制空调/除湿机/新风。

两个 systemd 服务常驻：
- `room-state-machine.service`（每 5s）：批量读 11 个房间的人在/无人传感器 → 本地判定房间活动状态（empty / entering / occupied / sleeping）→ 写 HA entity + `room_state.json`
- `ha-controller.service`（每 60s）：读房间状态 → 环境质量评估 → 温控意图 → 设备保护 → 下发控制指令 → 写 `state_*.json`

**⚠️ 你（AI 助手）在这里的角色是改代码，不是改数据、不是调设备、不是部署。** 改完 commit + push 即可，部署由 Hermes 负责。

---

## 2. 代码结构（文件职责）

```
/opt/ha-controller/
├── room_state_machine.py          # 房间状态机（5s systemd 服务）——判断人的活动
├── controller.py                  # 双轨控制器（60s systemd 服务）——编排
├── ha-controller.service          # systemd 单元文件（勿改，除非明确要求）
├── energy_tracker.py              # 能耗统计（每 5min，估算功耗）
├── snapshot_sensors.py            # 传感器快照（供仪表盘用）
└── state_machines/
    ├── climate_intent.py          # 意图层 P0-P6（当前该做什么：制冷/除湿/新风/待机…）
    ├── climate_engine.py          # 设定点计算（体感温度 AT 驱动，已抽 5 个函数，可接收 intent）
    ├── climate_logger.py          # 审计 JSONL（记录每次动作 + 理由）
    ├── env_quality.py             # 环境质量评估 + 体感温度 AT 计算
    ├── device_protection.py       # 设备保护（压缩机保护/最小启停）+ 实际执行 HA 指令
    └── external_env.py            # 外部环境（户外传感器、新风触发条件）
```

**数据流**：
```
room_state_machine (5s)  →  room_state.json
        ↓
ha-controller (60s)      →  state_*.json（env_quality / device_protection / climate_intent …）
        ↓
同步脚本                  →  前端看板 /app/static/data/
```

---

## 3. 铁律：绝对不要做（改了必踩雷）

1. **体感温度 ≠ AC 设定点。** `at_comfort` 是体感目标（人感觉到的温度），**不是**空调设定点。引擎用 `round(comfort + ac_cur - sense_temp)` 把体感偏差换算成设定点——**AC 设定点低于体感目标是正常的，绝不要把 `at_comfort` 直接当设定点用。**（2026-08-09 因此被严厉纠正过。）

2. **所有时间判断必须用 Asia/Shanghai。** 机器系统时区可能是 UTC。`datetime.now().hour` 必须是北京时间。相关脚本顶部有 `os.environ["TZ"]="Asia/Shanghai"; time.tzset()`，别删，别改成 UTC。

3. **Python 缓存陷阱。** 改 `.py` 后必须清 `__pycache__` + 重启 daemon，否则旧代码继续跑。**这个由 Hermes 部署时处理**，但你改代码时要意识到：你 push 的代码要等 Hermes pull + 清缓存 + 重启后才生效。

4. **不要动传感器掉线兜底逻辑。** `env_quality.py` 传感器 unavailable 时必须**保留卡片 + 标记 `sensor_disconnected` + 用 AC 回风温度兜底**，绝不能 `return None`。

5. **`EMPTY_DELAY_MIN` 必须 > 0**（当前 10 分钟），否则离开防抖失效、房间状态来回抖。`OCC_DELAY_MIN` 当前 5 分钟。

6. **不要用传感器自带的 duration 做状态持续时间。** 状态机的房间持续时间是本地 tick 文件独立累加（`/tmp/hermes_states/dur_tick_{room}`），传感器 `has_someone_duration` / `no_one_duration` 有自己的防抖，与状态机不同步。`no_one` 传感器已被整体废弃（2026-08-10），无人时长改用本地 `occ_off` 时间戳。

7. **不要删 `_safe_eval()` 包裹。** 每个 `evaluate_*()` 房间判定函数必须被 `_safe_eval` 隔离，单房间崩溃不能拖垮全部房间。

---

## 4. 关键设计决策（这些是刻意的，别"优化"掉）

- **手动关机保护 = 方案 A（2026-08-22 定稿）**：删掉了三套 `manual_off`/`manual_on` 状态机，只保留一条——「AC `last_changed` 距今 < 30min 不自动开机」。引擎决策前必须实时查 HA 校准，不能依赖快照（快照滞后 60s 会覆盖手动关）。
- **自动温控只管理制冷/除湿，不自动制热**。`heat` / `fan_only` / `auto` 均视为手动模式。自动启停：① 已确认跑温 → 请求关机；② 连续节能待机，或房间达标且连续卸载 30 分钟 → 请求关机；③ AC 全关、确认有人且持续不适 3 分钟，不在新风观察期、关机已满 30 分钟 → 开一台。跑温、偏冷/过冷、舒适拥有全局否决权，睡眠/待客/共享空间不能绕过。偏湿先交新风/独立除湿机。所有开关机仍经过设备保护。详见 `docs/climate-strategy.md`。
- **投影 > 电脑 > 普通有人**（书房活动判定优先级）。
- **猫检测**：客厅/餐厅夜间（22:00–07:00）+ 灯关 + 电视关 + 噪音 < 45dB → 强制判 empty，防宠物误触空调。
- **除湿舒适守卫**：房间体感已"舒适"时，即使目标是 dry 也强制切回 cool，防止除湿在舒适房里持续制冷过冷。
- **睡眠温度用时钟判断，不是时长**：先 `occupied` 才能判睡觉；睡眠温度窗口统一 **3:00–10:00**（三个文件同步：engine / intent / env_quality，改一处必须三处同改）。
- **新风触发与优先级**：室外 20–26°C 且室外绝对湿度 < 室内最低，或 CO₂ 触发通风（阈值以代码为准）。对每个房间分别判断室外至少低 2°C、分别观察 30 分钟；观察期内空调保持关闭。新风未确认启动最多等待 2 分钟，之后仍须满足空调的持续需求确认与启停保护。
- **运行调节必须用 `set`，开机/关机/调节互斥**。关机分支直接结束，不得被后续模式调整覆盖。每轮撤销旧自动队列并按新输入重判，自动指令 180 秒失效，不覆盖用户排队指令。待客仅保护本次夜间已开启的设备，不授权空房自动开机；历史预冷时间表不得取消待机关机。
- **前端关闭必须由控制器确认**。`control_switches.py` 统一读取 `STATE_DIR/ac_disabled.json` 和 `STATE_DIR/device_soft_off.json`：文件缺失、损坏或值不是 JSON 布尔值时禁止自动开机/调节；有效空字典兼容历史未禁用状态。意图、引擎、入队和执行阶段都检查开关，`force` 不得绕过自动控制关闭。前端只有收到设备快照中的新 `control_switches` 回读才显示操作成功；HTTP 错误、静态副本和 API 成功响应都不能单独证明控制器已采用。接口与控制器须共用同一文件，详见 `docs/control-switch-contract.md`。
- **房间状态机只判断"人的活动"**（empty/entering/occupied/sleeping/napping），不判断房间属性、不输出舒适度（那是 env_quality 的活）。

---

## 5. 传感器注意事项

- **户外传感器只能用** `sensor.lumi_cn_lumi_158d000116b3a8_v1_*`（真户外）。`sensor.miaomiaoc_..._146ht5qh85s00_t2_*` 在玄关（半室内偏凉），**不能**当户外温度用。
- **睡眠检测亮度阈值** < 100 lx，窗口 0–12 点（含午睡）。三卧传感器：br=`linp_cn_1083284825_hb01_illumination_p_2_5`、sb=`izq_cn_1086679488_24_illumination_p_2_5`、nb=`izq_cn_1086711016_24_illumination_p_2_5`。
- **人在传感器**：主卧 `linp_cn_1083284825_hb01`（dur: p_2_3/p_2_4）；主卫 `linp_cn_blt_3_1pjv3016t0g01_es4b`（ES4B 电池型）。主卫的 `no_one_duration` 返回文字如 "60分钟持续无人"，用 `re.search(r'(\d+)', raw)` 提取数字。
- **`no_one` 值 ≥ 1440 是秒值**（小米传感器标注"不支持"时返回固定秒值），需 `// 60` 转分钟（此守卫已内置，别删）。

---

## 6. 房间标签 ↔ 显示名（改房间必须同步多文件）

| 标签 | 显示名 | 标签 | 显示名 |
|------|--------|------|--------|
| br | 主卧 | nb | 北次卧 |
| st | 书房 | kt | 厨房 |
| lr | 客厅 | cr | 走廊 |
| dr | 餐厅 | en | 玄关 |
| sb | 南次卧 | gb | 客卫 |
| bath | 主卫 | | |

**改一个房间的标签/显示名，必须同步这些文件**（漏一个就会"看板对但后端错"）：
`room_state_machine.py`、`env_quality.py`、`climate_engine.py`（含 `suite_bath` 等关联配置）、前端看板 `state-machine-dashboard.html`、`home3d_scene.json`（3D 用中文标签）。改完用 `grep` 全量搜旧名，零残留才算干净。

---

## 7. 温度参数快照（2026-09 从代码提取，改动前仍以代码为准）

> 下面是当前代码里的真实值。**若与代码不一致，以代码为准。** 改任何参数前先问 Hermes。

### 引擎设定点（climate_engine.py）

| 参数 | 值 | 说明 |
|------|-----|------|
| `at_comfort`（正常有人） | 27.5°C | **体感目标，非设定点** |
| `at_comfort`（睡觉） | 3:00–10:00 → 28.5，其余 27.5 | **时钟判断，非时长** |
| `energy_temp`（节能/空房） | 30°C | |
| `unloaded_off_min` | 30 分钟 | 房间达标且连续卸载后关机；带载/不适/数据中断重计 |
| `auto_start_confirm_min` | 3 分钟 | 确认有人且持续需求；entering 不授权开机 |
| 舒适带 | `(at_comfort - 0.5, at_comfort]` | 下边界属于偏冷主动回升 |
| 偏冷主动回升 | `sense_temp <= at_comfort - 0.5` | 制冷时取 `max(原设定点, ceil(回风), round(at_comfort-0.5))`，不得合并进舒适保持分支 |
| `comfort` 下限 | `max(20, at_comfort)` | |
| `setpoint_min` / `setpoint_max` | 16 / 32 | 设定点硬边界，step=1 |
| 设定点换算 | `ideal = round(comfort + ac_cur - sense_temp)` | 体感偏差 → 设定点 |

### 意图层（climate_intent.py，P0–P6 优先级链）

P0 设备安全 → P1 用户手动控制 → P2 房间保护（guest 22:00–07:00）→ P3 房间状态（睡眠）→ P4 温控策略 → P5/P6 执行。
- 睡眠 `comfort_target = 28.5 if 3 <= hour < 10 else 27.5`（与引擎同步）
- 节能 `comfort_target = 30`，正常有人 `comfort_target = 27.5`

### 体感温度 AT + 舒适阈值（env_quality.py）

- **AT 线性湿度补偿法**：60% RH 基准零偏移；`base_comp = (rh - 60) * 0.05`；`heat_multiplier = 1.0 + max(0, temp - 24) * 0.1`；22–24°C 线性淡入（<22°C 不补偿）。
- **裸温偏热/偏冷线**：`TEMP_COLD/COOL/HOT = 22/24/29°C`；湿度 `HUM_DRY/COOL/HIGH/WET = 30/40/65/80%`。
- **四季舒适阈值**（杭州，按月份切换，判定逻辑以代码为准）：

| 季节 | at_min | at_max | thot | temp_max_normal |
|------|--------|--------|------|-----------------|
| 春/秋 | 20 | 26 | 28 | 26 |
| 夏 | 24 | 27.5 | 30 | 28 |
| 冬 | 18 | 24 | 26 | 25 |

- **活动 profile**：`sleeping` → at_max=28.5, temp_max=26.5, co2_warn=1000；`normal` → temp_max=26.5, co2_warn=1200；`movie` → temp_max=28。
- **`at_max` 睡觉后半夜（3:00–10:00）严格对齐引擎 28.5**，其余 27.5 —— 防"看板判适宜、引擎仍在压低设定点"的两层 1°C 错位。
- **跑温防抖** `RUNAWAY_DEBOUNCE_SEC = 600`（门窗连续开 ≥10min 才算跑温）。

### 改温度参数的联动清单

任何温度改动都要同时检查五处联动：睡眠窗口（3:00–10:00）、节能温度（30）、除湿舒适守卫、跑温（门窗开不制冷）、env_quality 的 `at_max` 对齐。**改一处，五处全查。**

---

## 8. 提交规范

- 一次 commit 只做一件事，message 写清**改了什么 + 为什么**。
- 不要提交 `*.bak.*`、`data/`、`__pycache__/`、`.env`（已在 `.gitignore`）。
- 改完自查：`python3 -m py_compile <改动的文件>` 通过；`grep` 确认没有漏改的关联文件。
- **只 push，不部署。** 部署（pull + review + 清缓存 + 重启 systemd）由 Hermes 负责，你在 commit message 里写清楚预期影响即可。

---

## 9. 有疑问先停下来问

这套系统有大量历史演进和边界条件（体感换算、跨日判断、防抖、传感器掉线、手动关保护）。**拿不准一个改动是否安全时，不要猜，先停下来说明疑问。** 宁可少改，不可盲改。

## 10. 和风天气 API 接入（qweather_forecast.py）

- **响应强制 gzip**：Python `urllib` 不自动解压。`response.read()` 后必须检测 `data[:2] == b"\x1f\x8b"` 再 `gzip.decompress(data)`，否则 `decode("utf-8")` 报 `0x8b` 错误（2026-09-06 Codex 踩过，已修复）。
- **新版 v1 API**（`/weather/v1/hourly`、`/weather/v1/daily`）用账户专属 host + `X-QW-Api-Key` 请求头，不是 `?key=` URL 参数。
- **凭据在宿主机 `/root/.hermes/.env`**（`QWEATHER_API_HOST` + `QWEATHER_API_KEY`），脚本 `_load_env()` 自动读取，别硬编码进代码。
- **输出路径**：脚本 OUT 默认 `/app/static/data/` 在宿主机跑是错的（Docker overlay，容器读不到）。必须用 `QWEATHER_FORECAST_OUT=/root/.hermes/static/data/weather_forecast.json` 覆盖，再由 dashboard-sync 同步到容器。

