"""Windows 每日六点任务计划；不在任务参数中保存任何登录凭据。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .collections import CollectionError
from .daily import BEIJING


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


class DailySchedule:
    def __init__(self, root, *, executable=None, project=None):
        self.root = Path(root).resolve()
        self.project = Path(project or Path.cwd()).resolve()
        executable = Path(executable or sys.executable).resolve()
        windowless = executable.with_name("pythonw.exe")
        self.executable = windowless if windowless.is_file() else executable
        key = hashlib.sha256(str(self.root).casefold().encode()).hexdigest()[:12]
        self.name = f"拾藏每日整理-{key}"
        self.marker = f"douyin-rag:daily:{key}:v1"

    def script(self, operation, moment=None):
        moment = (moment or datetime.now(BEIJING)).astimezone(BEIJING)
        boundary = moment.replace(hour=6, minute=0, second=0, microsecond=0)
        if boundary <= moment:
            boundary += timedelta(days=1)
        arguments = subprocess.list2cmdline(["-m", "douyin_rag.app", "--daily-run", "--state-dir", str(self.root)])
        setup = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$name = {literal(self.name)}
$marker = {literal(self.marker)}
$existing = Get-ScheduledTask -TaskName $name -TaskPath '\\' -ErrorAction SilentlyContinue
if ($existing -and $existing.Description -ne $marker) {{ throw '同名任务不属于拾藏，已停止操作。' }}
"""
        if operation == "install":
            setup += f"""
if (-not (Test-Path -LiteralPath {literal(self.executable)} -PathType Leaf)) {{ throw 'Python 运行文件不存在。' }}
$action = New-ScheduledTaskAction -Execute {literal(self.executable)} -Argument {literal(arguments)} -WorkingDirectory {literal(self.project)}
$trigger = New-ScheduledTaskTrigger -Daily -At '06:00'
$trigger.StartBoundary = {literal(boundary.isoformat())}
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 20) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 30)
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -TaskPath '\\' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $marker -Force | Out-Null
"""
        elif operation == "remove":
            setup += "if ($existing) { Unregister-ScheduledTask -TaskName $name -TaskPath '\\' -Confirm:$false }\n"
        elif operation != "status":
            raise CollectionError("不支持的任务计划操作。", "invalid_schedule_operation")
        setup += """
$task = Get-ScheduledTask -TaskName $name -TaskPath '\\' -ErrorAction SilentlyContinue
if (-not $task) {
    @{installed=$false; name=$name} | ConvertTo-Json -Compress
} else {
    $info = Get-ScheduledTaskInfo -InputObject $task
    @{installed=$true; name=$name; state=[string]$task.State;
      next_run=$info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss');
      last_result=$info.LastTaskResult; start_boundary=$task.Triggers[0].StartBoundary;
      wake_to_run=$task.Settings.WakeToRun; start_when_available=$task.Settings.StartWhenAvailable;
      multiple_instances=[string]$task.Settings.MultipleInstances;
      logon_type=[string]$task.Principal.LogonType;
      executable=$task.Actions[0].Execute; arguments=$task.Actions[0].Arguments} | ConvertTo-Json -Compress
}
"""
        return setup

    def manage(self, operation):
        if os.name != "nt":
            raise CollectionError("后台计划目前仅支持 Windows 任务计划。", "unsupported_platform")
        encoded = base64.b64encode(self.script(operation).encode("utf-16-le")).decode("ascii")
        try:
            reply = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                   capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            raise CollectionError("无法调用 Windows 任务计划，请检查当前用户权限。", "scheduler_error") from None
        if reply.returncode:
            # 不回显系统输出，避免意外传播任务或环境信息。
            raise CollectionError("Windows 任务计划操作失败，可能存在同名任务冲突或权限不足。", "scheduler_error")
        try:
            result = json.loads(reply.stdout.decode("utf-8-sig"))
            if not isinstance(result, dict) or not isinstance(result.get("installed"), bool):
                raise ValueError()
            return result
        except (UnicodeError, ValueError):
            raise CollectionError("无法解析 Windows 任务计划的返回状态。", "scheduler_error") from None
