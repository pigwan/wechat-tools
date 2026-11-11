"""把采集插件安装进 helper，并按本机配置生成 bot_config.json。

可重复执行：已存在的 bot_config.json 会被保留（只更新 report_bridge 段），
其它插件统一关闭以节省内存。配置来源为 config.local.json。

    python deploy.py
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
HELPER = WORKSPACE / "vendor" / "helper-main"
CONFIG_LOCAL = ROOT / "config.local.json"
PLUGIN_SRC = ROOT / "plugin"
PLUGIN_DST = HELPER / "plugins" / "report_bridge"

_RUNTIME_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")
_PATCH_MARKER = "# --- portable VC++ runtime preload ---"
_NEEDLE = (
    '                dll_parameter = allocate((str(dll) + "\\0").encode("utf-16-le"))\n'
    '                remote_call(process, remote_system_function(pid, b"LoadLibraryW"), dll_parameter)'
)
_PATCH = (
    '                # --- portable VC++ runtime preload ---\n'
    '                # WeChat 4.2.0.12 does not load MSVCP140.dll. Preload the\n'
    '                # CRT by absolute path so the bridge imports resolve from\n'
    '                # the target process loaded-module list.\n'
    '                for _crt in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):\n'
    '                    _crt_path = dll.parent / _crt\n'
    '                    if _crt_path.is_file():\n'
    '                        _arg = allocate((str(_crt_path) + "\\0").encode("utf-16-le"))\n'
    '                        remote_call(process, remote_system_function(pid, b"LoadLibraryW"), _arg)\n'
    '                # --- end portable VC++ runtime preload ---\n'
) + _NEEDLE

DEFAULT_PLUGIN_OPTIONS = {
    "only_groups": True,
    "include_self": False,
    "collect_official": True,
    "group_allowlist": [],
    "batch_size": 50,
    "flush_seconds": 30,
    "http_timeout_seconds": 5,
    "report_keywords": ["/报告", "/今日报告"],
    "report_top_n": 5,
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def patch_native_runtime():
    """微信 4.2.0.12 进程不加载 MSVCP140，远程 LoadLibraryW(text_bridge.dll)
    会因依赖缺失失败。把便携 Python 自带的 VC++ 运行库复制到桥接目录，
    并在注入前按绝对路径预加载进微信进程（幂等）。"""
    source = WORKSPACE / "runtime" / "python313"
    build = HELPER / "native" / "build_text"
    for name in _RUNTIME_DLLS:
        src, dst = source / name, build / name
        # 桥接正在运行时，这些 DLL 已被微信进程加载，Windows 会锁住文件。
        # 内容一致时直接跳过；仅在确实缺失或不同才尝试覆盖。
        if dst.is_file():
            try:
                if dst.read_bytes() == src.read_bytes():
                    continue
            except OSError:
                continue
        try:
            shutil.copy2(src, dst)
        except PermissionError:
            print("提示：%s 正被微信占用，已保留现有副本（停止桥接后重跑可更新）" % name)
    target = HELPER / "native" / "run_selftest.py"
    text = target.read_text(encoding="utf-8")
    if _PATCH_MARKER in text:
        return "运行库补丁已存在（跳过）"
    if _NEEDLE not in text:
        raise SystemExit("helper 源码结构变化，无法自动打补丁：native/run_selftest.py")
    target.write_text(text.replace(_NEEDLE, _PATCH, 1), encoding="utf-8")
    return "已注入 VC++ 运行库预加载补丁"


def main():
    if not HELPER.is_dir():
        raise SystemExit("未找到 helper 源码目录：%s" % HELPER)
    local = read_json(CONFIG_LOCAL)
    token = str(local.get("token") or "")
    endpoint = str(local.get("endpoint") or "")
    if not token or token == "CHANGE_ME" or not endpoint:
        raise SystemExit("请先在 config.local.json 填好 token 与 endpoint")

    # 1. 安装插件源码
    PLUGIN_DST.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "plugin.json"):
        shutil.copy2(PLUGIN_SRC / name, PLUGIN_DST / name)
    print("插件已安装到 %s" % PLUGIN_DST)

    # 2. 补齐微信进程缺失的 VC++ 运行库（否则桥接 DLL 加载失败）
    print("运行库：%s" % patch_native_runtime())

    # 3. 生成/更新 bot_config.json
    target = HELPER / "bot_config.json"
    if target.exists():
        config = read_json(target)
    else:
        config = read_json(HELPER / "bot_config.example.json")
        config["chat_allowlist"] = []
        config["plugin_queue_size"] = 32
        config["console"] = True
    config["backend"] = "native_text"

    plugins = config.setdefault("plugins", {})
    for name, options in plugins.items():
        if name != "report_bridge" and isinstance(options, dict):
            options["enabled"] = False
    entry = dict(DEFAULT_PLUGIN_OPTIONS)
    existing = plugins.get("report_bridge")
    if isinstance(existing, dict):
        entry.update(existing)  # 保留用户手工调整过的参数
    entry.update({"enabled": True, "endpoint": endpoint, "token": token})
    plugins["report_bridge"] = entry

    target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print("配置已写入 %s" % target)
    print("  backend       = native_text")
    print("  chat_allowlist= %s" % config.get("chat_allowlist"))
    print("  endpoint      = %s" % endpoint)
    print("  其余插件已全部关闭（省内存）；如需只收指定群，把群 ID 填进 chat_allowlist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
