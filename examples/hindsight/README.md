# 示例集成：Hindsight（默认不启用）

本目录是 llm-relay「可选上游集成」的一个**示例**。中继本体是通用的：它不认识任何厂商名，
只认 `config.json` 里的 `integrations.upstream` 这一段。这一段**默认不存在**，
所以中继 clone 下来后：

- `GET /admin/upstream` → `{"configured": false}`（HTTP 200，不摸任何上游）；
- 9111 面板**不渲染**「上游」标签页；
- 其余功能（候选链、Key 池、用量、设置…）完全不受影响。

## 这个集成做什么

把本机的 Hindsight（长期记忆服务）接进中继，并在 9111 面板里多看两块东西：

| 能力 | 说明 |
|---|---|
| 上游健康 | 只读探测 `health_url`（Hindsight 的 `/health`），失败只标 error，整体仍 200 |
| 配置回显 | 从 `start_script` 里解析 `{env_prefix}PROVIDER/MODEL/BASE_URL/API_KEY` 四行；`API_KEY` 只显示「已设置/未设置」，**绝不出明文** |
| 一键回滚 | 面板「设置」页执行 `rollback_script`，把 Hindsight 的 `start.sh` 还原到最近备份并重启；必须输入 `ROLLBACK` 才会执行 |

中继侧新增/沿用的接口（都只读或需显式确认）：

| 接口 | 说明 |
|---|---|
| `GET /admin/upstream` | 探测 + 回显；旧路径 `GET /admin/hindsight` 是等价的 deprecated 别名 |
| `POST /admin/upstream/rollback` | body 必须 `{"confirm":"ROLLBACK"}`，否则 400；旧路径 `POST /admin/hindsight/rollback` 等价 |
| `GET /api/upstream`（面板 9111） | 面板用的聚合只读接口（api / cp / stats / relay_usage 四大组）；旧路径 `GET /api/hindsight` 等价 |

## 怎么开

1. 把 `config.snippet.json` 那一段 `integrations` 贴进（或合并到）本机的 `config.json`，
   并把里面的路径改成你机器上的真实路径。中继支持热重载，改完不用重启：

   ```bash
   curl -s -X POST 127.0.0.1:9110/admin/reload   # 或 kill -HUP <relay-pid>
   ```

2. 需要的脚本：

   - `start_script`：Hindsight 自己的启动脚本（本示例默认 `~/.hermes/scripts/hindsight-mac-start.sh`）。
     中继只**读**它（解析四行配置 / 找 `.bak.*` 备份），不写。
   - `rollback_script`：本目录的 [`rollback.sh`](rollback.sh)。它把 `start_script` 还原到最近的
     `.bak.*` 备份，再 `launchctl kickstart` 对应服务；支持 `--dry-run` / `--no-restart`，
     也可用环境变量 `HINDSIGHT_START_SH` 指向别的脚本。**默认不会被自动调用**，只有面板上输入
     `ROLLBACK` 并点按钮才会执行。

3. 面板入口：`http://127.0.0.1:9111/#upstream`；旧的 `#hindsight` 深链接仍可用。

## 关掉

删掉 `config.json` 里的 `integrations` 段并热重载即可：接口回到 `{"configured": false}`，
面板标签页消失，别的功能不受影响。
