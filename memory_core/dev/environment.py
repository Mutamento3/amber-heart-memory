"""Only manages this slice's labelled, private disposable Docker resources."""

import argparse
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state"
LABEL = "memory-core-storage-slice"
PG_IMAGE = "pgvector/pgvector:0.8.0-pg17@sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87"
TEST_IMAGE = "memory-core-storage-tests:20261029"


def docker(*args, input=None):
    result = subprocess.run(["docker", *args], input=input, capture_output=True, text=True)
    if result.returncode:
        # Do not print argument lists: they may contain isolated DB credentials.
        message = result.stderr
        if (STATE / "state.json").exists():
            state = json.loads((STATE / "state.json").read_text())
            for key in ("admin_password", "owner_password", "app_password"):
                message = message.replace(state[key], "[redacted]")
        raise RuntimeError(message.strip())
    return result.stdout + result.stderr


def load():
    if not (STATE / "state.json").is_file():
        raise RuntimeError("没有本工具创建的测试环境；先运行 start")
    state = json.loads((STATE / "state.json").read_text())
    if not state["project"].startswith("memory-core-slice-"):
        raise RuntimeError("测试环境身份无效")
    return state


def assert_owned(kind, name, project):
    details = json.loads(docker(kind, "inspect", name))[0]
    labels = details.get("Labels") if kind in ("network", "volume") else details["Config"].get("Labels")
    if not labels or labels.get(LABEL) != project:
        raise RuntimeError("拒绝操作不属于本测试环境的资源")


def wait_ready(name):
    for _ in range(60):
        ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], capture_output=True)
        if ready.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError("隔离数据库未能及时启动；运行 destroy 清理后排查")


def start():
    if STATE.exists():
        state = load()
        assert_owned("container", state["project"], state["project"])
        assert_owned("network", state["project"], state["project"])
        docker("start", state["project"])
        wait_ready(state["project"])
        print("已有隔离环境已启动，保留其合成数据与凭据。")
        return
    docker("build", "--tag", TEST_IMAGE, "--file", str(ROOT / "dev/Dockerfile"), str(ROOT))
    state = {
        "project": "memory-core-slice-" + secrets.token_hex(6),
        "admin_password": secrets.token_hex(24),
        "owner_password": secrets.token_hex(24),
        "app_password": secrets.token_hex(24),
    }
    STATE.mkdir(mode=0o700)
    file = STATE / "state.json"
    file.write_text(json.dumps(state))
    file.chmod(0o600)
    name = state["project"]
    label = f"{LABEL}={name}"
    # No published ports, host data paths, production networks or Docker socket mounts.
    docker("network", "create", "--internal", "--label", label, name)
    docker("volume", "create", "--label", label, name)
    docker("run", "-d", "--name", name, "--label", label,
        "--network", name, "--network-alias", "db", "--memory", "384m", "--cpus", "1",
        "--mount", f"type=volume,source={name},target=/var/lib/postgresql/data",
        "--env", f"POSTGRES_PASSWORD={state['admin_password']}", PG_IMAGE)
    wait_ready(name)
    bootstrap = f"""
        CREATE ROLE memory_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{state['owner_password']}';
        CREATE ROLE memory_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{state['app_password']}';
        CREATE ROLE memory_outsider NOLOGIN NOSUPERUSER;
    """
    docker("exec", "-i", name, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres", input=bootstrap)
    print("隔离环境已启动；无公开端口、独立数据卷、独立凭据、无定时任务。")


def test(pattern=None):
    state = load()
    project = state["project"]
    assert_owned("container", project, project)
    assert_owned("network", project, project)
    env = []
    for role, key, password in (
        ("postgres", "ADMIN", state["admin_password"]),
        ("memory_owner", "OWNER", state["owner_password"]),
        ("memory_app", "APP", state["app_password"]),
    ):
        env.extend(["--env", f"MEMORY_TEST_{key}_DSN=host=db dbname=postgres user={role} password={password} sslmode=disable"])
    # Only the candidate public subset is mounted. The private repository,
    # host .env, datasets and production credentials are absent.
    runner = project + "-tests"
    selection = ["-p", pattern] if pattern else []
    result = docker("run", "--rm", "--name", runner, "--label", f"{LABEL}={project}",
        "--network", project, "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--user", f"{ROOT.stat().st_uid}:{ROOT.stat().st_gid}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "256m", "--cpus", "1",
        "--mount", f"type=bind,source={ROOT},target=/work/memory_core,readonly",
        *env, TEST_IMAGE, "python", "-m", "unittest", "discover", "-s", "memory_core/tests", *selection, "-v")
    print(result, end="")
    print("真实数据库测试完成；结果中的数量以 unittest 输出为准。")


def stop():
    state = load()
    assert_owned("container", state["project"], state["project"])
    docker("stop", state["project"])
    print("测试库已停止，合成数据卷和私有凭据保留；destroy 会彻底销毁。")


def destroy():
    state = load()
    project = state["project"]
    # Exact names and ownership labels are both required; no prune operations.
    for kind, name in (("container", project + "-tests"), ("container", project), ("volume", project), ("network", project)):
        probe = subprocess.run(["docker", kind, "inspect", name], capture_output=True)
        if probe.returncode:
            continue
        assert_owned(kind, name, project)
        if kind == "container":
            docker("rm", "-f", name)
        else:
            docker(kind, "rm", name)
    shutil.rmtree(STATE)
    print("本测试环境的容器、独立数据卷、网络及凭据已销毁；测试镜像保留供复跑。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "test", "stop", "destroy"))
    parser.add_argument("--pattern", help="optional unittest filename pattern, only for test")
    args = parser.parse_args()
    try:
        if args.action == "test":
            test(args.pattern)
        else:
            if args.pattern:
                parser.error("--pattern only applies to test")
            globals()[args.action]()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
