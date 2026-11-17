"""Build libpq-safe credentials for the compose database, then install or serve."""

import argparse
import os

from psycopg.conninfo import make_conninfo

from memory_core.config import Settings
from memory_core.http_models import EmbeddingDimensionError, HTTPRecallModels
from memory_core.http_transport import ModelCallError
from memory_core.model_config import ModelRole, RecallModelsConfig


def verify_models(env, dimensions, *, transport=None):
    """Probe each configured installation role without opening a database."""
    main_url = env.get("MEMORY_UPSTREAM_BASE_URL") or None
    main_key = env.get("MEMORY_UPSTREAM_API_KEY") or None
    roles = (
        ("嵌入", "EMBEDDING"),
        ("第一级精排", "DOCUMENT_RERANK"),
        ("对话（轻任务）", "KEYWORD"),
    )
    sample = "请找出与散步有关的记忆。"

    for label, name in roles:
        try:
            model_name = env.get(f"MEMORY_{name}_MODEL") or None
            if name == "KEYWORD":
                if not model_name:
                    continue
                model = ModelRole(model_name, main_url, main_key)
                config = RecallModelsConfig(cleaner=model, timeout_seconds=20)
                HTTPRecallModels(config, dimensions, transport=transport).clean_query(
                    sample, prompt="只用一个关键词回答。"
                )
            else:
                base_url = env.get(f"MEMORY_{name}_BASE_URL") or None
                api_key = env.get(f"MEMORY_{name}_API_KEY") or None
                if not any((model_name, base_url, api_key)):
                    continue
                model = ModelRole(model_name, base_url, api_key)
                if name == "EMBEDDING":
                    config = RecallModelsConfig(
                        embedding=model.resolve(main_url, main_key), timeout_seconds=20,
                    )
                    HTTPRecallModels(config, dimensions, transport=transport).embed_for(
                        sample, purpose="query"
                    )
                else:
                    config = RecallModelsConfig(
                        reranker=model, timeout_seconds=20,
                        document_rerank_format=env.get("MEMORY_DOCUMENT_RERANK_FORMAT", "voyage"),
                    )
                    adapter = HTTPRecallModels(config, dimensions, transport=transport)
                    adapter.rerank(
                        adapter.prepare_rerank_query(sample),
                        [1, 2],
                        documents=["傍晚去公园散步。", "午饭吃了面条。"],
                    )
        except EmbeddingDimensionError as exc:
            raise ValueError(f"{label}验证失败。{exc}") from None
        except (ModelCallError, ValueError) as exc:
            code = exc.code if isinstance(exc, ModelCallError) else ""
            if code.endswith(("_401", "_403")):
                detail = "请核对 API key 是否有效、是否有权使用这个模型"
            elif code.endswith("_429"):
                detail = "服务商限流或额度不足，请核对账户额度或稍后重试"
            elif code == "document_rerank_format_mismatch":
                detail = "返回格式与配置不符，请核对接口地址及 voyage/cohere 格式"
            elif code.endswith("_timeout"):
                detail = "请求超时，请核对接口地址和服务器网络"
            else:
                detail = "请核对接口地址、API key 和模型名"
            raise ValueError(f"{label}验证失败。{detail}。") from None
        print(f"{label}验证通过。", flush=True)


def deploy_environment(env):
    values = {}
    for role, variable in (
        ("memory_owner", "MEMORY_CONFIG_DSN"),
        ("memory_app", "MEMORY_DSN"),
    ):
        password = env.get(
            "MEMORY_DB_" + ("OWNER" if role == "memory_owner" else "APP") + "_PASSWORD"
        )
        if not password:
            raise ValueError("数据库密码未配置，请先运行 ./install.sh")
        values[variable] = make_conninfo(
            host="db",
            port=5432,
            dbname="amber_heart",
            user=role,
            password=password,
            sslmode="disable",
        )
    values["MEMORY_WEB_PASSWORD_FILE"] = "/var/lib/memory-admin/password.hash"
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "serve", "verify-models"))
    parser.add_argument("--dimensions", type=int, default=1536)
    parser.add_argument("--timezone", default=Settings().timezone)
    parser.add_argument("--day-start-hour", type=int, default=Settings().day_start_hour)
    args = parser.parse_args()
    if args.action == "verify-models":
        try:
            verify_models(os.environ, Settings(args.dimensions).dimensions)
        except ValueError as exc:
            parser.exit(2, f"安装停止：{exc}\n")
        return
    os.environ.update(deploy_environment(os.environ))
    if args.action == "install":
        from memory_core.install import install
        from memory_web.install import install_web

        install(
            os.environ["MEMORY_CONFIG_DSN"],
            Settings(args.dimensions, args.timezone, args.day_start_hour),
        )
        install_web(os.environ["MEMORY_CONFIG_DSN"])
    else:
        os.execvp(
            "uvicorn",
            [
                "uvicorn",
                "memory_web.app:create_app",
                "--factory",
                "--host",
                "0.0.0.0",
                "--port",
                "8080",
                "--workers",
                "1",
                "--limit-concurrency",
                "8",
                "--no-access-log",
                "--no-proxy-headers",
            ],
        )


if __name__ == "__main__":
    main()
