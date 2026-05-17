import os
import time
import threading
from datetime import datetime

from flask import Blueprint, request, Response, jsonify

from ..models import load_tasks
from ..state import RUNNING
from ..task_runner import run_task_now, stop_task_now, is_running
from ..utils import h, now_str, safe_name
from ..ui.layout import layout

bp = Blueprint("exec", __name__)

_PROCESS_STATS = {}


def get_process_cpu_mem(pid):
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        
        vmrss = 0
        for line in content.split("\n"):
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    vmrss = int(parts[1]) * 1024
                break
    except Exception:
        vmrss = 0

    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="ignore") as f:
            stat = f.read()
        
        end = stat.rfind(")")
        if end < 0:
            cpu_percent = 0.0
        else:
            after = stat[end + 2:].split()
            utime = int(after[11])
            stime = int(after[12])
            
            key = (pid, "utime", "stime")
            last = _PROCESS_STATS.get(key, (0, 0, time.time()))
            
            delta = utime + stime - last[0] - last[1]
            elapsed = time.time() - last[2]
            
            if elapsed > 0 and _PROCESS_STATS.get((pid, "total")):
                cpu_delta = _PROCESS_STATS[(pid, "total")]
                cpu_percent = (delta / cpu_delta) * 100 if cpu_delta > 0 else 0.0
            else:
                cpu_percent = 0.0
            
            _PROCESS_STATS[key] = (utime, stime, time.time())
    except Exception:
        cpu_percent = 0.0

    return cpu_percent, vmrss


def update_process_stats():
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="ignore") as f:
            line = f.readline()
        
        parts = line.strip().split()
        if parts and parts[0] == "cpu":
            total = sum(int(x) for x in parts[1:])
            _PROCESS_STATS[("total",)] = total
    except Exception:
        pass


