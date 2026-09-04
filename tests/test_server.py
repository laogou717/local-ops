import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import server


class ParsingTests(unittest.TestCase):
    def test_parse_etime(self):
        self.assertEqual(server.parse_etime("02:03"), 123)
        self.assertEqual(server.parse_etime("01:02:03"), 3723)
        self.assertEqual(server.parse_etime("2-01:02:03"), 176523)
        self.assertEqual(server.parse_etime("bad"), 0)

    def test_validate_port(self):
        self.assertEqual(server.validate_port("8791"), (8791, None))
        self.assertEqual(server.validate_port(None), (None, None))
        self.assertIsNotNone(server.validate_port(True)[1])
        self.assertIsNotNone(server.validate_port(70000)[1])

    def test_listener_scan_preserves_ipv6_loopback_for_open_links(self):
        output = """COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
node 101 user 1u IPv6 0x0 0t0 TCP [::1]:5173 (LISTEN)
node 202 user 2u IPv4 0x0 0t0 TCP 127.0.0.1:8000 (LISTEN)
node 303 user 3u IPv6 0x0 0t0 TCP *:3000 (LISTEN)
"""
        with mock.patch.object(server, "run_cmd", return_value=output):
            listeners = server.scan_listeners()

        self.assertEqual(listeners[(101, 5173)], {"::1"})
        self.assertEqual(
            server.listener_open_host(listeners, 5173, {101}), "localhost")
        self.assertEqual(
            server.listener_open_host(listeners, 8000, {202}), "127.0.0.1")
        self.assertEqual(
            server.listener_open_host(listeners, 3000, {303}), "127.0.0.1")


class OriginAttributionTests(unittest.TestCase):
    """attribute_origin：沿 PPID 链识别 AI 助手 / 编辑器 / 终端 / 总控台。"""

    @staticmethod
    def table(*rows):
        # rows: (pid, ppid, args)
        return {pid: (ppid, args) for pid, ppid, args in rows}

    def test_codex_chain_skips_shells_and_package_managers(self):
        table = self.table(
            (100, 90, "node server.mjs --open"),
            (90, 80, "node npm exec"),
            (80, 70, "pnpm dev"),
            (70, 60, "-zsh"),
            (60, 1, "/usr/local/bin/codex"),
        )
        origin = server.attribute_origin(100, table)
        self.assertEqual(origin, {"label": "Codex", "icon": "bot"})

    def test_vscode_bundle_is_named_and_uses_code_icon(self):
        table = self.table(
            (100, 90, "python3 -m http.server 8000"),
            (90, 80, "-zsh"),
            (80, 1, "/Applications/Visual Studio Code.app/Contents/MacOS/Electron"),
        )
        origin = server.attribute_origin(100, table)
        self.assertEqual(origin, {"label": "VS Code", "icon": "code"})

    def test_iterm_bundle_uses_terminal_icon(self):
        table = self.table(
            (100, 90, "-zsh"),
            (90, 1, "/Applications/iTerm.app/Contents/MacOS/iTerm2"),
        )
        origin = server.attribute_origin(100, table)
        self.assertEqual(origin, {"label": "iTerm", "icon": "terminal"})

    def test_console_run_token_marks_console_as_origin(self):
        table = self.table(
            (100, 90, "python3 -m http.server 8377"),
            (90, 1, "/bin/bash -c outer console-run:tok123 inner"),
        )
        origin = server.attribute_origin(100, table)
        self.assertEqual(origin, {"label": "总控台", "icon": "rocket"})

    def test_unknown_middle_process_is_named_honestly(self):
        table = self.table(
            (100, 90, "node server.js"),
            (90, 1, "/opt/homebrew/bin/mise run dev"),
        )
        origin = server.attribute_origin(100, table)
        self.assertEqual(origin, {"label": "mise", "icon": "package"})

    def test_launchd_parent_reports_system(self):
        table = self.table(
            (100, 90, "redis-server"),
            (90, 1, "launchd"),
        )
        origin = server.attribute_origin(100, table)
        self.assertEqual(origin, {"label": "系统", "icon": "server"})

    def test_missing_parent_returns_none(self):
        self.assertIsNone(server.attribute_origin(100, {}))

    def test_cycle_terminates_safely(self):
        table = self.table(
            (100, 90, "a"),
            (90, 100, "b"),
        )
        # 环会在 visited 集合处终止；最近的未识别进程作为兜底答案
        origin = server.attribute_origin(100, table)
        self.assertEqual(origin, {"label": "b", "icon": "package"})

    def test_unknown_wrapper_does_not_hide_the_real_agent(self):
        # 未识别的中间层继续上爬，优先报告真正的 AI 助手
        table = self.table(
            (100, 90, "node server.js"),
            (90, 80, "/opt/homebrew/bin/mise run dev"),
            (80, 1, "/usr/local/bin/claude"),
        )
        origin = server.attribute_origin(100, table)
        self.assertEqual(origin, {"label": "Claude Code", "icon": "bot"})


class ScriptCommandTests(unittest.TestCase):
    def test_script_extensions_choose_the_expected_runtime_and_quote_paths(self):
        cases = {
            ".py": "python3",
            ".zsh": "/bin/zsh",
            ".sh": "/bin/bash",
            ".bash": "/bin/bash",
        }
        with tempfile.TemporaryDirectory() as td:
            for suffix, runner in cases.items():
                with self.subTest(suffix=suffix):
                    path = os.path.join(td, "job's file" + suffix)
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write("echo ok\n")
                    parts = shlex.split(server.command_for_script(path))
                    self.assertEqual(parts, [runner, "--", path])

    def test_executable_command_is_invoked_directly(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "nightly job.command")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/zsh\necho ok\n")
            os.chmod(path, 0o700)
            self.assertEqual(
                shlex.split(server.command_for_script(path)), [path])

    def test_non_executable_command_uses_bash(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "nightly job.command")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("echo ok\n")
            os.chmod(path, 0o600)
            self.assertEqual(
                shlex.split(server.command_for_script(path)),
                ["/bin/bash", "--", path])


