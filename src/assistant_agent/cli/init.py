"""`assistant-agent init` 交互式配置向导。

降低首次上手门槛：选后端 → 配 model/env/端点 → 检测 → 生成 config.yaml → 校验。

安全原则（见 docs/m5-init-plan.md）：
- API key 绝不明文写入 config；云端只写 `${环境变量名}`，加载时由 loader 展开。
- init 默认不读取真实 key，只检测环境变量是否已设、并打印设置指引。
- 本地端点写占位 key（非机密）+ api_base，并做带超时的连通检测。
- 已存在 config.yaml 先备份，不静默覆盖。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from assistant_agent.config.loader import ConfigError, load_config
from assistant_agent.llm.client import _bypass_proxy_for_local
from assistant_agent.ui.console import Console

_DEFAULT_ENV = {"cloud": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
_LOCAL_PLACEHOLDER_KEY = "lm-studio"  # 本地端点通常不校验 key，占位即可（非机密）


def check_env_var(name: str) -> bool:
    """环境变量是否已设置（非空）。"""
    import os

    return bool(os.environ.get(name))


def _mask(s: str) -> str:
    """脱敏：只留前 3 字符，其余打码，避免误粘的 key 在提示里再次泄露。"""
    return s[:3] + "***" if len(s) > 3 else "***"


def env_setup_hint(var: str) -> str:
    """按平台给出设置环境变量的指引（只打印，不代改）。"""
    if sys.platform == "win32":
        return (
            f"  PowerShell（持久）：setx {var} \"你的key\"（重开终端生效）\n"
            f"  PowerShell（当前会话）：$env:{var}=\"你的key\""
        )
    return f'  bash/zsh：export {var}="你的key"（可写入 ~/.bashrc 持久化）'


def check_endpoint(base_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """检测本地/自托管端点连通性（GET {base}/models）。本地地址关代理、带超时。"""
    import httpx

    _bypass_proxy_for_local(base_url)
    url = base_url.rstrip("/") + "/models"
    try:
        resp = httpx.get(url, timeout=timeout, trust_env=False)
    except httpx.TimeoutException:
        return False, f"连接超时（>{timeout}s）"
    except Exception as exc:  # noqa: BLE001 - 网络异常类型多，统一归一
        return False, f"连接失败：{exc}"
    if resp.status_code != 200:
        return False, f"响应异常：HTTP {resp.status_code}"
    try:
        n = len(resp.json().get("data", []))
        return True, f"已连接，检测到 {n} 个模型"
    except Exception:  # noqa: BLE001
        return True, "已连接"


def normalize_model(kind: str, model: str) -> str:
    """确保模型名带 LiteLLM 可路由的 provider 前缀。

    用户常填裸模型名（如 LM Studio 里的 `lm_studio`）；LiteLLM 需要 `openai/xxx`
    这类前缀才能判断 provider（本地/OpenAI 兼容端点靠 `openai/` + api_base 路由）。
    已含 `/` 则原样保留（用户已指定 provider）。
    """
    if "/" in model:
        return model
    prefix = "anthropic/" if kind == "anthropic" else "openai/"
    return prefix + model


def generate_config(
    kind: str,
    model: str,
    *,
    api_base: str | None = None,
    env_var: str | None = None,
) -> dict[str, Any]:
    """按选择拼出 config 字典。key 用 ${VAR}（云端）或占位（本地），绝不含明文。"""
    name = kind  # provider 名直接用类别：cloud / anthropic / local
    prov: dict[str, Any] = {"model": normalize_model(kind, model)}
    if kind == "local":
        prov["api_key"] = _LOCAL_PLACEHOLDER_KEY
        prov["api_base"] = api_base
    else:
        prov["api_key"] = f"${{{env_var}}}"  # 例如 ${OPENAI_API_KEY}
        if api_base:
            prov["api_base"] = api_base
    return {"active": name, "providers": {name: prov}}


def write_config(path: Path, data: dict[str, Any]) -> Path | None:
    """写 config；已存在则先备份为 config.yaml.bak-<时间戳>。返回备份路径（无则 None）。"""
    backup: Path | None = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return backup


def validate_generated(path: Path) -> tuple[bool, str]:
    """校验生成的 config 能被 loader 解析、active 存在。"""
    try:
        load_config(path)
        return True, "配置校验通过"
    except ConfigError as exc:
        return False, str(exc)


def run_init(console: Console, config_path: Path | None = None) -> int:
    """交互式向导主流程。返回退出码（0 成功 / 1 失败或取消）。"""
    if not sys.stdin.isatty():
        console.error("init 需要交互式终端运行（非交互环境请直接编辑 config.yaml）。")
        return 1

    path = Path(config_path) if config_path else Path("config.yaml")
    console.info("欢迎使用 Assistant Agent 配置向导。将引导你生成 config.yaml。")

    # 1. 已存在则先问覆盖策略
    if path.exists():
        choice = console.ask_question(
            f"{path} 已存在，如何处理？",
            ["备份后覆盖", "取消"],
        )
        if not choice.startswith("备份"):
            console.info("已取消，未改动任何文件。")
            return 1

    # 2. 选后端
    kind_label = console.ask_question(
        "选择模型后端：",
        [
            "云端 API（OpenAI 兼容：OpenAI / DeepSeek 等）",
            "Anthropic Claude",
            "本地端点（LM Studio / Ollama / vLLM）",
        ],
    )
    if kind_label.startswith("云端"):
        kind = "cloud"
    elif kind_label.startswith("Anthropic"):
        kind = "anthropic"
    else:
        kind = "local"

    # 3. 各后端收集信息
    api_base: str | None = None
    env_var: str | None = None
    if kind == "local":
        api_base = console.input(
            "本地端点 base_url [http://localhost:1234/v1]: "
        ).strip() or "http://localhost:1234/v1"
        model = console.input(
            "模型名（LM Studio 里加载的名字，会自动补 openai/ 前缀）[openai/local-model]: "
        ).strip()
        model = model or "openai/local-model"
        ok, detail = check_endpoint(api_base)
        if ok:
            console.info(f"✅ 端点检测：{detail}")
        else:
            console.info(f"⚠ 端点检测：{detail}（可稍后启动服务后再试，不影响生成配置）")
    else:
        default_model = (
            "anthropic/claude-sonnet-4-6" if kind == "anthropic" else "openai/gpt-4o"
        )
        model = console.input(f"模型名 [{default_model}]: ").strip() or default_model
        if kind == "cloud":
            api_base = console.input(
                "自定义 API base_url（DeepSeek 等填，OpenAI 官方留空）: "
            ).strip() or None
        # 只填变量名（不是 key 本身）。校验为合法标识符，防用户误粘 key（含 - 等会被拒）。
        while True:
            env_var = console.input(
                f"存放 API key 的环境变量名（只填名字，如 {_DEFAULT_ENV[kind]}，不要填 key）: "
            ).strip() or _DEFAULT_ENV[kind]
            if env_var.isidentifier():
                break
            console.error(
                f"'{_mask(env_var)}' 不是合法的变量名。这里只填名字"
                f"（字母/数字/下划线，如 {_DEFAULT_ENV[kind]}）；真实 key 之后用 export 设置。"
            )
        # 只检测环境变量是否已设，绝不读取/写入真实 key
        if check_env_var(env_var):
            console.info(f"✅ 已检测到环境变量 {env_var}")
        else:
            console.info(f"⚠ 环境变量 {env_var} 尚未设置。请这样设置（key 不会写入配置文件）：")
            console.info(env_setup_hint(env_var))

    # 4. 生成 + 备份写入 + 校验
    data = generate_config(kind, model, api_base=api_base, env_var=env_var)
    backup = write_config(path, data)
    if backup is not None:
        console.info(f"原配置已备份为 {backup}")
    console.info(f"已生成 {path}")

    ok, detail = validate_generated(path)
    if not ok:
        console.error(f"配置校验失败：{detail}")
        return 1
    console.info(f"✅ {detail}")
    console.info('下一步：assistant-agent run "列出当前目录有哪些文件"')
    return 0
