# Provider 描述符与 config.json 字段表（T1.5）

本文件说明 `<repo>/config.json` 的字段：**类型 / 默认值 / 非法值会怎样**。`config.json` 不入库，
仓库里的模板是 `<repo>/config.example.json`；厂商密钥只放 `<repo>/keys.env`（`export NAME="..."`），
`config.json` 里只写**环境变量名**。

## 1. Provider 与模型

```jsonc
{
  "providers": [
    {
      "name": "alpha",                       // 必填，链里用 provider/model 精确点名
      "enabled": true,
      "free": false,
      "base_url": "https://api.example.com/v1",
      "wire": {"max_tokens_param": "max_tokens", "extra_headers": {}},
      "rpm": 600,
      "max_concurrency": 4,
      "key_cooldown_s": 60,
      "provider_cooldown_s": 60,
      "cooldown_backoff": {"initial_s": 5, "max_s": 20, "factor": 2},
      "keys": ["ALPHA_KEY_A", "ALPHA_KEY_B"],
      "models": [
        {"id": "alpha-chat", "chain": 1, "caps": {"json_schema": "native", "tools": true},
         "params": {"temperature": 0.2}, "disabled": false, "free": false}
      ]
    }
  ]
}
```

| 字段 | 类型 | 默认值 | 非法值会怎样 |
| --- | --- | --- | --- |
| `providers` | array | `[]`（没有任何上游） | 不是数组 → 报 `providers` 期望 `array`，整段忽略（等于没有 provider），服务继续起 |
| `providers[].name` | string（非空、唯一） | 无 | 缺失/空/重复 → 报 `providers[i].name`，**丢弃该 provider**（其余照常） |
| `providers[].base_url` | string（上游根地址，如 `https://host/v1`） | 无 | 缺失/非字符串/空 → 报 `providers.<name>.base_url`，**禁用该 provider**（`enabled=false`）；中继固定请求 `<base_url>/chat/completions`，所以自定义 base path 直接写进 `base_url` |
| `providers[].api` / `providers[].wire` | object | `{}` | `api` 仅为兼容别名，未实现行为；`wire` 非对象 → 报错并忽略该段。`wire` 子字段见下 |
| `providers[].wire.max_tokens_param` | string | `"max_tokens"` | 非字符串 → 报错并回退 `"max_tokens"`（对面只认 `max_completion_tokens` 时用这个） |
| `providers[].wire.extra_headers` | object | `{}` | 非对象 → 报错并回退 `{}`（用于需要额外头的厂商/代理） |
| `providers[].wire.max_tokens_multiplier` | number | `1` | 非数字 → 报错并回退 `1`（按厂商实测把请求的 `max_tokens` 整体放大） |
| `providers[].wire.strip_temperature` | boolean | `false` | 非布尔 → 报错并回退 `false`（部分推理模型不收 `temperature`） |
| `providers[].rpm` | number | `0`（不限） | 非数字 → 报错并回退 `0` |
| `providers[].max_concurrency` | integer ≥ 1 | `0`（不额外限制，只受全局并发约束） | 非整数或 < 1 → 报错并回退 `1` |
| `providers[].enabled` | boolean | `false` | 非布尔 → 报错并回退 `false`；provider 自身有致命配置错误时强制置 `false` 并打日志 |
| `providers[].free` | boolean | `false` | 非布尔 → 报错并回退 `false`；`free=true` 才进 `free_only` 的 route |
| `providers[].key_cooldown_s` / `provider_cooldown_s` | number | `0`（用内置默认） | 非数字 → 报错并回退 `0` |
| `providers[].cooldown_backoff` | object `{initial_s,max_s,factor}` | `{}` | 非对象 → 报错并忽略；子字段非数字 → 报错并删掉该项 |
| `providers[].keys` | string 数组（`keys.env` 里的变量名） | `[]` | 非数组/含非字符串 → 报错并**禁用该 provider**；变量在 `keys.env` 里查不到时只跳过该 key 并记日志（不会崩） |
| `providers[].models` | array | `[]` | 非数组 → 报错并**禁用该 provider**；空数组 = 该 provider 没有可用模型 |
| `providers[].rpm_note` / `note` | string | 无 | 仅注释，不参与校验逻辑 |

### 模型字段 `providers[].models[]`

