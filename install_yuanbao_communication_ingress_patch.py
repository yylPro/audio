from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


PROJECTS = Path(r"D:\OpenClaw\state\npm\projects")
SOURCE = Path(r"D:\代维\工单提醒\patches\yuanbao\communication-ingress.js")
SUPPORTED_VERSIONS = {"2.15.0", "2.17.0"}
IMPORT_LINE = 'import { communicationIngress } from "./middlewares/communication-ingress.js";'
USE_LINE = ".use(communicationIngress) // Deterministic communication-record ingress; bypasses Agent"


def find_packages() -> list[Path]:
    packages: set[Path] = set()
    for create_file in PROJECTS.glob(
        "openclaw-plugin-yuanbao*/node_modules/openclaw-plugin-yuanbao/dist/src/business/pipeline/create.js"
    ):
        packages.add(create_file.parents[4])
    return sorted(packages)


def patch_package(package: Path) -> None:
    metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    version = str(metadata.get("version", ""))
    if version not in SUPPORTED_VERSIONS:
        raise RuntimeError(f"不支持的元宝插件版本 {version}：{package}")

    create_file = package / "dist/src/business/pipeline/create.js"
    middleware_file = package / "dist/src/business/pipeline/middlewares/communication-ingress.js"
    original = create_file.read_text(encoding="utf-8")
    updated = original
    if IMPORT_LINE not in updated:
        marker = 'import { MessagePipeline } from "./engine.js";'
        if marker not in updated:
            raise RuntimeError(f"找不到管线导入锚点：{create_file}")
        updated = updated.replace(marker, marker + "\n" + IMPORT_LINE, 1)
    if USE_LINE not in updated:
        dispatch_marker = ".use(dispatchReply)"
        if dispatch_marker not in updated:
            raise RuntimeError(f"找不到管线插入锚点：{create_file}")
        updated = updated.replace(dispatch_marker, USE_LINE + "\n        " + dispatch_marker, 1)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if updated != original:
        shutil.copy2(create_file, create_file.with_name(f"create.js.before-communication-ingress-{timestamp}.bak"))
        create_file.write_text(updated, encoding="utf-8", newline="\n")
    middleware_file.parent.mkdir(parents=True, exist_ok=True)
    if middleware_file.exists() and middleware_file.read_bytes() != SOURCE.read_bytes():
        shutil.copy2(
            middleware_file,
            middleware_file.with_name(f"communication-ingress.js.before-{timestamp}.bak"),
        )
    shutil.copy2(SOURCE, middleware_file)
    node = Path(r"D:\Node\node.exe")
    subprocess.run([str(node), "--check", str(create_file)], check=True)
    subprocess.run([str(node), "--check", str(middleware_file)], check=True)
    print(f"patched {package} ({version})")


def main() -> int:
    packages = find_packages()
    if not packages:
        raise RuntimeError("未找到元宝插件安装目录")
    for package in packages:
        patch_package(package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
