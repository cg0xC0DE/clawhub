# backend/tools/

调试与辅助脚本，不属于主服务运行时依赖。

| 脚本 | 用途 | 需要服务运行 |
|------|------|:---:|
| `simulate_10_ticks.py` | 离线跑 10 轮模拟，输出指定角色历程 | ✗ |
| `game_recap.py` | 读 `game_state.json` 打印摘要 | ✗ |
| `check_gw.py` | 查看网关状态 | ✓ |
| `check_pool.py` / `check_pool2.py` | 查看太监传话池 | ✓ |
| `check_tg.py` | 测试 Telegram Bot 连通性 | ✗ |
| `check_agent_session.py` | 查看某 agent 最新 session | ✗ |
| `inspect_sessions.py` | 遍历所有 gateway session 结构 | ✗ |
| `trigger_heartbeat.py` | 手动触发某 agent 心跳 | ✓ |

> 带 ✓ 的脚本需要后端 `app.py` 正在运行。
