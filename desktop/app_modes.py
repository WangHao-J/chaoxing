"""One registry for the three public study modes; stored mode IDs stay stable."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    id: str
    title: str
    description: str
    scope: str


MODES = (
    Mode("quizzes", "独立答题", "集中处理章节测验\nAI 填写，核对后提交", "章节测验"),
    Mode("videos", "独立看视频", "专注音视频任务\n显示进度与预计用时", "音频 / 视频"),
    Mode("mixed", "答题和视频", "连贯处理两类任务\n从上次位置继续", "综合学习"),
)
MODE_IDS = tuple(mode.id for mode in MODES)
MODE_LABELS = {mode.id: mode.title for mode in MODES}
