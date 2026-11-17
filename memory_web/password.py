"""Interactive password provisioning. Only a salted hash is written to disk."""

import argparse
import getpass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import tempfile


PATTERN = re.compile(r"scrypt-v1\$[0-9a-f]{32}\$[0-9a-f]{64}")


def hash_password(password):
    if not 15 <= len(password) <= 256 or len(password.encode()) > 1024:
        raise ValueError("密码须为 15–256 个字符，且 UTF-8 不超过 1024 字节")
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=32768,
        r=8,
        p=3,
        maxmem=64 * 1024 * 1024,
        dklen=32,
    )
    return f"scrypt-v1${salt.hex()}${value.hex()}"


def read_hash(path):
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("密码哈希文件必须为私有普通文件（权限 0600）")
    value = path.read_text().strip()
    if not PATTERN.fullmatch(value):
        raise ValueError("密码哈希文件无效，请运行 memory_web.password 重设")
    return value


def verify_password(password, encoded):
    if len(password.encode()) > 1024:
        return False
    _, salt, expected = encoded.split("$")
    actual = hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt),
        n=32768,
        r=8,
        p=3,
        maxmem=64 * 1024 * 1024,
        dklen=32,
    )
    return hmac.compare_digest(actual.hex(), expected)


def write_password(path, password):
    value = hash_password(password)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".password-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file", type=Path, default=os.environ.get("MEMORY_WEB_PASSWORD_FILE")
    )
    args = parser.parse_args()
    try:
        if args.file is None or not args.file.is_absolute():
            raise ValueError("通过 --file 或 MEMORY_WEB_PASSWORD_FILE 指定绝对路径")
        password = getpass.getpass("新的管理员密码：")
        if password != getpass.getpass("再输入一次："):
            raise ValueError("两次密码不一致")
        write_password(args.file, password)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"设置停止：{exc}\n")
    print("密码已重设；所有旧会话立即失效，无须重启服务。")


if __name__ == "__main__":
    main()
