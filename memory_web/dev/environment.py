"""Only labelled disposable web acceptance resources; no production mounts."""

import argparse
import base64
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "memory_web/.state"
LABEL = "memory-web-acceptance"
IMAGE = "memory-web-acceptance:approval-manual"
PG = "pgvector/pgvector:0.8.0-pg17@sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87"


def docker(*args, input=None):
    result = subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True
    )
    if result.returncode:
        message = result.stdout + result.stderr
        if (STATE / "state.json").exists():
            for key, value in load().items():
                if "password" in key:
                    message = message.replace(value, "[redacted]")
        raise RuntimeError(message)
    return result.stdout + result.stderr


def load():
    state = json.loads((STATE / "state.json").read_text())
    if not state["project"].startswith("memory-web-slice-"):
        raise RuntimeError("环境身份无效")
    return state


def owned(kind, name):
    row = json.loads(docker(kind, "inspect", name))[0]
    labels = row["Config"].get("Labels") if kind == "container" else row.get("Labels")
    if not labels or labels.get(LABEL) != load()["project"]:
        raise RuntimeError("拒绝操作其他任务的资源")


def build():
    with tempfile.TemporaryDirectory(prefix="memory-web-public-") as directory:
        context = Path(directory)
        for name in ("memory_core", "memory_web"):
            shutil.copytree(
                ROOT / name,
                context / name,
                ignore=shutil.ignore_patterns(
                    ".state", "__pycache__", "*.pyc", ".env", ".env.local"
                ),
            )
        print(
            docker(
                "build",
                "-t",
                IMAGE,
                "-f",
                str(context / "memory_web/dev/Dockerfile"),
                directory,
            )
        )


def start():
    if STATE.exists():
        raise RuntimeError("测试环境已存在；先核对或 destroy，不能覆盖旧测试状态")
    state = dict(
        project="memory-web-slice-" + secrets.token_hex(6),
        admin_password=secrets.token_hex(24),
        owner_password=secrets.token_hex(24),
        app_password=secrets.token_hex(24),
    )
    STATE.mkdir(mode=0o700)
    path = STATE / "state.json"
    path.write_text(json.dumps(state))
    path.chmod(0o600)
    project = state["project"]
    label = f"{LABEL}={project}"
    docker("network", "create", "--internal", "--label", label, project)
    docker("volume", "create", "--label", label, project)
    docker(
        "run",
        "-d",
        "--name",
        project,
        "--label",
        label,
        "--network",
        project,
        "--network-alias",
        "db",
        "--memory",
        "384m",
        "--cpus",
        "1",
        "--mount",
        f"type=volume,source={project},target=/var/lib/postgresql/data",
        "--env",
        f"POSTGRES_PASSWORD={state['admin_password']}",
        PG,
    )
    for _ in range(100):
        probe = subprocess.run(
            [
                "docker",
                "exec",
                project,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
            ],
            capture_output=True,
        )
        if probe.returncode == 0:
            break
        time.sleep(0.3)
    else:
        raise RuntimeError("隔离 Postgres 启动失败")
    bootstrap = "\n".join(
        f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{state[key]}';"
        for role, key in [
            ("memory_owner", "owner_password"),
            ("memory_app", "app_password"),
        ]
    )
    docker(
        "exec",
        "-i",
        project,
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        input=bootstrap + "\nCREATE ROLE memory_outsider NOLOGIN NOSUPERUSER;",
    )
    print("隔离 Postgres 已启动：内部网络、独立卷、无发布端口、无生产配置。")


def run(suite, pattern=None):
    state = load()
    project = state["project"]
    owned("container", project)
    owned("network", project)
    variables = []
    for role, key, password_key in [
        ("postgres", "ADMIN", "admin_password"),
        ("memory_owner", "OWNER", "owner_password"),
        ("memory_app", "APP", "app_password"),
    ]:
        variables += [
            "--env",
            f"MEMORY_TEST_{key}_DSN=host=db dbname=postgres user={role} password={state[password_key]} sslmode=disable",
        ]
    command = ["python", "-m", "unittest", "discover", "-s", suite, "-v"]
    if pattern:
        variables += ['--env', 'MEMORY_TEST_PATTERN=' + pattern]
        command += ['-p', pattern]
    if suite == "memory_core/tests":
        # Preserve the core-only fixture's installation boundary as well.
        command = [
            "python",
            "-c",
            "import os,pathlib,sys,unittest; root=pathlib.Path('/tmp/core-only'); root.mkdir(); (root/'memory_core').symlink_to('/work/memory_core'); os.chdir(root); sys.path.insert(0,str(root)); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('memory_core/tests',pattern=os.environ.get('MEMORY_TEST_PATTERN','test*.py'))); sys.exit(not result.wasSuccessful())",
        ]
    print(
        docker(
            "run",
            "--rm",
            "--name",
            project + ("-core-tests" if suite == "memory_core/tests" else "-tests"),
            "--label",
            f"{LABEL}={project}",
            "--network",
            project,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,size=128m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "384m",
            "--cpus",
            "1",
            *variables,
            IMAGE,
            *command,
        )
    )


