# 用 Docker 跑 llm-relay

这是**可选渠道**：项目本身是单文件、零依赖，`python3 llm_relay.py` 就能跑。
这里给的是给「不想在机器上装 Python / 习惯用容器管服务」的人用的版本 —— 典型场景是 NAS、
小服务器或者旁路机器上常驻。

## 起

```bash
cd examples/docker
mkdir -p data && cp ../../config.example.json data/config.json
$EDITOR data/config.json      # 填上游地址与模型；keys 里写「环境变量名」
$EDITOR data/keys.env         # 形如 EXAMPLE_OPENAI_KEY=sk-...，建议 chmod 600
docker compose up -d
curl -s http://127.0.0.1:9110/health
```

面板在 <http://127.0.0.1:9111>（容器内的密钥文件落在 `data/access-key.txt`，首次启动自动生成）。

## 数据都在 `data/` 一个目录里

| 文件 | 说明 |
|---|---|
| `config.json` | 中继配置（`--config` 指到它） |
| `keys.env` | 各 provider 的密钥（`--keys` 指到它） |
| `usage.jsonl` | 用量记账；`config.json` 里 `usage_log.path` 留空即为「与 config.json 同目录」 |
| `access-key.txt` | 面板访问密钥；`ui.json`、壁纸也落在同目录 |

备份这一件事：把 `data/` 复制走就行。

## 四个容器特有的坑（都不大，但踩了会以为是程序坏了）

1. **`--host 0.0.0.0` 是必须的。** 服务默认只监听回环，而容器里的回环不是宿主机的回环 ——
   不显式放开，端口映射就是白做。compose 里已经给两个服务都写了。
2. **端口默认只发布到宿主机回环**（`127.0.0.1:9111:9111`）。想从手机看，改成 `0.0.0.0:9111:9111`，
   那时**访问密钥就是唯一的门**（非回环来源仍需密钥，且默认只读）。
3. **Linux / NAS 上的权限。** 镜像里以 uid 10001 运行；宿主机的 `data/` 如果不是它的，
   容器会写不进去 —— `sudo chown -R 10001:10001 data`，或在 compose 里加 `user: "1000:1000"` 对齐宿主机用户。
   Docker Desktop（macOS/Windows）通常不需要管这个。
4. **`config.json` 里的 `listen.host` 保持 `127.0.0.1` 也无妨** —— 命令行的 `--host 0.0.0.0` 优先级更高。
   如果你更愿意改配置，把它改成 `0.0.0.0` 并去掉命令行参数同样可以。

## 升级

```bash
git pull
docker compose build && docker compose up -d     # 重新构建后滚动替换，data/ 不受影响
```

## 关于「这个 Dockerfile 有没有被验证过」

诚实说明：这份 Dockerfile / compose **是按 llm-relay 0.1.0 的文件与默认值写的，但没有在本机实机构建过**
（维护者的环境里没有 Docker）。其中与容器无关的部分已经在本机直跑验证过：

- 容器里用的两条命令（`llm_relay.py --config … --keys … --host 0.0.0.0` 与
  `llm-relay-dashboard.py --host 0.0.0.0 --port … --key-file … --relay …`）在同样的文件集下真实跑通，
  `/health` 与面板均可达；
- `config.json` 里 `usage_log.path` 留空时，用量确实落在 `config.json` 同目录（即 `/data`）。

首次使用请按上面「起」那三步走一遍，任何一步不符合预期都欢迎开 issue 贴 `docker compose logs`。
