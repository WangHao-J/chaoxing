"""Per-account task reports: allowlisted fields, grouped causes and retained history."""
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib

from task_repository import list_pending

MODE_NAMES = {"all": "全部运行方式", "mixed": "答题和视频", "quizzes": "独立答题", "videos": "独立看视频"}
STATE_NAMES = {"historical_snapshot": "已有记录汇总", "completed": "本轮已结束", "needs_attention": "存在待处理项",
               "stopped": "已停止", "pending_review_finished": "所选测验处理结束", "unknown": "等待核对",
               "pending": "待处理", "not_executed": "尚未执行", "confirmed": "已确认完成"}


def clean(value):
    text = re.sub(r"https?://\S+", "[链接已隐藏]", str(value or ""))
    text = re.sub(r"(?i)\b(enc|dtoken|ktoken|token|cookie|authorization|api[_ -]?key)\s*[:=]\s*[^\s；;，,]+", r"\1=[已隐藏]", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)[:1000]


def category(reason):
    for label, words in (
        ("前置章节阻塞，尚未执行", ("前置章节", "未执行")),
        ("平台拒绝访问，需核对访问权限与环境", ("403",)),
        ("网络或服务暂时失败", ("Timeout", "超时", "RequestFailed", "ConnectionError", "RequestException", "网络", "连接", "AI 请求")),
        ("等待平台确认", ("未确认", "待确认", "结果待确认", "已提交")),
        ("本模式以外的任务", ("其他任务", "本模式", "本窗口没有")),
        ("需要登录或人工验证", ("登录", "验证码", "验证", "403", "429", "权限")),
        ("需要人工处理或程序适配", ("网页", "外链", "不兼容", "不支持", "字段", "题型", "直播", "人工处理")),
        ("当前尚未开放", ("尚未开放", "未开放", "截止", "次数耗尽")),
        ("用户跳过或尚未提交", ("跳过", "尚未提交", "未提交")),
        ("任务读取或记录异常", ("未读取", "未完整", "不完整", "记录无法", "记录需要", "编号缺失")),
    ):
        if any(word in reason for word in words):
            return label
    return "原因尚需核对"


def account_key(profile):
    key = str(getattr(profile, "id", Path(profile.directory).name))
    return key if re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", key) else hashlib.sha256(str(profile.directory).encode()).hexdigest()[:32]


def report_directory(root, profile):
    return Path(root) / "reports" / account_key(profile)


def build_report(root, profile, *, mode="all", state="historical_snapshot", observations=(), rounds=None):
    rows = []
    for pending in list_pending(profile):
        row = {key: clean(pending.get(key)) for key in
               ("course_id", "class_id", "chapter_id", "course_title", "chapter_title", "reason")}
        row["sources"] = [clean(source) for source in pending.get("sources", [])]
        row["state"] = "pending"
        if not row["reason"] and isinstance(pending.get("manual_tasks"), list):
            names = [clean(item.get("name") or item.get("kind")) if isinstance(item, dict) else clean(item)
                     for item in pending["manual_tasks"]]
            if any(names):
                row["reason"] = "需要人工处理的任务：" + "、".join(filter(None, names))
        row["reason"] = row["reason"] or "本地待处理记录未包含具体原因，尚未重新核对平台。"
        row["category"] = category(row["reason"])
        rows.append(row)
    known = {(row["course_id"], row["class_id"], row["chapter_id"]): row for row in rows}
    for observed in observations:
        row = {key: clean(observed.get(key)) for key in
               ("course_id", "class_id", "chapter_id", "course_title", "chapter_title", "reason", "state")}
        row["category"] = category(row["reason"])
        key = (row["course_id"], row["class_id"], row["chapter_id"])
        if row["state"] == "confirmed":
            # Other execution modes may still have stale checkpoint records.
            existing = known.get(key)
            if existing is not None:
                existing.update(row)
            continue
        if key in known:
            known[key].update(row)
        else:
            rows.append(row)
            known[key] = row
    rows = [row for row in rows if row["state"] != "confirmed"]
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return {"version": 1, "account_id": account_key(profile), "account_name": clean(profile.name),
            "generated_at": now.isoformat(timespec="seconds"), "mode": mode, "run_state": state,
            "evidence": "本地记录快照，尚未重新核对平台" if state == "historical_snapshot" else "本轮执行及平台查询结果；后续状态仍可能变化",
            "summary": dict(Counter(row["category"] for row in rows)), "tasks": rows,
            "rounds": rounds or {}}


def markdown(report):
    text = [f"# 账号运行报告：{report['account_name']}", "", f"生成时间：{report['generated_at']}",
            f"运行方式：{MODE_NAMES.get(report['mode'], '当前运行方式')}；状态：{STATE_NAMES.get(report['run_state'], '等待核对')}", f"依据：{report['evidence']}", "",
            "未完成、等待确认及尚未执行分别记录。报告不将失败或未执行判定为永久无法完成。", "",
            f"共 {len(report['tasks'])} 个需要核对的章节。", ""]
    rounds = report.get("rounds", {})
    if rounds:
        text += [f"执行轮次：{rounds['current']}；首轮后最多重跑 {rounds['restart_limit']} 轮。", ""]
        for item in rounds.get("history", []):
            text += [f"- 第 {item['round']} 轮：剩余 {item['remaining']} 章，任务成功 {item['successful_tasks']} 个，新提交 {item['new_submissions']} 份。"]
        text += [""]
    for label, count in report["summary"].items():
        text += [f"## {label}（{count}）", ""]
        for row in report["tasks"]:
            if row["category"] == label:
                text += [f"- **{row['course_title'] or '未记录课程名'} / {row['chapter_title'] or '未记录章节名'}**",
                         f"  原因：{row['reason']}", f"  记录状态：{STATE_NAMES.get(row['state'], '等待核对')}", ""]
    return "\n".join(text)


def plain_text(report):
    lines = ["账号运行报告：" + report["account_name"], "", "生成时间：" + report["generated_at"].replace("T", " "),
             "运行方式：" + MODE_NAMES.get(report["mode"], "当前运行方式"),
             "当前状态：" + STATE_NAMES.get(report["run_state"], "等待核对"),
             "依据：" + report["evidence"], "", f"共 {len(report['tasks'])} 个需要核对的章节。", ""]
    rounds = report.get("rounds", {})
    if rounds:
        lines += [f"执行轮次：{rounds['current']}；首轮后最多重跑 {rounds['restart_limit']} 轮。", ""]
    for label, count in report["summary"].items():
        lines += [f"【{label}】{count} 项", ""]
        for number, row in enumerate((row for row in report["tasks"] if row["category"] == label), 1):
            lines += [f"{number}. {row['course_title'] or '未记录课程名'} / {row['chapter_title'] or '未记录章节名'}",
                      "原因：" + row["reason"], "状态：" + STATE_NAMES.get(row["state"], "等待核对"), ""]
    return "\n".join(lines)


def write_report(root, profile, **options):
    report = build_report(root, profile, **options)
    directory = report_directory(root, profile)
    history = directory / "history"
    history.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"].replace(":", "").replace("+0800", "") + "-" + uuid.uuid4().hex[:8]
    content = json.dumps(report, ensure_ascii=False, indent=2)
    for suffix, value in (("json", content), ("md", markdown(report))):
        (history / f"{stamp}.{suffix}").write_text(value, encoding="utf-8")
        target = directory / ("latest." + suffix)
        temporary = directory / (".latest-" + uuid.uuid4().hex + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(target)
    return directory / "latest.md"
