#!/usr/bin/env python3
"""Arthas 多 Agent 聚合代理 MCP

对上：以 stdio 暴露工具，每个工具用 target 参数选目标 agent。
对下：
  - 30 个官方工具（镜像 arthas 原生 /mcp，schema 与官方完全一致）走目标 agent 的 /mcp 转发。
  - list_agents / help / execute 为代理自有工具，走目标 agent 的 /api。
agent 清单维护在 agents.yaml；官方工具定义固化在 arthas_tools.json。
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.tools.base import Tool, ToolResult
from mcp.types import TextContent

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
# 黑名单：完全禁用，不暴露 mirror 工具、execute 也拒绝（默认空）
DEFAULT_BLOCKED: list[str] = []
# 灰名单：暴露工具，但执行前需用户通过 MCP elicitation 明确确认
DEFAULT_CONFIRM = ["stop", "redefine", "retransform", "mc", "reset"]
MAX_RESULT_CHARS = 15000  # 单次返回给 LLM 的最大字符数，超出截断


def _config_path() -> Path:
    env = os.environ.get("ARTHAS_AGENTS_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent / "agents.yaml"


def load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"未找到 agent 清单：{path}。请复制 agents.example.yaml 为 agents.yaml 并填写。"
        )
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    defaults = cfg.get("defaults", {}) or {}
    agents: dict[str, Any] = {}
    for a in cfg.get("agents", []):
        merged = {
            "name": a["name"],
            "url": a["url"],
            "username": a.get("username", defaults.get("username", "")),
            "password": a.get("password", defaults.get("password", "")),
            "auth": a.get("auth", defaults.get("auth", "basic")),
            "tags": a.get("tags") or [],
        }
        agents[merged["name"]] = merged
    if not agents:
        raise ValueError(f"{path} 中没有配置任何 agent。")
    return {
        "agents": agents,
        "exec_timeout_ms": int(defaults.get("exec_timeout_ms", 10000)),
        "blocked": defaults.get("blocked_commands", DEFAULT_BLOCKED),
        "confirm": defaults.get("confirm_commands", DEFAULT_CONFIRM),
    }


CONFIG = load_config()
mcp = FastMCP("arthas-proxy")


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------
def _resolve_agent(target: str) -> dict[str, Any]:
    agent = CONFIG["agents"].get(target)
    if agent is None:
        available = ", ".join(CONFIG["agents"].keys())
        raise ValueError(f"未知 target「{target}」。可用：{available}")
    return agent


def _auth_header(agent: dict[str, Any]) -> dict[str, str]:
    if agent.get("auth", "basic") == "bearer":
        return {"Authorization": f"Bearer {agent['password']}"}
    token = base64.b64encode(
        f"{agent.get('username', '')}:{agent.get('password', '')}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}"}


def _truncate(payload: Any) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + f"\n... [输出过长已截断，共 {len(text)} 字符]"
    return text


CONFIRM_HINT = (
    "⚠️ 危险操作「{action}」需要明确确认。请在用户明确同意后，带 confirm=true 重新调用本工具。"
)


# ---------------------------------------------------------------------------
# /api 调用（execute / help 使用）
# ---------------------------------------------------------------------------
async def _call_api(agent: dict[str, Any], command: str, timeout_ms: int) -> Any:
    url = agent["url"].rstrip("/") + "/api"
    payload = {"action": "exec", "command": command, "execTimeout": timeout_ms}
    headers = {"Content-Type": "application/json", **_auth_header(agent)}
    async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 5) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            return {"error": f"连接 {url} 失败：{exc}"}
    if resp.status_code == 401:
        return {"error": f"认证失败(401)：检查 {agent['name']} 的 username/password/auth 配置。"}
    if resp.status_code >= 400:
        return {"error": f"HTTP {resp.status_code}：{resp.text[:500]}"}
    try:
        data = resp.json()
    except ValueError:
        return {"error": f"响应非 JSON：{resp.text[:500]}"}
    body = data.get("body", {})
    state = data.get("state") or body.get("jobStatus")
    results = [r for r in body.get("results", []) if r.get("type") != "status"]
    return {"agent": agent["name"], "command": command, "state": state, "results": results}


async def _exec(target: str, command: str, timeout_ms: int | None) -> str:
    agent = _resolve_agent(target)
    timeout = timeout_ms or CONFIG["exec_timeout_ms"]
    return _truncate(await _call_api(agent, command, timeout))


# ---------------------------------------------------------------------------
# 代理自有工具：发现 / 帮助 / 万能执行
# ---------------------------------------------------------------------------
@mcp.tool
def list_agents(tag: str | None = None) -> str:
    """列出可用的 arthas agent（target 名称、地址、tags，不含凭证）。

    tag: 可选，只返回带该 tag 的 agent（按业务/环境分组批量定位机器）。
    """
    items = []
    for a in CONFIG["agents"].values():
        if tag and tag not in a["tags"]:
            continue
        items.append({"name": a["name"], "url": a["url"], "tags": a["tags"]})
    return json.dumps({"count": len(items), "agents": items}, ensure_ascii=False, indent=2)


@mcp.tool
async def help(target: str, command: str | None = None) -> str:
    """查看目标 agent 上 arthas 的命令帮助（权威、与实际版本一致）。

    command 为空：列出所有可用命令。
    指定命令：查看该命令的详细用法、参数和示例，如 help(target, "trace")。
    """
    cmd = f"help {command}" if command else "help"
    return await _exec(target, cmd, None)


@mcp.tool
async def execute(
    target: str, command: str, timeout_ms: int | None = None, confirm: bool = False
) -> str:
    """在指定 agent 上执行任意 arthas 命令（兜底入口，可跑组合命令）。

    target: agent 名称（见 list_agents）
    command: 完整 arthas 命令，如 "thread -n 3"
    timeout_ms: 执行超时（毫秒），默认取配置
    confirm: 危险命令的确认开关。灰名单命令必须 confirm=true 才执行；请在用户明确同意后再设。
    优先用 30 个官方结构化工具；本工具用于它们覆盖不到的组合场景。
    危险命令：黑名单直接拒绝；灰名单（stop/redefine/retransform/mc/reset）需 confirm=true。
    """
    head = command.strip().split()[0] if command.strip() else ""
    if head in CONFIG["blocked"]:
        return json.dumps(
            {"error": f"命令「{head}」在黑名单中，已禁用。"}, ensure_ascii=False
        )
    if head in CONFIG["confirm"] and not confirm:
        return json.dumps(
            {"need_confirm": CONFIRM_HINT.format(action=f"{head}（在 {target} 上）")},
            ensure_ascii=False,
        )
    return await _exec(target, command, timeout_ms)


# ---------------------------------------------------------------------------
# 30 个官方工具：镜像 arthas 原生 /mcp，schema 与官方一致，按 target 转发
# ---------------------------------------------------------------------------
def _downstream_transport(agent: dict[str, Any]) -> StreamableHttpTransport:
    return StreamableHttpTransport(
        url=agent["url"].rstrip("/") + "/mcp", headers=_auth_header(agent)
    )


class MirrorTool(Tool):
    """镜像官方 arthas 工具：取出 target，将其余参数原样转发到该 agent 的 /mcp 同名工具。"""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        if self.name in CONFIG["blocked"]:
            return ToolResult(
                content=[TextContent(type="text", text=f"工具「{self.name}」在黑名单中，已禁用。")]
            )
        args = dict(arguments)
        target = args.pop("target", None)
        if target not in CONFIG["agents"]:
            available = ", ".join(CONFIG["agents"].keys())
            return ToolResult(
                content=[TextContent(type="text", text=f"未知 target「{target}」。可用：{available}")]
            )
        if self.name in CONFIG["confirm"] and not args.pop("confirm", False):
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=CONFIRM_HINT.format(action=f"{self.name}（在 {target} 上）"),
                    )
                ]
            )
        agent = CONFIG["agents"][target]
        try:
            async with Client(_downstream_transport(agent)) as client:
                result = await client.call_tool(self.name, args)
        except Exception as exc:  # noqa: BLE001 - fail loud
            return ToolResult(
                content=[TextContent(type="text", text=f"调用 {target} 的 {self.name} 失败：{exc}")]
            )
        text = result.content[0].text if result.content else "(空结果)"
        return ToolResult(content=[TextContent(type="text", text=_truncate(text))])


def _register_mirror_tools() -> int:
    path = Path(__file__).resolve().parent / "arthas_tools.json"
    defs = json.loads(path.read_text(encoding="utf-8"))
    blocked = set(CONFIG["blocked"])
    registered = 0
    for d in defs:
        if d["name"] in blocked:
            continue  # 黑名单工具不暴露给 LLM
        base = d.get("inputSchema") or {"type": "object", "properties": {}, "required": []}
        props = {
            "target": {"type": "string", "description": "目标 agent 名称（见 list_agents）"}
        }
        props.update(base.get("properties", {}))
        if d["name"] in CONFIG["confirm"]:
            props["confirm"] = {
                "type": "boolean",
                "description": "危险操作确认开关，必须为 true 才执行；请在用户明确同意后再设置。",
            }
        schema = {
            "type": "object",
            "properties": props,
            "required": ["target"] + list(base.get("required", [])),
            "additionalProperties": False,
        }
        mcp.add_tool(MirrorTool(name=d["name"], description=d["description"], parameters=schema))
        registered += 1
    return registered


_register_mirror_tools()


if __name__ == "__main__":
    mcp.run()