def fmt_bytes(n):
    try:
        n = float(n)
    except Exception:
        return "-"
    
    units = ["B", "KB", "MB", "GB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.1f} {units[i]}"


def running_tasks_info():
    tasks = load_tasks()
    running_list = []
    
    for task_id, info in RUNNING.items():
        status = info.get("status", "unknown")
        proc = info.get("process")
        pid = info.get("pid", "-")
        
        cpu = 0.0
        mem = 0
        
        if proc and pid != "-":
            try:
                pid_int = int(pid) if isinstance(pid, str) and pid.isdigit() else None
                if pid_int:
                    cpu, mem = get_process_cpu_mem(pid_int)
            except Exception:
                pass
        
        start_time = info.get("start_time", 0)
        elapsed = time.time() - start_time if start_time else 0
        
        elapsed_str = ""
        if elapsed >= 3600:
            elapsed_str = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"
        elif elapsed >= 60:
            elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
        else:
            elapsed_str = f"{int(elapsed)}s"
        
        task_name = ""
        for t in tasks:
            if t.get("id") == task_id:
                task_name = t.get("name") or t.get("command") or task_id
                break
        
        running_list.append({
            "task_id": task_id,
            "name": task_name,
            "status": status,
            "pid": pid,
            "cpu": f"{cpu:.1f}%",
            "mem": fmt_bytes(mem),
            "elapsed": elapsed_str,
            "start_time": datetime.fromtimestamp(start_time).strftime("%H:%M:%S") if start_time else "-",
            "log_file": info.get("log_file", ""),
        })
    
    return running_list


@bp.route("/exec")
def exec_panel():
    tasks = load_tasks()
    
    task_options = ""
    for t in tasks:
        tid = h(t.get("id", ""))
        tname = h(t.get("name") or t.get("command", "")[:40])
        task_options += f'<option value="{tid}">{tname}</option>'
    
    body = f"""
<div class="card">
    <div class="card-title">🚀 执行面板</div>
    <div class="help">实时查看运行中的任务进程，支持 CPU / 内存监控。</div>
</div>

<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px;">
        <div class="card-title" style="margin:0;">实时进程</div>
        <div class="action-row">
            <button class="btn btn-primary" onclick="refreshExecPanel()">🔄 刷新</button>
        </div>
    </div>
    <div id="execRunningList">
        <div style="text-align:center;color:#6b7280;padding:20px;">加载中...</div>
    </div>
</div>

<div class="card">
    <div class="card-title">⚡ 快速执行</div>
    <div class="help">选择一个任务立即执行。已在运行的任务不能重复执行。</div>
    <br>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
        <select id="quickTaskSelect" style="flex:1;min-width:200px;">
            <option value="">-- 选择任务 --</option>
            {task_options}
        </select>
        <button class="btn btn-primary" onclick="quickRun()">▶ 运行</button>
    </div>
</div>

<script>
let execRefreshTimer = null;

function refreshExecPanel() {{
    fetch("/api/exec/running", {{
        headers: {{"X-Requested-With": "FLS-Ajax"}},
        credentials: "same-origin"
    }})
    .then(r => r.json())
    .then(data => {{
        renderRunningList(data.running || []);
    }})
    .catch(err => {{
        document.getElementById("execRunningList").innerHTML = 
            '<div style="color:#dc2626;padding:12px;">加载失败: ' + err + '</div>';
    }});
}}

function renderRunningList(list) {{
    const container = document.getElementById("execRunningList");
    
    if (!list || list.length === 0) {{
        container.innerHTML = '<div class="log-empty-card">暂无运行中的任务</div>';
        return;
    }}
    
    let html = '<div class="table-wrap"><table><thead><tr>' +
        '<th>任务名</th>' +
        '<th>状态</th>' +
        '<th>PID</th>' +
        '<th>CPU</th>' +
        '<th>内存</th>' +
        '<th>运行时长</th>' +
        '<th>开始时间</th>' +
        '<th>操作</th>' +
        '</tr></thead><tbody>';
    
    list.forEach(item => {{
        const statusClass = item.status === 'running' ? 'green' : 
                           item.status === 'starting' ? 'blue' : 'orange';
        const statusText = item.status === 'running' ? '运行中' :
                          item.status === 'starting' ? '启动中' : '延迟中';
        
        html += '<tr>' +
            '<td data-label="任务名"><b>' + escapeHtml(item.name || item.task_id) + '</b></td>' +
            '<td data-label="状态"><span class="badge ' + statusClass + '">' + statusText + '</span></td>' +
            '<td data-label="PID">' + escapeHtml(item.pid || "-") + '</td>' +
            '<td data-label="CPU" style="color:#dc2626;font-weight:700;">' + escapeHtml(item.cpu || "0.0%") + '</td>' +
            '<td data-label="内存" style="color:#2563eb;font-weight:700;">' + escapeHtml(item.mem || "0 B") + '</td>' +
            '<td data-label="运行时长">' + escapeHtml(item.elapsed || "0s") + '</td>' +
            '<td data-label="开始时间">' + escapeHtml(item.start_time || "-") + '</td>' +
            '<td data-label="操作">' +
            '<a class="btn btn-gray" href="/log/' + escapeHtml(item.task_id) + '">📋 日志</a> ' +
            '<a class="btn btn-red" href="/stop/' + escapeHtml(item.task_id) + '" onclick="return confirm(\\'确定结束该任务吗？\\')">⏹ 停止</a>' +
            '</td></tr>';
    }});
    
    html += '</tbody></table></div>';
    container.innerHTML = html;
}}

function escapeHtml(s) {{
    return String(s || "").replace(/[&<>"']/g, function(c) {{
        return {{"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}}[c];
    }});
}}

function quickRun() {{
    const select = document.getElementById("quickTaskSelect");
    const taskId = select.value;
    
    if (!taskId) {{
        alert("请先选择一个任务");
        return;
    }}
    
    if (!confirm("确定要运行该任务吗？")) return;
    
    window.location.href = "/run/" + taskId;
}}

function startExecRefresh() {{
    refreshExecPanel();
    execRefreshTimer = setInterval(refreshExecPanel, 3000);
}}

if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", startExecRefresh);
}} else {{
    startExecRefresh();
}}
</script>
"""
    return layout("执行面板", "exec", body)


@bp.route("/api/exec/running")
def api_exec_running():
    update_process_stats()
    running = running_tasks_info()
    return jsonify({"running": running, "timestamp": time.time()})