class AppHealthTests(unittest.TestCase):
    def test_python_script_with_spaces_is_checked_without_running_it(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "daily task.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("raise RuntimeError('must not execute')\n")
            app = {"id": "deadbeef", "kind": "task", "cwd": td,
                   "command": server.command_for_script(path)}
            self.assertEqual(server.inspect_app_health(app)["status"], "ok")
            os.unlink(path)
            health = server.inspect_app_health(app)

        self.assertTrue(health["blocking"])
        self.assertEqual(health["issues"][0]["kind"], "script-missing")
        self.assertEqual(health["issues"][0]["action"], "pick-script")

    def test_relative_script_uses_configured_working_directory(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "job.sh")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("echo ok\n")
            app = {"command": "/bin/bash -- job.sh", "cwd": td}
            self.assertFalse(server.inspect_app_health(app)["blocking"])

    def test_missing_cwd_does_not_cascade_for_relative_script(self):
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "gone")
            health = server.inspect_app_health({
                "command": "/bin/bash -- job.sh", "cwd": missing})
        self.assertEqual([item["kind"] for item in health["issues"]],
                         ["cwd-missing"])

    def test_complex_or_dynamic_command_is_unknown_and_not_blocked(self):
        for command in ("python3 job.py && echo done", "python3 '$JOB'",
                        "python3 'unterminated"):
            with self.subTest(command=command):
                health = server.inspect_app_health(
                    {"command": command, "cwd": None})
                self.assertEqual(health["status"], "unknown")
                self.assertFalse(health["blocking"])

    def test_python_module_and_inline_code_are_not_treated_as_files(self):
        for command in ("python3 -m http.server", "python3 -c 'print(1)'"):
            with self.subTest(command=command):
                health = server.inspect_app_health(
                    {"command": command, "cwd": None})
                self.assertFalse(health["blocking"])

    def test_missing_runtime_is_blocking(self):
        with mock.patch.object(server.shutil, "which", return_value=None):
            health = server.inspect_app_health(
                {"command": "definitely-not-installed --version", "cwd": None})
        self.assertEqual(health["issues"][0]["kind"], "runtime-missing")

    def test_direct_script_requires_execute_permission_but_bash_script_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "job.command")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("echo ok\n")
            os.chmod(path, 0o600)
            direct = server.inspect_app_health(
                {"command": shlex.quote(path), "cwd": td})
            wrapped = server.inspect_app_health(
                {"command": "/bin/bash -- " + shlex.quote(path), "cwd": td})
        self.assertEqual(direct["issues"][0]["kind"], "script-not-executable")
        self.assertFalse(wrapped["blocking"])

    def test_broken_script_symlink_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            link = os.path.join(td, "job.py")
            os.symlink(os.path.join(td, "missing.py"), link)
            health = server.inspect_app_health(
                {"command": server.command_for_script(link), "cwd": td})
        self.assertEqual(health["issues"][0]["kind"], "script-missing")

    def test_task_cancel_exit_code_survives_shell_wrapper(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            app = {"id": "deadbeef", "cwd": td,
                   "command": "python3 -c 'raise SystemExit(130)'"}
            ok, error, proc, _, _ = server.start_app(app)
            self.assertTrue(ok, error)
            self.assertEqual(proc.wait(timeout=3), 130)


class ProjectDetectionTests(unittest.TestCase):
    def test_package_json_uses_lockfile_runner_and_framework_port(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "package.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "scripts": {"build": "vite build", "dev": "vite --host", "preview": "vite preview"},
                    "devDependencies": {"vite": "latest"},
                }, f)
            with open(os.path.join(td, "pnpm-lock.yaml"), "w", encoding="utf-8") as f:
                f.write("lockfileVersion: '9.0'\n")

            result, error = server.detect_project(td)

        self.assertIsNone(error)
        self.assertEqual([item["command"] for item in result["candidates"]],
                         ["pnpm run dev", "pnpm run preview"])
        self.assertEqual([item["port"] for item in result["candidates"]],
                         [5173, 4173])
        self.assertIn("package.json", result["files"])
        self.assertIn("pnpm-lock.yaml", result["files"])

    def test_explicit_script_port_wins_over_framework_default(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "package.json"), "w", encoding="utf-8") as f:
                json.dump({"scripts": {"dev": "next dev --port 4321"},
                           "dependencies": {"next": "latest"}}, f)
            result, error = server.detect_project(td)

        self.assertIsNone(error)
        self.assertEqual(result["candidates"][0]["port"], 4321)

    def test_detects_positional_http_server_port_used_by_static_blogs(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "package.json"), "w", encoding="utf-8") as f:
                json.dump({"scripts": {"dev": "python3 -m http.server 4173"}}, f)
            result, error = server.detect_project(td)

        self.assertIsNone(error)
        self.assertEqual(result["candidates"][0]["port"], 4173)

    def test_detects_django_and_static_site_fallback(self):
        with tempfile.TemporaryDirectory() as django_dir, tempfile.TemporaryDirectory() as static_dir:
            with open(os.path.join(django_dir, "manage.py"), "w", encoding="utf-8") as f:
                f.write("#!/usr/bin/env python3\n")
            with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write("<!doctype html><title>Blog</title>")

            django, django_error = server.detect_project(django_dir)
            static, static_error = server.detect_project(static_dir)

        self.assertIsNone(django_error)
        self.assertEqual(django["candidates"][0]["command"], "python3 manage.py runserver")
        self.assertEqual(django["candidates"][0]["port"], 8000)
        self.assertIsNone(static_error)
        self.assertEqual(static["candidates"][0]["command"],
                         "python3 -m http.server 8000")

    def test_poetry_lock_uses_poetry_runner_for_django(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write(
                    "[tool.poetry]\n"
                    'name = "demo"\n'
                    'version = "0.1.0"\n'
                )
            with open(os.path.join(td, "poetry.lock"), "w", encoding="utf-8") as f:
                f.write("")
            with open(os.path.join(td, "manage.py"), "w", encoding="utf-8") as f:
                f.write("#!/usr/bin/env python3\n")

            result, error = server.detect_project(td)

        self.assertIsNone(error)
        self.assertEqual(
            result["candidates"][0]["command"],
            "poetry run python manage.py runserver",
        )
        self.assertIn("poetry.lock", result["files"])

    def test_poetry_lock_uses_poetry_runner_for_fastapi(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write(
                    "[tool.poetry.dependencies]\n"
                    'python = "^3.12"\n'
                    'fastapi = "*"\n'
                    'uvicorn = "*"\n'
                )
            with open(os.path.join(td, "poetry.lock"), "w", encoding="utf-8") as f:
                f.write("")
            with open(os.path.join(td, "main.py"), "w", encoding="utf-8") as f:
                f.write("from fastapi import FastAPI\napp = FastAPI()\n")

            result, error = server.detect_project(td)

        self.assertIsNone(error)
        self.assertEqual(
            result["candidates"][0]["command"],
            "poetry run uvicorn main:app --reload",
        )
        self.assertEqual(result["candidates"][0]["port"], 8000)
        self.assertIn("poetry.lock", result["files"])

    def test_uv_lock_keeps_priority_over_poetry_lock(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write(
                    "[project]\n"
                    'name = "demo"\n'
                    'dependencies = ["fastapi", "uvicorn"]\n'
                )
            with open(os.path.join(td, "uv.lock"), "w", encoding="utf-8") as f:
                f.write("")
            with open(os.path.join(td, "poetry.lock"), "w", encoding="utf-8") as f:
                f.write("")
            with open(os.path.join(td, "main.py"), "w", encoding="utf-8") as f:
                f.write("from fastapi import FastAPI\napp = FastAPI()\n")

            result, error = server.detect_project(td)

        self.assertIsNone(error)
        self.assertEqual(
            result["candidates"][0]["command"],
            "uv run uvicorn main:app --reload",
        )
        self.assertIn("uv.lock", result["files"])
        self.assertNotIn("poetry.lock", result["files"])

    def test_invalid_folder_returns_a_clear_error(self):
        result, error = server.detect_project("/path/that/does/not/exist")
        self.assertIsNone(result)
        self.assertIn("不存在", error)

    def test_framework_names_in_plain_strings_do_not_trigger_python_detection(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "server.py"), "w", encoding="utf-8") as f:
                f.write('HELP = "try import streamlit or FastAPI( or Flask("\n')
            result, error = server.detect_project(td)

        self.assertIsNone(error)
        self.assertEqual(result["candidates"], [])

    def test_hexo_structure_needs_no_package_script(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "_config.yml"), "w", encoding="utf-8") as f:
                f.write("title: Blog\n")
            os.mkdir(os.path.join(td, "source"))
            os.mkdir(os.path.join(td, "themes"))

            result, error = server.detect_project(td)

        self.assertIsNone(error)
        self.assertEqual(result["candidates"], [
            {"command": "hexo s", "label": "Hexo 本地服务",
             "source": "Hexo 项目结构", "port": 4000,
             "kind": "service", "detail": "等同于 hexo server"},
            {"command": "hexo cl", "label": "Hexo 清除缓存",
             "source": "Hexo 项目结构", "port": None,
             "kind": "task", "detail": "清除缓存和已生成文件，不启动服务"},
        ])

    def test_hexo_server_script_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "package.json"), "w", encoding="utf-8") as f:
                json.dump({"scripts": {"server": "hexo server"},
                           "dependencies": {"hexo": "latest"}}, f)

            result, error = server.detect_project(td)

        self.assertIsNone(error)
        self.assertEqual([item["command"] for item in result["candidates"]],
                         ["hexo s", "hexo cl"])