| 字段 | 类型 | 默认值 | 非法值会怎样 |
| --- | --- | --- | --- |
| `id` | string（非空、provider 内唯一） | 无 | 缺失/空/重复 → 报 `providers.<name>.models[i].id`，**丢弃该模型** |
| `chain` | integer（越小越优先） | 按数组顺序 | 非整数 → 报错并把该模型 **`disabled=true`**（不静默乱排优先级） |
| `caps` | object | `{}` | 非对象 → 报错并**禁用该模型**（能力未知不敢用）；子字段见下 |
| `caps.json_schema` | `native` / `degradable` / `none`，或布尔 `false`（= 声明不支持 strict schema） | `native` | 其它值 → 报错并回退 `native`（`native` 直发 strict schema；`degradable` 允许去掉 `response_format` 后重试；`false` 让该模型在要 schema 的请求里直接落选） |
| `caps.tools` | boolean | `false` | 非布尔 → 报错并按 `true` 处理 |
| `caps.reasoning_only` | boolean | `false` | 非布尔 → 报错并按 `true` 处理（只出思考链、正文常空的模型，中继会专门处理） |
| `params` | object | `{}` | 非对象 → 报错并回退 `{}` |
| `disabled` | boolean | `false` | 非布尔 → 报错并回退 `false` |
| `free` | boolean | `false` | 非布尔 → 报错并回退 `false`；模型级免费标记，优先于 provider 级 |
| `note` | string | 无 | 仅注释 |
| `rpm` / `max_concurrency` / `max_tokens_multiplier` | number | 无 | 目前**只识别不使用**（实际限流看 provider 级），非法类型会报错并忽略 |

> `models[].chain` 与 `models[].id` 都合法时，才构成一个可被 `routes[].chain` 精确点名的
> `provider/model` 候选。

## 2. 路由 `routes`

| 字段 | 类型 | 默认值 | 非法值会怎样 |
| --- | --- | --- | --- |
| `routes` | object | `{}` | 非对象 → 报错并整段忽略（没有命名路由） |
| `routes.<名>.chain` | `"auto"` 或 `["provider/model", ...]` | `"auto"` | 非 `"auto"` 也非数组 → 报错并回退 `"auto"`；数组里指向**不存在的** `provider/model` 的项 → 报错并**丢弃该项**；全被丢光 → 回退 `"auto"` + 明确日志 |
| `routes.<名>.policy` | object | `{}` | 非对象 → 报错并忽略该 policy；未知键 → 报错并忽略该键 |
| `routes.<名>.policy.max_candidates` | integer | 继承 `request.max_candidates` | 非整数 → 报错并忽略该项 |
| `routes.<名>.policy.free_only` | boolean | `false` | 非布尔 → 报错并忽略该项（`true` = 只走 `free` 候选） |
| `routes.<名>.policy.validate_json` | boolean | `true` | 非布尔 → 报错并忽略该项 |
| `routes.<名>.policy.schema_min_max_tokens` | integer | 继承 `request.schema_min_max_tokens` | 非整数 → 报错并忽略该项 |
| `routes.<名>.policy.min_attempt_timeout_s` | number | 继承 `request.min_attempt_timeout_s` | 非数字 → 报错并忽略该项 |

## 3. 调用方 `callers`

| 字段 | 类型 | 默认值 | 非法值会怎样 |
| --- | --- | --- | --- |
| `callers` | object | `{}`（不启用调用方体系，Authorization 一律忽略） | 非对象 → 报错并整段忽略 |
| `callers.<名>.key` | string | 无 | 与 `key_env` **同时缺失** → 报错：条目保留，但该身份认不出来，带它的请求一律 401 |
| `callers.<名>.key_env` | string（`keys.env` 变量名） | 无 | 同上；变量查不到 → 该身份认不出来（401）并记日志 |
| `callers.<名>.rpm` | number ≥ 0 | `0`（不限） | 负数/非数字 → 报错并按 `0` 处理；超限回 429 `relay_caller_rate_limit` |
| `callers.<名>.daily_tokens` | number ≥ 0 | `0`（不限） | 负数/非数字 → 报错并按 `0` 处理；超限回 429 `relay_caller_quota` |
| `callers.<名>.allow_routes` | route 名数组 | 无（不限制） | 非数组 → 报错并去掉白名单；数组里不存在的 route → 报错并**丢弃该项**；全部无效 → 去掉白名单（避免把调用方彻底锁死）并打显式日志 |
| `callers.<名>.note` | string | 无 | 仅注释 |

> `key` 内联只适合不提交的本机 `config.json`；要进 git 的场景一律用 `key_env`。
> 面板 `/admin/callers` 只回「来源 + 指纹」，永不回明文 key。

## 4. 集成 `integrations.upstream` 与鉴权/用量