def preview(origin=None, *, module='memory_web.testing.preview'):
    state = load()
    project = state["project"]
    owned("container", project)
    variables = []
    for role, key, password_key in [
        ("postgres", "ADMIN", "admin_password"),
        ("memory_owner", "OWNER", "owner_password"),
        ("memory_app", "APP", "app_password"),
    ]:
        variables += [
            "--env",
            f"MEMORY_TEST_{key}_DSN=host=db dbname=postgres user={role} password={state[password_key]} sslmode=disable",
        ]
    if origin is not None:
        if not origin.startswith("https://"):
            raise ValueError("浏览器直达预览必须使用 HTTPS 来源")
        state["preview_password"] = secrets.token_urlsafe(24)
        (STATE / "state.json").write_text(json.dumps(state))
        variables += [
            "--env",
            f"MEMORY_WEB_PREVIEW_ORIGIN={origin}",
            "--env",
            f"MEMORY_WEB_PREVIEW_PASSWORD={state['preview_password']}",
        ]
    print(
        docker(
            "run",
            "-d",
            "--name",
            project + "-preview",
            "--label",
            f"{LABEL}={project}",
            "--network",
            project,
            "--network-alias",
            "preview",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,size=128m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "384m",
            "--cpus",
            "1",
            *variables,
            IMAGE,
            "python",
            "-m",
            module,
        )
    )


def browser_test(browser_image, screenshots, *, script=None):
    state = load()
    project = state["project"]
    name = project + "-browser"
    owned("container", project + "-preview")
    probe = subprocess.run(
        ["docker", "container", "inspect", name], capture_output=True
    )
    if probe.returncode == 0:
        owned("container", name)
        docker("rm", "-f", name)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--name",
            name,
            "--label",
            f"{LABEL}={project}",
            "--network",
            "container:" + project + "-preview",
            "--tmpfs",
            "/tmp:rw,nosuid,size=256m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--entrypoint",
            "python",
            "-i",
            browser_image,
            "-",
        ],
        input=(script or ROOT / "memory_web/tests/browser_acceptance.py").read_text(),
        capture_output=True,
        text=True,
    )
    screenshots.mkdir(parents=True, exist_ok=True)
    for line in result.stdout.splitlines():
        if line.startswith("SCREENSHOT:"):
            artifact = json.loads(line[len("SCREENSHOT:") :])
            filename = artifact["name"]
            if Path(filename).name != filename or not filename.endswith(".png"):
                raise RuntimeError("截图文件名无效")
            (screenshots / filename).write_bytes(base64.b64decode(artifact["data"]))
            print("Saved " + filename)
        else:
            print(line)
    if result.stderr:
        print(result.stderr)
    if result.returncode:
        raise RuntimeError("浏览器验收未通过")


def browser_import_test(browser_image, screenshots):
    # A fresh database keeps import counts and deduplication assertions repeatable.
    start()
    try:
        preview(module="memory_web.testing.import_preview")
        browser_test(
            browser_image, screenshots,
            script=ROOT / "memory_web/tests/browser_import_acceptance.py",
        )
    finally:
        destroy()


def destroy():
    project = load()["project"]
    for kind, name in [
        ("container", project + "-core-tests"),
        ("container", project + "-tests"),
        ("container", project + "-preview"),
        ("container", project + "-browser"),
        ("container", project),
        ("volume", project),
        ("network", project),
    ]:
        probe = subprocess.run(["docker", kind, "inspect", name], capture_output=True)
        if probe.returncode:
            continue
        owned(kind, name)
        docker("rm", "-f", name) if kind == "container" else docker(kind, "rm", name)
    shutil.rmtree(STATE)
    print("本任务的隔离容器、网络、数据卷与凭据已销毁；验收镜像保留供复跑。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "build",
            "start",
            "test",
            "core-test",
            "preview",
            "browser-test",
            "browser-import-test",
            "destroy",
        ],
    )
    parser.add_argument(
        "--browser-image",
        help="Explicit tool image with Playwright 1.64.1 and matching Chromium",
    )
    parser.add_argument(
        "--screenshots", type=Path, help="Absolute screenshot output directory"
    )
    parser.add_argument(
        "--preview-origin", help="Optional HTTPS origin for temporary browser review"
    )
    parser.add_argument('--pattern', help='Optional unittest filename pattern for test/core-test')
    args = parser.parse_args()
    if args.action == "preview":
        preview(args.preview_origin)
        return
    if args.action in ("browser-test", "browser-import-test"):
        if (
            not args.browser_image
            or not args.screenshots
            or not args.screenshots.is_absolute()
        ):
            parser.error(
                f"{args.action} requires --browser-image and an absolute --screenshots"
            )
        test_browser = browser_test if args.action == "browser-test" else browser_import_test
        test_browser(args.browser_image, args.screenshots)
        return
    if args.action in ("test", "core-test"):
        run("memory_core/tests" if args.action == "core-test" else "memory_web/tests", args.pattern)
    else:
        globals()[args.action]()


if __name__ == "__main__":
    main()