class ConfigTests(unittest.TestCase):
    def test_new_config_does_not_mutate_class_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            original = json.loads(json.dumps(server.Config.DEFAULT))
            cfg = server.Config(os.path.join(td, "config.json"))
            cfg.update(lambda data: data["watchedKeywords"].append("node"))
            self.assertEqual(server.Config.DEFAULT, original)

    def test_atomic_write_keeps_previous_good_version_as_backup(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({**server.Config.DEFAULT,
                           "watchedKeywords": ["node"]}, f)
            cfg = server.Config(path)
            cfg.update(lambda data: data["watchedKeywords"].append("ffmpeg"))
            with open(path, "r", encoding="utf-8") as f:
                current = json.load(f)
            with open(path + ".bak", "r", encoding="utf-8") as f:
                backup = json.load(f)
            self.assertEqual(current["watchedKeywords"], ["node", "ffmpeg"])
            self.assertEqual(backup["watchedKeywords"], ["node"])
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            self.assertEqual(oct(os.stat(path + ".bak").st_mode & 0o777),
                             "0o600")

    def test_load_falls_back_to_backup(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{")
            with open(path + ".bak", "w", encoding="utf-8") as f:
                json.dump({**server.Config.DEFAULT, "watchedKeywords": ["node"]}, f)
            cfg = server.Config(path)
            self.assertEqual(cfg.snapshot()["watchedKeywords"], ["node"])
            with open(path, "r", encoding="utf-8") as f:
                restored = json.load(f)
            self.assertEqual(restored["watchedKeywords"], ["node"])
            self.assertTrue(cfg.health_info()["recoveredFromBackup"])

    def test_legacy_schema_is_migrated_once_and_old_config_is_backup(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            legacy = {key: value for key, value in server.Config.DEFAULT.items()
                      if key != "schemaVersion"}
            legacy["watchedKeywords"] = ["ffmpeg"]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(legacy, f)

            cfg = server.Config(path)
            self.assertEqual(cfg.snapshot()["schemaVersion"],
                             server.CURRENT_SCHEMA_VERSION)
            self.assertEqual(cfg.health_info()["migratedFromSchema"], 0)
            with open(path, "r", encoding="utf-8") as f:
                migrated = json.load(f)
            with open(path + ".bak", "r", encoding="utf-8") as f:
                previous = json.load(f)
            self.assertEqual(migrated["schemaVersion"], 1)
            self.assertNotIn("schemaVersion", previous)

            # 第二次读取已是当前 schema，不再改写备份。
            with open(path + ".bak", "rb") as f:
                previous_bytes = f.read()
            cfg2 = server.Config(path)
            self.assertIsNone(cfg2.health_info()["migratedFromSchema"])
            with open(path + ".bak", "rb") as f:
                self.assertEqual(f.read(), previous_bytes)

    def test_future_schema_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            future = {**server.Config.DEFAULT,
                      "schemaVersion": server.CURRENT_SCHEMA_VERSION + 1,
                      "watchedKeywords": ["future-data"]}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(future, f)
            previous_backup = {**server.Config.DEFAULT,
                               "watchedKeywords": ["older-backup"]}
            with open(path + ".bak", "w", encoding="utf-8") as f:
                json.dump(previous_backup, f)
            cfg = server.Config(path)

            self.assertFalse(cfg.health_info()["writable"])
            with self.assertRaises(OSError):
                cfg.update(lambda data: data["watchedKeywords"].append("x"))
            with open(path, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), future)
            with open(path + ".bak", "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), previous_backup)


class RuntimeStorageTests(unittest.TestCase):
    def test_runtime_override_requires_a_dedicated_absolute_directory(self):
        with mock.patch.dict(os.environ, {"TEST_CONSOLE_DIR": ""}):
            with self.assertRaises(RuntimeError):
                server.resolve_runtime_dir("TEST_CONSOLE_DIR", "/tmp/default")
        with mock.patch.dict(os.environ, {"TEST_CONSOLE_DIR": "relative"}):
            with self.assertRaises(RuntimeError):
                server.resolve_runtime_dir("TEST_CONSOLE_DIR", "/tmp/default")
        with mock.patch.dict(os.environ,
                             {"TEST_CONSOLE_DIR": os.path.expanduser("~")}):
            with self.assertRaises(RuntimeError):
                server.resolve_runtime_dir("TEST_CONSOLE_DIR", "/tmp/default")

    def test_first_run_copies_legacy_data_privately_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as td:
            legacy = os.path.join(td, "project-data")
            target = os.path.join(td, "Application Support", "总控台")
            logs = os.path.join(td, "Logs", "总控台")
            os.makedirs(os.path.join(legacy, "icons"))
            os.makedirs(os.path.join(legacy, "logs"))
            with open(os.path.join(legacy, "config.json"), "w",
                      encoding="utf-8") as f:
                json.dump({**server.Config.DEFAULT,
                           "watchedKeywords": ["legacy"]}, f)
            with open(os.path.join(legacy, "icons", "deadbeef.png"), "wb") as f:
                f.write(b"icon")
            with open(os.path.join(legacy, "logs", "deadbeef.log"), "wb") as f:
                f.write(b"log")

            result = server.migrate_legacy_runtime_data(
                target, logs, legacy, False, False)

            self.assertEqual(result,
                             {"dataMigrated": True, "logsMigrated": True})
            self.assertTrue(os.path.isfile(os.path.join(target, "config.json")))
            with open(os.path.join(target, "icons", "deadbeef.png"), "rb") as f:
                self.assertEqual(f.read(), b"icon")
            with open(os.path.join(logs, "deadbeef.log"), "rb") as f:
                self.assertEqual(f.read(), b"log")
            self.assertTrue(os.path.isfile(os.path.join(legacy, "config.json")))
            self.assertEqual(oct(os.stat(target).st_mode & 0o777), "0o700")
            self.assertEqual(
                oct(os.stat(os.path.join(target, "config.json")).st_mode & 0o777),
                "0o600")

            # 已存在的目标绝不被旧项目目录二次覆盖。
            with open(os.path.join(legacy, "config.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"changed": True}, f)
            again = server.migrate_legacy_runtime_data(
                target, logs, legacy, False, False)
            self.assertEqual(again,
                             {"dataMigrated": False, "logsMigrated": False})
            with open(os.path.join(target, "config.json"),
                      encoding="utf-8") as f:
                self.assertNotIn("changed", json.load(f))

    def test_explicit_overrides_never_trigger_legacy_migration(self):
        with tempfile.TemporaryDirectory() as td:
            legacy = os.path.join(td, "legacy")
            target = os.path.join(td, "custom-data")
            logs = os.path.join(td, "custom-logs")
            os.makedirs(os.path.join(legacy, "logs"))
            with open(os.path.join(legacy, "config.json"), "w") as f:
                f.write("{}")
            with open(os.path.join(legacy, "logs", "console.log"), "w") as f:
                f.write("log")

            result = server.migrate_legacy_runtime_data(
                target, logs, legacy, True, True)
            self.assertEqual(result,
                             {"dataMigrated": False, "logsMigrated": False})
            self.assertFalse(os.path.exists(target))
            self.assertFalse(os.path.exists(logs))

    def test_prepare_storage_cli_exits_without_starting_server(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "custom-data")
            logs = os.path.join(td, "custom-logs")
            env = dict(os.environ,
                       CONSOLE_DATA_DIR=target,
                       CONSOLE_LOG_DIR=logs)
            result = subprocess.run(
                [sys.executable, server.__file__, "--prepare-storage"],
                cwd=td, env=env, capture_output=True, text=True, timeout=5)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.isdir(target))
            self.assertTrue(os.path.isdir(os.path.join(target, "icons")))
            self.assertTrue(os.path.isdir(logs))
            # 显式 override 只准备私有目录，不复制项目内旧配置。
            self.assertFalse(os.path.exists(os.path.join(target, "config.json")))
            self.assertNotIn("总控台已启动", result.stdout + result.stderr)

    def test_prepare_storage_cli_fails_nonzero_when_directory_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            blocker = os.path.join(td, "not-a-directory")
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("block")
            env = dict(os.environ,
                       CONSOLE_DATA_DIR=os.path.join(blocker, "data"),
                       CONSOLE_LOG_DIR=os.path.join(td, "logs"))
            result = subprocess.run(
                [sys.executable, server.__file__, "--prepare-storage"],
                cwd=td, env=env, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("总控台已启动", result.stdout + result.stderr)

    def test_app_launcher_redirects_output_only_after_storage_is_ready(self):
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "custom-data")
            logs = os.path.join(td, "custom-logs")
            env = dict(os.environ,
                       CONSOLE_DATA_DIR=target,
                       CONSOLE_LOG_DIR=logs)
            script = (
                "import server; "
                "server.prepare_runtime_storage(); "
                "server.redirect_console_output(); "
                "print('launcher-log-ready', flush=True)"
            )
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=server.BASE_DIR,
                env=env, capture_output=True, text=True, timeout=5)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            log_path = os.path.join(logs, "console.log")
            with open(log_path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "launcher-log-ready\n")
            self.assertEqual(os.stat(log_path).st_mode & 0o777, 0o600)


class ProcessIdentityTests(unittest.TestCase):
    def test_random_marker_is_required_for_whole_process_group(self):
        app = {"id": "a", "lastPid": 42, "lastPgid": 42, "runToken": "right"}
        groups = {42: [42, 43]}
        snap = {
            42: {"uid": server.SELF_UID, "args": "bash console-run:right"},
            43: {"uid": server.SELF_UID, "args": "python service.py"},
        }
        with mock.patch.object(server, "ps_snapshot", return_value=snap):
            index, _, _ = server.managed_process_index([app], groups)
            self.assertEqual(index["a"], [42, 43])
            stale = dict(app, runToken="wrong")
            index, _, _ = server.managed_process_index([stale], groups)
            self.assertEqual(index["a"], [])

    def test_real_started_process_is_identified_and_stoppable(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            app = {"id": "deadbeef", "command": "sleep 20", "cwd": td}
            ok, error, proc, pgid, token = server.start_app(app)
            self.assertTrue(ok, error)
            tracked = dict(app, lastPid=proc.pid, lastPgid=pgid, runToken=token)
            try:
                time.sleep(0.15)
                self.assertIn(proc.pid, server.managed_pids(tracked))
                self.assertEqual(
                    server.managed_pids(dict(tracked, runToken="wrong")), [])
                target, error = server.resolve_app_stop_target(tracked)
                self.assertIsNotNone(target, error)
                stopped, error = server.signal_app_stop(target)
                self.assertTrue(stopped, error)
                proc.wait(timeout=3)
            finally:
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    proc.wait(timeout=3)

    def test_verified_legacy_process_can_be_stopped_without_port_kill(self):
        app = {"id": "legacy", "lastPid": 999, "lastPgid": None,
               "runToken": None, "port": 8080, "cwd": "/tmp/project"}
        with mock.patch.object(server, "managed_pids", return_value=[]), \
                mock.patch.object(server, "legacy_managed_pid", return_value=999), \
                mock.patch.object(server.os, "kill") as stop:
            target, error = server.resolve_app_stop_target(
                app, {(999, 8080)})
            self.assertIsNone(error)
            stopped, error = server.signal_app_stop(target)
            self.assertTrue(stopped, error)
        stop.assert_called_once_with(999, signal.SIGTERM)

    def test_running_app_can_be_stopped_in_place_before_update(self):
        cfg = mock.Mock()
        app = {"id": "a", "runToken": "token"}
        with mock.patch.object(server, "app_alive_sign", return_value=True), \
                mock.patch.object(server, "stop_app_and_clear",
                                  return_value=(True, None)) as stop:
            ok, error, stopped = server.stop_app_for_update(cfg, app)

        self.assertTrue(ok, error)
        self.assertTrue(stopped)
        stop.assert_called_once_with(cfg, app, 5.0)

    def test_stopped_app_update_does_not_send_another_signal(self):
        cfg = mock.Mock()
        app = {"id": "a", "runToken": None}
        with mock.patch.object(server, "app_alive_sign", return_value=False), \
                mock.patch.object(server, "stop_app_and_clear") as stop:
            ok, error, stopped = server.stop_app_for_update(cfg, app)

        self.assertTrue(ok, error)
        self.assertFalse(stopped)
        stop.assert_not_called()

    def test_attach_claims_current_user_listener_via_legacy_identity(self):
        stored = {"apps": [{"id": "a", "port": 8080, "cwd": "/old",
                            "kind": "service"}]}
        cfg = mock.Mock()
        cfg.snapshot.return_value = stored
        cfg.update.side_effect = lambda op: op(stored)
        app = dict(stored["apps"][0])
        with mock.patch.object(server, "app_alive_sign", return_value=False), \
                mock.patch.object(server, "scan_listeners",
                                  return_value={(4242, 8080)}), \
                mock.patch.object(server, "ps_snapshot",
                                  return_value={4242: {"uid": server.SELF_UID}}), \
                mock.patch.object(server, "listener_app_owners",
                                  return_value={}), \
                mock.patch.object(server, "lsof_cwds",
                                  return_value={4242: "/new"}):
            ok, error, info = server.attach_app_process(cfg, "a", app, 4242)

        self.assertTrue(ok, error)
        target = stored["apps"][0]
        self.assertEqual(target["lastPid"], 4242)
        self.assertIsNone(target["lastPgid"])
        self.assertIsNone(target["runToken"])
        self.assertTrue(target["attached"])
        self.assertEqual(target["cwd"], "/new")
        self.assertTrue(info["cwdUpdated"])

    def test_attached_listener_survives_child_pid_rotation_by_unique_cwd(self):
        app = {"id": "a", "port": 3000, "cwd": "/project",
               "kind": "service", "lastPid": 4242, "attached": True}
        pid = server.legacy_managed_pid(
            app,
            listeners={(5252, 3000), (6262, 3000)},
            snap={
                5252: {"uid": server.SELF_UID},
                6262: {"uid": server.SELF_UID},
            },
            cwds={5252: "/project", 6262: "/other"},
        )
        self.assertEqual(pid, 5252)

    def test_attached_listener_requires_a_unique_uid_cwd_match(self):
        app = {"id": "a", "port": 3000, "cwd": "/project",
               "kind": "service", "lastPid": 4242, "attached": True}
        common = {
            "listeners": {(5252, 3000), (6262, 3000)},
            "snap": {
                5252: {"uid": server.SELF_UID},
                6262: {"uid": server.SELF_UID},
            },
        }
        self.assertIsNone(server.legacy_managed_pid(
            app, **common, cwds={5252: "/other", 6262: "/elsewhere"}))
        self.assertIsNone(server.legacy_managed_pid(
            app, **common, cwds={5252: "/project", 6262: "/project"}))

    def test_attach_rejects_foreign_unrelated_or_running(self):
        cfg = mock.Mock()
        app = {"id": "a", "port": 8080, "kind": "service"}
        with mock.patch.object(server, "app_alive_sign", return_value=False), \
                mock.patch.object(server, "scan_listeners",
                                  return_value={(4242, 9999)}):
            ok, error, _ = server.attach_app_process(cfg, "a", app, 4242)
        self.assertFalse(ok)
        self.assertIn("并未监听", error)

        with mock.patch.object(server, "app_alive_sign", return_value=False), \
                mock.patch.object(server, "scan_listeners",
                                  return_value={(4242, 8080)}), \
                mock.patch.object(server, "ps_snapshot",
                                  return_value={4242: {"uid": server.SELF_UID + 1}}):
            ok, error, _ = server.attach_app_process(cfg, "a", app, 4242)
        self.assertFalse(ok)
        self.assertIn("不属于当前用户", error)

        with mock.patch.object(server, "app_alive_sign", return_value=True):
            ok, error, _ = server.attach_app_process(cfg, "a", app, 4242)
        self.assertFalse(ok)
        self.assertIn("已在运行", error)

        task = {"id": "a", "port": None, "kind": "task"}
        ok, error, _ = server.attach_app_process(cfg, "a", task, 4242)
        self.assertFalse(ok)
        self.assertIn("批处理任务", error)

    def test_task_exit_records_duration_and_unique_run_time(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            path = os.path.join(td, "config.json")
            app = {**server.Config.APP_DEFAULT, "id": "deadbeef",
                   "name": "任务", "kind": "task", "lastPid": 4321,
                   "lastPgid": 4321, "runToken": "token"}
            with open(path, "w", encoding="utf-8") as f:
                json.dump({**server.Config.DEFAULT, "apps": [app]}, f)
            cfg = server.Config(path)
            proc = mock.Mock(pid=4321)
            proc.wait.return_value = 0
            started_at = time.time() - 1.25

            thread = server.watch_app_exit(
                cfg, "deadbeef", proc, "token", started_at)
            thread.join(timeout=2)
            result = cfg.snapshot()["apps"][0]["lastExit"]

        self.assertEqual(result["code"], 0)
        self.assertEqual(result["status"], "succeeded")
        self.assertAlmostEqual(result["durationSec"], 1.25, delta=0.2)
        self.assertEqual(result["startedAt"], int(started_at * 1000))

    def test_task_exit_status_classifier_covers_cancel_and_failure(self):
        self.assertEqual(server.classify_task_exit(0), "succeeded")
        self.assertEqual(server.classify_task_exit(130), "canceled")
        self.assertEqual(server.classify_task_exit(1), "failed")
        self.assertEqual(server.classify_task_exit(-15), "failed")

    def test_old_task_exit_status_is_normalized_only_for_api_output(self):
        legacy = {"code": 0, "at": 123}
        app = {"kind": "task", "lastExit": legacy}
        public = server.public_last_exit(app)
        self.assertEqual(public["status"], "succeeded")
        self.assertNotIn("status", legacy)
        stopped = server.public_last_exit({
            "kind": "task",
            "lastExit": {"status": "canceled", "code": None, "at": 456},
        })
        self.assertEqual(stopped["status"], "stopped")

    def test_task_start_preserves_previous_completed_result(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            previous = {"code": 0, "at": 123, "durationSec": 0.4}
            task = {**server.Config.APP_DEFAULT, "id": "deadbeef",
                    "name": "任务", "kind": "task", "lastExit": previous}
            service = {**server.Config.APP_DEFAULT, "id": "feedface",
                       "name": "服务", "kind": "service", "lastExit": previous}
            with open(path, "w", encoding="utf-8") as f:
                json.dump({**server.Config.DEFAULT,
                           "apps": [task, service]}, f)
            cfg = server.Config(path)
            proc = mock.Mock(pid=4321)

            with mock.patch.object(server, "watch_app_exit"):
                self.assertTrue(server.persist_started_app(
                    cfg, "deadbeef", proc, 4321, "task-token"))
                self.assertTrue(server.persist_started_app(
                    cfg, "feedface", proc, 4321, "service-token"))
            apps = {app["id"]: app for app in cfg.snapshot()["apps"]}

        self.assertEqual(apps["deadbeef"]["lastExit"], previous)
        self.assertIsNone(apps["feedface"]["lastExit"])


class LaunchEnvironmentTests(unittest.TestCase):
    def test_headless_launch_path_includes_common_user_node_locations(self):
        with mock.patch.object(server.os.path, "expanduser", return_value="/Users/example"), \
                mock.patch.object(server.glob, "glob", side_effect=[
                    ["/Users/example/.nvm/versions/node/v22/bin"],
                    ["/Users/example/.fnm/node-versions/v20/installation/bin"],
                ]):
            env = server.build_launch_env("secret", {"PATH": "/usr/bin:/bin"})

        paths = env["PATH"].split(os.pathsep)
        self.assertIn("/Users/example/.local/bin", paths)
        self.assertIn("/usr/local/bin", paths)
        self.assertIn("/opt/homebrew/bin", paths)
        self.assertIn("/Users/example/.nvm/versions/node/v22/bin", paths)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(env[server.RUN_TOKEN_ENV], "secret")

    def test_immediate_failure_message_uses_last_log_line(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            with open(os.path.join(td, "deadbeef.log"), "w", encoding="utf-8") as f:
                f.write("===== 启动于 now =====\nenv: node: No such file or directory\n")
            message = server.startup_failure_message("deadbeef", 127)

        self.assertIn("exit 127", message)
        self.assertIn("node: No such file", message)


class StateTests(unittest.TestCase):
    def test_app_and_service_expose_ipv6_aware_open_host(self):
        app = {**server.Config.APP_DEFAULT, "id": "vite", "name": "公众号排版",
               "command": "npm run dev", "cwd": "/tmp/vite", "port": 5173}
        listeners = {(4242, 5173): {"::1"}}
        proc = {
            4242: {
                "uid": server.SELF_UID, "comm": "/usr/local/bin/node",
                "args": "node vite", "cpu": 0.1, "mem": 0.2, "etime": 12,
            },
        }
        with mock.patch.object(server, "scan_listeners", return_value=listeners), \
                mock.patch.object(server, "ps_snapshot", return_value=proc), \
                mock.patch.object(server, "lsof_cwds",
                                  return_value={4242: "/tmp/vite"}), \
                mock.patch.object(
                    server, "managed_process_index",
                    return_value=({"vite": [4242]}, proc, {})):
            service = server.build_services({"apps": [app]})[0][0]
            built_app = server.build_apps({"apps": [app]}, listeners)[0]

        self.assertEqual(service["openHost"], "localhost")
        self.assertEqual(built_app["openHosts"], {"5173": "localhost"})

    def test_service_listener_is_linked_by_managed_identity_not_configured_port(self):
        app = {**server.Config.APP_DEFAULT, "id": "old-card",
               "name": "旧项目", "command": "npm run dev",
               "cwd": "/tmp/old", "port": 3000}
        listener = {
            83182: {
                "uid": server.SELF_UID,
                "comm": "/usr/local/bin/next-server",
                "args": "next-server",
                "cpu": 0.1,
                "mem": 0.2,
                "etime": 42,
            },
        }
        common = [
            mock.patch.object(server, "scan_listeners",
                              return_value={(83182, 3000)}),
            mock.patch.object(server, "ps_snapshot", return_value=listener),
            mock.patch.object(server, "lsof_cwds",
                              return_value={83182: "/tmp/new-blog"}),
        ]
        with common[0], common[1], common[2], mock.patch.object(
                server, "managed_process_index",
                return_value=({"old-card": []}, {}, {})):
            row = server.build_services({"apps": [app]})[0][0]

        self.assertIsNone(row["appId"])
        self.assertIsNone(row["appName"])
        self.assertEqual(row["project"], "new-blog")
        self.assertEqual(row["instanceKey"], "83182:3000")

        with mock.patch.object(server, "scan_listeners",
                               return_value={(83182, 3000)}), \
                mock.patch.object(server, "ps_snapshot",
                                  return_value=listener), \
                mock.patch.object(server, "lsof_cwds",
                                  return_value={83182: "/tmp/old"}), \
                mock.patch.object(
                    server, "managed_process_index",
                    return_value=({"old-card": [83182]}, {}, {})):
            managed_row = server.build_services({"apps": [app]})[0][0]

        self.assertEqual(managed_row["appId"], "old-card")
        self.assertEqual(managed_row["appName"], "旧项目")

    def test_legacy_listener_is_recognized_only_with_full_identity_match(self):
        app = {**server.Config.APP_DEFAULT, "id": "legacy", "name": "Legacy",
               "command": "python3 app.py", "cwd": "/tmp/project",
               "port": 8080, "lastPid": 999}
        proc = {999: {"uid": server.SELF_UID, "comm": "/usr/bin/python3",
                      "args": "python3 app.py", "etime": 42}}
        with mock.patch.object(
                server, "managed_process_index", return_value=({"legacy": []}, {}, {})), \
                mock.patch.object(server, "ps_snapshot", return_value=proc), \
                mock.patch.object(server, "lsof_cwds", return_value={999: "/tmp/project"}):
            row = server.build_apps({"apps": [app]}, {(999, 8080)})[0]
        self.assertTrue(row["running"])
        self.assertTrue(row["listening"])
        self.assertTrue(row["legacyManaged"])
        self.assertFalse(row["portOccupied"])

        with mock.patch.object(server, "ps_snapshot", return_value=proc), \
                mock.patch.object(server, "lsof_cwds", return_value={999: "/tmp/other"}):
            self.assertIsNone(server.legacy_managed_pid(app, {(999, 8080)}))

    def test_foreign_listener_is_conflict_not_running(self):
        app = {**server.Config.APP_DEFAULT, "id": "a", "name": "A",
               "command": "x", "port": 8080}
        with mock.patch.object(
                server, "managed_process_index", return_value=({"a": []}, {}, {})), \
                mock.patch.object(server, "ps_snapshot", return_value={
                    999: {"uid": server.SELF_UID, "comm": "/usr/bin/python3",
                          "args": "python3 other.py", "etime": 42},
                }), \
                mock.patch.object(server, "lsof_cwds", return_value={999: "/tmp/other"}):
            row = server.build_apps({"apps": [app]}, {(999, 8080)})[0]
        self.assertFalse(row["running"])
        self.assertTrue(row["portOccupied"])
        self.assertEqual(row["portOccupiedPid"], 999)
        self.assertEqual(row["portOwner"]["name"], "python3")
        self.assertEqual(row["portOwner"]["cwd"], "/tmp/other")
        self.assertTrue(row["portOwner"]["currentUser"])

    def test_duplicate_configured_ports_are_allowed_until_runtime(self):
        a = {**server.Config.APP_DEFAULT, "id": "a", "name": "A",
             "command": "x", "port": 8080}
        b = {**server.Config.APP_DEFAULT, "id": "b", "name": "B",
             "command": "y", "port": 8080}
        with mock.patch.object(
                server, "managed_process_index",
                return_value=({"a": [], "b": []}, {}, {})):
            rows = server.build_apps({"apps": [a, b]}, set())
        self.assertTrue(all(not row["portConflict"] for row in rows))
        self.assertTrue(all(row["portConflictApps"] == [] for row in rows))
        self.assertTrue(all(not row["portOccupied"] for row in rows))

    def test_app_state_exposes_health_and_normalizes_legacy_task_result(self):
        task = {**server.Config.APP_DEFAULT, "id": "task", "name": "Task",
                "kind": "task", "command": "echo ok",
                "lastExit": {"code": 0, "at": 123}}
        with mock.patch.object(
                server, "managed_process_index",
                return_value=({"task": []}, {}, {})):
            row = server.build_apps({"apps": [task]}, set())[0]
        self.assertEqual(row["health"]["status"], "ok")
        self.assertFalse(row["health"]["blocking"])
        self.assertEqual(row["lastExit"]["status"], "succeeded")
        self.assertNotIn("status", task["lastExit"])

    def test_watched_processes_are_current_user_only(self):
        snap = {
            10: {"uid": server.SELF_UID, "comm": "ffmpeg",
                 "args": "ffmpeg -i render-worker.mov",
                 "cpu": 1.0, "mem": 2.0, "etime": 3},
            11: {"uid": server.SELF_UID + 1, "comm": "ffmpeg", "args": "ffmpeg -i b",
                 "cpu": 1.0, "mem": 2.0, "etime": 3},
        }
        with mock.patch.object(server, "ps_snapshot", return_value=snap):
            rows = server.build_watched(
                ["ffmpeg", "render-worker", "FFMPEG"])
        self.assertEqual([row["pid"] for row in rows], [10])
        self.assertEqual(rows[0]["keywords"], ["ffmpeg", "render-worker"])
        self.assertEqual(rows[0]["keyword"], "ffmpeg、render-worker")


class LogTests(unittest.TestCase):
    def test_rotation_and_tail_are_bounded(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(server, "LOGS_DIR", td):
            path = os.path.join(td, "a.log")
            with open(path, "wb") as f:
                f.write(b"one\ntwo\nthree\nfour\n")
            self.assertTrue(server.rotate_log_file(path, max_bytes=8, backups=2))
            with open(path, "ab") as f:
                f.write(b"five\nsix\n")
            self.assertEqual(server.read_log_tail("a", 3), "four\nfive\nsix")


class IconTests(unittest.TestCase):
    def test_all_allowed_icon_extensions_have_mime_types(self):
        for ext in server.ICON_EXTS:
            self.assertIn(ext, server.STATIC_TYPES)

    def test_favicon_urls_cannot_leave_the_managed_loopback_port(self):
        self.assertTrue(server.is_loopback_service_url(
            "http://127.0.0.1:4187/icon.png", 4187))
        self.assertTrue(server.is_loopback_service_url(
            "http://localhost:4187/icon.png", 4187))
        self.assertFalse(server.is_loopback_service_url(
            "https://127.0.0.1:4187/icon.png", 4187))
        self.assertFalse(server.is_loopback_service_url(
            "http://127.0.0.1:4188/icon.png", 4187))
        self.assertFalse(server.is_loopback_service_url(
            "http://example.com/icon.png", 4187))
        self.assertFalse(server.is_loopback_service_url(
            "http://127.0.0.1:4187@example.com/icon.png", 4187))

    def test_external_favicon_links_and_svg_payloads_are_rejected(self):
        png = b"\x89PNG\r\n\x1a\n" + b"payload"
        html = (b'<link rel="icon" href="https://example.com/track.svg">'
                b'<link rel="icon" href="/safe.png">')
        calls = []

        def fake_get(url, port, timeout=3, limit=262144):
            calls.append((url, port))
            if url.endswith("/"):
                return html, "text/html"
            if url.endswith("/safe.png"):
                return png, "image/png"
            return None, None

        with mock.patch.object(server, "http_get", side_effect=fake_get):
            data, ext = server.fetch_favicon(4187)

        self.assertEqual((data, ext), (png, "png"))
        self.assertNotIn(("https://example.com/track.svg", 4187), calls)
        self.assertIsNone(server.sniff_icon_bytes(
            b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
            "image/svg+xml"))


class ConsoleRestartTests(unittest.TestCase):
    def test_instance_discovery_is_limited_to_same_project(self):
        snap = {
            71001: {"uid": server.SELF_UID, "args": "python3 server.py",
                    "etime": 10},
            71002: {"uid": server.SELF_UID, "args": "python3 server.py",
                    "etime": 20},
            71003: {"uid": server.SELF_UID + 1, "args": "python3 server.py",
                    "etime": 30},
            71004: {"uid": server.SELF_UID, "args": "python3 server.py --launcher",
                    "etime": 40},
        }
        with mock.patch.object(server, "ps_snapshot", return_value=snap), \
                mock.patch.object(server, "lsof_cwds", return_value={
                    71001: server.BASE_DIR,
                    71002: "/tmp/different-project",
                    71004: server.BASE_DIR,
                }), \
                mock.patch.object(server, "scan_listeners", return_value={
                    (71001, 9600), (71004, 9601)}):
            found = server.find_console_instances()
        self.assertEqual([item["pid"] for item in found], [71001, 71004])
        self.assertEqual(found[0]["ports"], [9600])
        self.assertEqual(found[1]["ports"], [9601])

    def test_panel_restart_spawns_helper_before_shutdown(self):
        class FakeServer:
            def __init__(self):
                self.stopped = threading.Event()

            def shutdown(self):
                self.stopped.set()

        fake_server = FakeServer()
        fake_proc = mock.Mock(pid=72001)
        with mock.patch.object(server.subprocess, "Popen", return_value=fake_proc) as popen, \
                mock.patch.object(server.time, "sleep", return_value=None):
            helper_pid = server.schedule_console_restart(fake_server, 9603)
            self.assertTrue(fake_server.stopped.wait(1))
        self.assertEqual(helper_pid, 72001)
        command = popen.call_args.args[0]
        self.assertIn("--restart-helper", command)
        self.assertEqual(command[-1], "9603")

    def test_panel_stop_shuts_down_after_response_window(self):
        class FakeServer:
            def __init__(self):
                self.stopped = threading.Event()

            def shutdown(self):
                self.stopped.set()

        fake_server = FakeServer()
        with mock.patch.object(server.time, "sleep", return_value=None):
            server.schedule_console_stop(fake_server)
            self.assertTrue(fake_server.stopped.wait(1))


class DiagnoseTests(unittest.TestCase):
    def _run(self, app, log="", cfg_apps=None):
        cfg = {"apps": cfg_apps or [app]}
        with mock.patch.object(server, "read_log_tail", return_value=log):
            return server.diagnose_app(cfg, app)

    def test_missing_node_modules_suggests_lockfile_manager(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "package.json"), "w", encoding="utf-8") as f:
                f.write('{"scripts": {"dev": "vite"}}')
            with open(os.path.join(td, "pnpm-lock.yaml"), "w", encoding="utf-8") as f:
                f.write("lockfileVersion: '9.0'\n")
            app = {"id": "aabbccdd", "name": "x", "cwd": td,
                   "command": "pnpm run dev", "port": 5173,
                   "lastExit": {"code": 2}}
            r = self._run(app)
        issue = next(i for i in r["issues"] if i["kind"] == "deps-missing")
        self.assertIn("pnpm install", issue["fix"])

    def test_cannot_find_module_from_log(self):
        app = {"id": "aabbccdd", "cwd": None, "command": "hexo s",
               "port": 4000, "lastExit": {"code": 2}}
        r = self._run(app, log="ERROR Cannot find module 'hexo' from '/x'")
        self.assertTrue(any(i["kind"] == "deps-missing" for i in r["issues"]))

    def test_missing_script_lists_available_scripts(self):
        with tempfile.TemporaryDirectory() as td:
            os.mkdir(os.path.join(td, "node_modules"))
            with open(os.path.join(td, "package.json"), "w", encoding="utf-8") as f:
                json.dump({"scripts": {"dev": "next dev", "build": "next build"}}, f)
            app = {"id": "aabbccdd", "cwd": td, "command": "npm run buld",
                   "port": None, "lastExit": {"code": 1}}
            r = self._run(app, log='npm error Missing script: "buld"')
        issue = next(i for i in r["issues"] if i["kind"] == "npm-script")
        self.assertIn("dev", issue["detail"])

    def test_exit_127_reports_missing_runtime_before_log_fallback(self):
        app = {"id": "aabbccdd", "cwd": None, "command": "nooope",
               "port": None, "lastExit": {"code": 127}}
        r = self._run(app)
        self.assertTrue(any(i["kind"] == "runtime-missing" for i in r["issues"]))

    def test_duplicate_port_config_is_not_a_diagnostic_error(self):
        a1 = {"id": "aabbccdd", "name": "A", "cwd": None, "command": "x",
              "port": 8080, "lastExit": {"code": 1}}
        a2 = {"id": "eeff0011", "name": "B", "cwd": None, "command": "y", "port": 8080}
        r = self._run(a1, cfg_apps=[a1, a2])
        self.assertFalse(any(i["kind"] == "port-dup" for i in r["issues"]))

    def test_clean_log_reports_no_match(self):
        app = {"id": "aabbccdd", "cwd": None, "command": "echo ok",
               "port": None, "lastExit": {"code": 1}}
        r = self._run(app, log="some random output")
        self.assertEqual(r["issues"], [])
        self.assertIn("常见错误模式", r["summary"])

    def test_successful_task_is_not_diagnosed_as_quick_exit(self):
        app = {"id": "aabbccdd", "kind": "task", "cwd": None,
               "command": "echo ok", "port": None,
               "lastExit": {"code": 0, "status": "succeeded"}}
        r = self._run(app, log="ok")
        self.assertFalse(any(i["kind"] == "quick-exit" for i in r["issues"]))

    def test_static_health_issue_is_included_before_a_failed_run(self):
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "missing.py")
            app = {"id": "aabbccdd", "kind": "task", "cwd": td,
                   "command": server.command_for_script(missing),
                   "port": None, "lastExit": None}
            r = self._run(app)
        issue = next(i for i in r["issues"] if i["kind"] == "script-missing")
        self.assertEqual(issue["action"], "pick-script")


class ThemeTests(unittest.TestCase):
    def test_list_themes_reads_manifests(self):
        listed = server.list_themes()
        self.assertEqual([theme["id"] for theme in listed], ["ops"])
        themes = {t["id"]: t for t in listed}
        self.assertEqual(themes["ops"]["name"], "Ops 指挥台")
        self.assertTrue(themes["ops"]["colors"])

    def test_config_defaults_ui_theme(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = server.Config(os.path.join(td, "config.json"))
            self.assertEqual(cfg.snapshot()["uiTheme"], "ops")

    def test_config_preserves_ui_theme_and_scalars(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "config.json")
            cfg = server.Config(path)
            cfg.update(lambda d: d.__setitem__("uiTheme", "custom"))
            cfg2 = server.Config(path)
            snap = cfg2.snapshot()
            self.assertEqual(snap["uiTheme"], "custom")
            self.assertIsInstance(snap["apps"], list)


if __name__ == "__main__":
    unittest.main()
