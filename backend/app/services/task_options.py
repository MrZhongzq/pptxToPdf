"""从 Task 行上读回后处理选项与书签标题。

单独成模块，是为了让 pipeline（单次转换）与 shard_pipeline（分片合并）
共用同一份解析逻辑，而不是让后者去 import 前者的私有函数——那会在两个
本来平级的模块之间造出一条只为复用两行代码的依赖。
"""

import json
import logging

from app.models import Task
from app.schemas import ConversionOptions

logger = logging.getLogger(__name__)


def options_of(task: Task) -> ConversionOptions:
    """读回落库的后处理选项。

    落库的 JSON 坏掉时退回全默认（三项都不做）而不是让整次转换失败：
    用户要的是 PDF，一个畸形的选项字段不该把已经转好的东西作废。
    """
    if not task.options_json:
        return ConversionOptions()
    try:
        return ConversionOptions.model_validate_json(task.options_json)
    except ValueError:
        logger.warning("task %s 的 options_json 无法解析，按默认处理", task.task_id)
        return ConversionOptions()


def titles_of(task: Task) -> list[str]:
    if not task.outline_json:
        return []
    try:
        return json.loads(task.outline_json)
    except ValueError:
        logger.warning("task %s 的 outline_json 无法解析，跳过书签", task.task_id)
        return []