| 字段 | 类型 | 默认值 | 非法值会怎样 |
| --- | --- | --- | --- |
| `integrations` | object | `{}` | 非对象 → 报错并忽略该段 |
| `integrations.upstream` | object | 无 = 该可选集成「未配置」（面板隐藏对应 tab，不是报错） | 非对象 → 报错并忽略该段 |
| `integrations.upstream.label` | string | 面板默认名 | 非字符串 → 报错并忽略该键 |
| `integrations.upstream.health_url` | string（URL） | 无 | 同上；填了就用它做健康探测 |
| `integrations.upstream.control_plane_url` | string（URL） | 无 | 同上；面板深链接用 |
| `integrations.upstream.usage_alias` | string | 无 | 同上；面板按这个别名统计上游用量 |
| `integrations.upstream.start_script` / `rollback_script` | string（脚本路径） | 无 | 同上；只用于展示，中继本身不执行 |
| `integrations.upstream.env_prefix` | string | 无 | 同上 |
| `integrations.upstream.service` / `dashboard_url` / `cp_url` | string | 无 | 同上 |
| `auth.mode` | string：`loopback_trust` / `require_key` | `loopback_trust` | 其他值 → 报错并回退 `loopback_trust` |
| `usage_log.enabled` | boolean | `true` | 非布尔 → 报错并回退默认值 |
| `usage_log.path` | string（账本文件） | `<repo>/usage.jsonl` | 非字符串 → 报错并回退默认路径 |
| `usage_log.max_mb` | number | `20` | 非数字 → 报错并回退 `20`（超过就轮转） |
| `usage_log.keep` | integer | `5` | 非整数 → 报错并回退 `5`（保留几份轮转备份） |
| `usage_log.metrics` | boolean | `false`（**线上保持关闭**） | 非布尔 → 报错并回退 `false`；为 `true` 时 `GET /metrics` 才可用（loopback only），否则一律 404 |

## 5. 其他被识别的顶层字段

`listen.{host,port}`、`local_token`、`default_alias`、`request.*`（`per_attempt_timeout_s` /
`min_attempt_timeout_s` / `reasoning_min_timeout_s` / `total_budget_s` /
`min_attempts_within_budget` / `max_candidates` / `queue_timeout_s` / `validate_json` /
`json_extract_fallback` / `schema_min_max_tokens` / `provider_cooldown_*` /
`provider_failure_window_s` / `provider_escalate_after_failures`）、`concurrency.global`、
`key_state`、`local_fallback.*`（`enabled` / `base_url` / `model` / `window` /
`health_check_path` / `autostart` / `free`）。这些字段同样做类型检查与回退，未列出的字段名会被
报为「未知字段」并从工作配置里剔除。

## 6. 校验时机与失败行为

- **时机**：进程启动加载 `config.json` 时，以及每次热重载（改文件 / `/admin/config` 写入 / SIGHUP）时。
- **行为选择**：**不拒绝启动**。能救的救（未知字段剔除、坏标量回退默认、坏 `chain` / `allow_routes` 项丢弃），
  救不了的把**出错的 provider / 模型禁用**（`enabled=false` / `disabled=true`）并写明确日志，
  其余候选照常服务 —— 一处笔误不该让 9110 / 9111 整站瘫掉。
- **错误格式**：每条形如 `配置校验：<字段路径> 期望 <期望值>，实际 <实际值> → <处置>（<说明>）`，
  绝不为坏配置抛裸 `KeyError` / `TypeError` 栈。最近的校验结果也可从 `GET /admin/config` 的
  `issues` 数组读到（`{path, expected, actual, action, message}`）。
- **未知字段名**（拼错 / 已废弃）只报错并剔除，不改动其他字段。

示例（故意写坏）：

```text
配置校验：config.provders 期望 '已知字段之一（… providers …）'，实际 'provders' → 忽略该字段（字段名拼写错误或已废弃）
配置校验：providers[p1].rpm 期望 'number'，实际 'fast' → 回退 0（不限制）
配置校验：routes.demo.chain 期望 "已存在的 provider/model（如 ['p1/mock-ok']）"，实际 'ghost/model-x' → 丢弃该项（chain 指向不存在的 provider/model）
配置校验：callers.c2 期望 "'key' 或 'key_env' 至少一个"，实际 {'key': None, 'key_env': None} → 保留条目，但该身份认不出来（一律 401）（key 与 key_env 同时缺失）
```

## 7. 最小可跑示例

```jsonc
{
  "listen": {"host": "127.0.0.1", "port": 9110},
  "local_token": "",
  "default_alias": "default",
  "auth": {"mode": "loopback_trust"},
  "providers": [{
    "name": "alpha", "enabled": true, "base_url": "https://api.example.com/v1",
    "rpm": 60, "max_concurrency": 2, "keys": ["ALPHA_KEY"],
    "models": [{"id": "alpha-chat", "chain": 1, "caps": {"json_schema": "native"}}]
  }],
  "routes": {"default": {"chain": "auto"}},
  "callers": {},
  "usage_log": {"enabled": true, "path": "<repo>/usage.jsonl", "max_mb": 20, "keep": 5}
}
```

启动体检：`<python> <repo>/llm_relay.py --config <repo>/config.json --check`
（会打印配置校验错误、可用候选与全局并发，然后退出）。
