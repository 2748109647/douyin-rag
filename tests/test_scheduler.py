import base64
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

from douyin_rag.app import main
from douyin_rag.collections import CollectionError
from douyin_rag.daily import BEIJING
from douyin_rag.scheduler import DailySchedule


class SchedulerTest(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.schedule = DailySchedule(Path(self.temp.name) / "数据'目录", project=self.temp.name, executable=sys.executable)

    def tearDown(self):
        self.temp.cleanup()

    def test_install_fixed_beijing_time_interactive_user_and_catchup(self):
        script = self.schedule.script("install", datetime(2026, 11, 12, 7, tzinfo=BEIJING))
        self.assertIn("2026-11-13T06:00:00+08:00", script)
        for value in ("-WakeToRun", "-StartWhenAvailable", "-MultipleInstances IgnoreNew", "-LogonType Interactive", "-RunLevel Limited", "--daily-run", "--state-dir"):
            self.assertIn(value, script)
        self.assertIn("数据''目录", script)
        self.assertNotIn("-Password", script)
        self.assertNotIn("DOUYIN_COOKIE", script)
        self.assertNotIn("api_key", script)
        before_six = self.schedule.script("install", datetime(2026, 11, 12, 5, tzinfo=BEIJING))
        self.assertIn("2026-11-12T06:00:00+08:00", before_six)

    def test_different_runtime_directories_have_different_task_names(self):
        other = DailySchedule(Path(self.temp.name) / "other")
        self.assertNotEqual(other.name, self.schedule.name)
        for operation in ("install", "remove", "status"):
            self.assertIn("$existing.Description -ne $marker", self.schedule.script(operation))
        self.assertNotIn("Unregister-ScheduledTask", self.schedule.script("install"))
        self.assertIn("Unregister-ScheduledTask", self.schedule.script("remove"))

    @skipUnless(os.name == "nt", "Windows 任务计划专用")
    def test_native_command_uses_unicode_encoded_script_and_validates_status(self):
        reply = Mock(returncode=0, stdout=json.dumps({"installed": True, "name": self.schedule.name}).encode())
        with patch("douyin_rag.scheduler.subprocess.run", return_value=reply) as run:
            result = self.schedule.manage("status")
        self.assertTrue(result["installed"])
        command = run.call_args.args[0]
        self.assertEqual(command[0], "powershell.exe")
        decoded = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertIn(self.schedule.name, decoded)
        self.assertIn("Get-ScheduledTaskInfo", decoded)
        self.assertEqual(run.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    @skipUnless(os.name == "nt", "Windows 任务计划专用")
    def test_command_errors_are_sanitized(self):
        with patch("douyin_rag.scheduler.subprocess.run", return_value=Mock(returncode=1, stderr=b"synthetic-private-value")):
            with self.assertRaises(CollectionError) as caught:
                self.schedule.manage("install")
            self.assertNotIn("synthetic-private-value", str(caught.exception))
        with patch("douyin_rag.scheduler.subprocess.run", side_effect=subprocess.TimeoutExpired("powershell", 60)):
            with self.assertRaises(CollectionError):
                self.schedule.manage("status")
        with patch("douyin_rag.scheduler.subprocess.run", return_value=Mock(returncode=0, stdout=b'{}')):
            with self.assertRaises(CollectionError):
                self.schedule.manage("status")

    @skipUnless(os.name == "nt", "Windows 任务计划专用")
    def test_powershell_parses_generated_install_status_and_remove_scripts(self):
        # 只解析脚本，不注册测试任务，也不访问真实抖音。
        for operation in ("install", "status", "remove"):
            source = base64.b64encode(self.schedule.script(operation).encode("utf-8")).decode()
            parser = "$source=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + source + "')); $tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors) | Out-Null; if($errors.Count){exit 1}"
            encoded = base64.b64encode(parser.encode("utf-16-le")).decode()
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_schedule_dispatches_without_starting_web_server(self):
        with patch.object(sys, "argv", ["douyin-rag", "--daily-schedule", "status", "--state-dir", self.temp.name]), \
                patch("douyin_rag.scheduler.DailySchedule.manage", return_value={"installed": False}) as manage, \
                patch("uvicorn.run") as server, patch("builtins.print"):
            main()
        manage.assert_called_once_with("status")
        server.assert_not_called()

    def test_cli_daily_result_exit_codes_and_pythonw_without_console(self):
        for status, expected in (("baseline", 0), ("completed", 0), ("partial", 1), ("failed", 1)):
            async def daily(root): return {"status": status}
            with patch.object(sys, "argv", ["douyin-rag", "--daily-run", "--state-dir", self.temp.name]), \
                    patch("douyin_rag.daily.run_daily", side_effect=daily), patch.object(sys, "stdout", None), \
                    patch("uvicorn.run") as server:
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(caught.exception.code, expected)
            server.assert_not_called()
