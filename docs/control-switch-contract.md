# 前端自动控制开关的读写约定

## 已确认的问题与修复

房间开关原来把任何可解析的 JSON 都当作成功，包括 HTTP 501/500；设备开关也未检查 HTTP 状态。仓库配套的本地预览明确拒绝 `/api/*` POST 并返回 501，旧页面仍能显示关闭。页面还依赖静态文件副本，房间开关只首次加载，设备软关文件在控制器中漏了同步。

设备执行前原来只检查 HA 模式和最近关机时间，没有再次检查自动控制开关。现在意图、引擎、入队、读取队列、执行前均检查统一的开关状态；HA 回读期间发生的关闭也会在设备写操作前再拦截。已排队的自动 `on` / `set` 被撤销，自动 `force` 不绕过开关。

前端由 `control-switches.js` 统一处理两类按钮，检查 HTTP 状态和显式业务错误，并轮询控制器新一轮回读后才显示确认。等待期间禁用重复点击；数据未知/过期显示待确认样式，不当作已关闭或已开启。单次请求超时 8 秒，正常轮询约 75 秒仍未确认则提示同步异常。

## 后端接口必须满足的约定（当前仓库没有接口实现）

| 请求 | 必须持久写入的逻辑状态 |
| --- | --- |
| `POST /api/toggle/br?state=off` | 控制器 `STATE_DIR/ac_disabled.json` 中 `"br": true` |
| `POST /api/toggle/br?state=on` | 同文件 `"br": false` |
| `POST /api/toggle/device/br_ac?state=true` | 控制器 `STATE_DIR/device_soft_off.json` 中 `"br_ac": true` |
| `POST /api/toggle/device/br_ac?state=false` | 同文件 `"br_ac": false` |

其他房间/设备使用对应标识。值必须是 JSON 布尔值，不能是字符串 `"false"`。有效稀疏字典中缺少某个键表示未禁用；文件本身缺失、不可读、JSON 损坏或字段类型错误则表示未知，禁止自动开启/调节。关闭指令仍允许，用户显式手动指令沿用既有边界。

控制器的 `STATE_DIR` 当前是宿主机 `/tmp/hermes_states`。如果接口运行在 Docker 里，容器内同名路径未必是同一个目录，必须绑定到宿主机的实际状态目录，或通过宿主机服务写入；只写 `/app/static/data`、容器私有 `/tmp` 或浏览器状态不会关闭宿主机自动控制。

接口应原子替换 JSON、保留其他房间键、串行化并发更新，写入后回读确认再返回成功。写失败应返回非 2xx，不应只修改前端静态副本。`/tmp` 重建后应恢复已保存的用户开关设置；不可默认把曾经关闭的设备全部开启。本次代码不会自动生成空字典来重新授权控制。

## 前端确认的依据

`state_device_protection.json` 中每台设备新增：

```json
{
  "control_switches": {
    "room_disabled": true,
    "device_soft_off": false,
    "automation_allowed": false,
    "blocked_reason": "room_disabled",
    "observed_at": 1789128000
  }
}
```

`observed_at` 是控制器读取自身状态文件的 Unix 时间。前端要求请求后出现比请求前更新的观察、对应字段为期望布尔值，并且观察距当前时间不超过 180 秒。这里确认的是自动控制许可，不是物理空调瞬间停机；关机仍需经过设备保护。

## 验证与部署要求

Python 回放覆盖关闭后禁止入队、撤销旧队列、执行前竞态、文件缺失/损坏、重新开启后的需求确认；Node 测试覆盖 501 假关闭、200 业务失败、接口成功但控制器未采用、重复点击、过期状态与脚本语法。

```text
python -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_control_switches.cjs
```

Hermes 部署时需同时更新新增的 `state_machines/control_switches.py`、`frontend/control-switches.js`、控制器和页面，再清缓存、重启控制器。先检查并保留两份开关文件中的真实用户设置；接口实现和 Docker 挂载尚未包含在仓库，生产写入路径仍需核对。
