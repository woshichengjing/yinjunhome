# 发布状态机看板

由 Hermes 在 Ubuntu 宿主机部署。拉取提交后，从 `/opt/ha-controller` 运行 `python3 publish_dashboard.py`。发布脚本把墙面主题、天气预报样式和脚本、开关脚本打包进 HTML，并原子更新 `~/.hermes/dashboards` 下的两个现有入口。`dashboard-html-sync.timer` 会把这两个文件恢复到容器的 `/app/static/`。持久化入口和定时器白名单沿用现有设置。若本地 Git 合并钩子还调用旧的 `sync_frontend.sh` 直接复制容器副本，将钩子改为调用 `python3 /opt/ha-controller/publish_dashboard.py`，以后从持久目录发布。

启用新的灯光和能耗数据需部署 `room-state-machine.service` 和 `ha-controller.service` 对应的 Python 更新，按项目约定清缓存并重启这两个服务。灯光复用房间状态机已有的 HA 批量读数。控制器将灯光、能耗汇总和历史发布到 `~/.hermes/static/data/`，由 WebUI 已运行的 `sync_static_data.py` 同步。发布时检查该同步器在运行，并确认两个入口及三个 JSON 文件均返回 200。

核对两个入口都无旧的前端 HA 凭据。旧凭据曾出现在入口、Git 历史和旧副本中，须在 Home Assistant 确认对应令牌并撤销。轮换前先确认使用该令牌的服务，避免中断现有控制。外网验证应走现有登录入口，并更新仍在提供旧 HTML 的缓存。
