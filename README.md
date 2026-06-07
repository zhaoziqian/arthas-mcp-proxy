# arthas-mcp-proxy

Arthas 多 Agent 聚合代理 MCP。Claude 端只配 **1 个** MCP，即可诊断任意多个已启动 arthas-agent 的 Java 进程。

## 原理

```
Claude (Code / Desktop) ──stdio──> server.py ──┬─ /mcp 转发（30 个官方工具）──> 各 arthas agent
                                       │读      └─ /api 执行（list_agents/help/execute）
                                       ├─ agents.yaml      （agent 清单 + tags）
                                       └─ arthas_tools.json（官方 30 工具 schema，固化）
```

对外暴露 **33 个工具**，每个用 `target` 参数选目标 agent。新增服务**只改 `agents.yaml`**，Claude 端零改动。

## 工具

**代理自有（走 /api）：**
- `list_agents(tag?)` — 列出所有可用 target，可按 tag 筛选
- `help(target, command?)` — 查 arthas 命令的权威帮助/语法
- `execute(target, command, timeout_ms?)` — 兜底执行任意命令（危险命令走灰名单确认 / 黑名单拦截）

**30 个官方工具（镜像 arthas 原生 /mcp，schema 与官方 100% 一致，走 /mcp 转发）：**
`jvm` `dashboard` `memory` `thread` `sysprop` `sysenv` `perfcounter` `mbean` `vmoption` `vmtool` `options`
`sc` `sm` `jad` `getstatic` `classloader` `dump` `redefine` `retransform`
`trace` `watch` `stack` `monitor` `tt` `profiler`
`ognl` `mc` `heapdump` `viewfile` `stop`

> 每个官方工具多一个 `target` 参数，其余参数与官方完全相同。调用如 `thread(target="xxx服务", topN=3)`。

## 工作机制

- **官方工具定义固化在 `arthas_tools.json`**（启动不联网拉取）。`MirrorTool` 子类读取它批量注册，运行时取出 `target` → 连该 agent 的 `/mcp` → 转调同名工具 → 原样返回。
- 因此工具的参数/描述始终与官方一致，且 arthas 升级后只需重新 dump 一次。

### arthas 升级后刷新工具定义

```bash
.venv/bin/python -c "
import asyncio, json
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
tp = StreamableHttpTransport('http://xx.xx.xx.xx:8563/mcp', headers={'Authorization':'Bearer arthas'})
async def main():
    async with Client(tp) as c:
        tools = await c.list_tools()
        json.dump([{'name':t.name,'description':t.description,'inputSchema':t.inputSchema} for t in tools],
                  open('arthas_tools.json','w'), ensure_ascii=False, indent=2)
        print('refreshed', len(tools))
asyncio.run(main())
"
```

## 配置（agents.yaml）

```yaml
agents:
  - name: "xxx服务"
    url: http://xx.xx.xx.xx:8563
    tags: ["测试", "IC", "biz"]   # 打标签，便于 list_agents(tag=...) 批量定位
defaults:
  username: arthas     # 公共账号，所有 agent 共用（个别不同可在该 agent 下覆盖）
  password: arthas
  auth: basic          # basic | bearer
  exec_timeout_ms: 10000
  confirm_commands: [stop, redefine, retransform, mc, reset]  # 灰名单：执行前需用户确认
  blocked_commands: []                                        # 黑名单：完全禁用、不暴露
```

配置路径默认本目录 `agents.yaml`，也可用环境变量 `ARTHAS_AGENTS_CONFIG` 指定。

## 安全控制：灰名单 / 黑名单

危险操作分两级，对 **30 个官方工具**和 **execute** 两条路径统一生效：

- **灰名单 `confirm_commands`**：工具正常暴露，但带一个 `confirm` 参数。不带 `confirm=true` 调用会被拦下并返回警告，需在用户明确同意后带 `confirm=true` 重新调用才执行。默认含 `stop/redefine/retransform/mc/reset`。
- **黑名单 `blocked_commands`**：完全禁用——mirror 工具不暴露、execute 直接拒绝。默认空。

> 说明：原计划用 MCP elicitation 做弹窗确认，但实测 Claude 客户端在自动流程下会自动 decline、不弹窗，故改用更可靠的 `confirm` 参数二次确认。改 `agents.yaml` 这两项即可调整，重启 Claude 生效。

## 安装

```bash
cd /path/to/arthas-mcp-proxy
uv venv && uv pip install fastmcp httpx pyyaml
```

## 接入 Claude

**Claude Code（local scope）：**
```bash
claude mcp add arthas-proxy --scope local -- \
  /path/to/arthas-mcp-proxy/.venv/bin/python /path/to/arthas-mcp-proxy/server.py
```

**Claude Desktop**（`claude_desktop_config.json`）：
```json
"arthas-proxy": {
  "command": "/path/to/arthas-mcp-proxy/.venv/bin/python",
  "args": ["/path/to/arthas-mcp-proxy/server.py"]
}
```

改动 `server.py` / `agents.yaml` 后需重启 Claude 生效。

## 本地冒烟测试

```bash
.venv/bin/python -c "
import asyncio, server
from fastmcp import Client
async def main():
    async with Client(server.mcp) as c:
        print('工具数:', len(await c.list_tools()))
        r = await c.call_tool('jvm', {'target':'xxx服务'})
        print(r.content[0].text[:120])
asyncio.run(main())
"
```
