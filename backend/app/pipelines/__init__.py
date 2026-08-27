"""流水线层：通用状态机引擎（engine）+ 模块步骤注册表（registry）。

对第二棒（各模块流水线实现者）暴露的核心对象：
- engine.StepDef / StepResult / InteractionRequest / Ctx；
- registry.register(module, build_steps) 注册工厂。
"""

from .engine import Ctx, InteractionRequest, StepDef, StepResult  # noqa: F401
