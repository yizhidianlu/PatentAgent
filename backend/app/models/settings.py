"""设置相关模型：LLM / Embedding / General，含 api_key 掩码逻辑。

约定（backend-architecture.md §2 要点）：
- API key 明文存 DB（本地单用户）；GET 返回掩码（如 sk-***ab12，保留尾 4 位）；
- 前端 PUT 提交空 api_key 表示"不修改"。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

# 数值能力位的合法区间（越界即 422；写死在契约层，前后端共用同一口径）
TEMPERATURE_MIN, TEMPERATURE_MAX = 0.0, 2.0
#: 上下文窗口 / 出参上限 / 向量维度都必须为正整数——0 或负数会让预算器与
#: OpenAI 兼容端点在**每一次**调用上失败（表现为「几个功能都不可用」），
#: 故在保存时就拦下，而不是等到跑流水线时才炸。
MIN_POSITIVE = 1

_SettingsT = TypeVar("_SettingsT", bound=BaseModel)


def load_tolerant(model: type[_SettingsT], stored: Mapping[str, Any] | None) -> _SettingsT:
    """把 DB 里存着的 settings JSON 读成契约模型，**读取永不 500**。

    范围校验是后加的，早于它写进库的值（如 context_window=0）依然躺在 settings 表里。
    若直接构造会抛 ValidationError，让 GET /settings/* 与整条流水线一起挂掉；
    故此处逐个丢弃校验不过的字段、回退到该字段的默认值，并记一条 warning。
    """
    data = dict(stored or {})
    for _ in range(len(data) + 1):
        try:
            return model(**data)
        except ValidationError as exc:
            dropped = {
                str(err["loc"][0])
                for err in exc.errors()
                if err.get("loc") and isinstance(err["loc"][0], str) and err["loc"][0] in data
            }
            if not dropped:
                break
            logger.warning(
                "%s 中的字段 %s 不合法，已回退到默认值（请到设置页重新保存）",
                model.__name__, "、".join(sorted(dropped)),
            )
            for key in dropped:
                data.pop(key, None)
    return model()


def mask_api_key(key: str) -> str:
    """把 API key 掩码为 `sk-***尾4位` 形式；空值原样返回空串。"""
    if not key:
        return ""
    prefix = "sk-" if key.startswith("sk-") else ""
    tail = key[-4:] if len(key) > 8 else ""
    return f"{prefix}***{tail}"


class LlmSettings(BaseModel):
    """LLM（OpenAI 兼容 chat）配置。"""

    model_config = ConfigDict(extra="ignore")

    base_url: str = ""                    # OpenAI 兼容 API base_url（空 = 官方默认）
    api_key: str = ""
    model: str = ""
    temperature: float = Field(default=0.2, ge=TEMPERATURE_MIN, le=TEMPERATURE_MAX)
    max_output_tokens: int = Field(default=4096, ge=MIN_POSITIVE)
    # 上下文窗口（能力位，供预算器使用）
    context_window: int = Field(default=131072, ge=MIN_POSITIVE)
    supports_json_mode: bool = True       # 是否支持 response_format=json_object（能力位）

    def masked(self) -> "LlmSettings":
        """返回 api_key 掩码后的副本（GET 响应用）。"""
        return self.model_copy(update={"api_key": mask_api_key(self.api_key)})


EmbeddingProvider = Literal["zhipu", "dashscope", "minimax", "local", "custom"]
ImageGenProvider = Literal["zhipu", "dashscope", "openai", "local", "custom"]


class EmbeddingSettings(BaseModel):
    """Embedding（OpenAI 兼容）配置。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False                 # 是否启用向量检索（关闭时案例库退化为关键词检索）
    provider: EmbeddingProvider = "custom"  # 仅作 UI 预设分组，实际以 base_url/model 为准
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    # 向量维度；变更需重建 oa_vec 虚拟表（0/负数会让 vec0 建表直接失败）
    dim: int = Field(default=1024, ge=MIN_POSITIVE)

    def masked(self) -> "EmbeddingSettings":
        """返回 api_key 掩码后的副本（GET 响应用）。"""
        return self.model_copy(update={"api_key": mask_api_key(self.api_key)})


class ImageGenSettings(BaseModel):
    """图像生成（OpenAI 兼容 images 接口）配置。

    用于实用新型 / 外观设计线稿与附图渲染的可选增强能力，默认关闭。
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    provider: ImageGenProvider = "custom"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    size: str = "1024x1024"               # 出图尺寸（透传给 images 接口）

    def masked(self) -> "ImageGenSettings":
        """返回 api_key 掩码后的副本（GET 响应用）。"""
        return self.model_copy(update={"api_key": mask_api_key(self.api_key)})


class GeneralSettings(BaseModel):
    """通用设置。"""

    model_config = ConfigDict(extra="ignore")

    output_dir: str = ""                                                      # 空 = 默认 data/outputs
    browser_channel: Literal["chrome", "msedge"] = "chrome"                   # Playwright 浏览器通道
    pdf_engine: Literal["auto", "word", "soffice", "pillow"] = "auto"         # PDF 导出引擎
    language: Literal["zh"] = "zh"


class LlmTestRequest(BaseModel):
    """POST /settings/llm/test 请求体：全部可选；缺省字段回落到已存配置。"""

    model_config = ConfigDict(extra="ignore")

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=TEMPERATURE_MIN, le=TEMPERATURE_MAX)


class ModelCapability(BaseModel):
    """从服务商 /v1/models 探测到的模型能力（探测不到的字段留空）。"""

    context_length: int | None = None
    max_output_tokens: int | None = None
    supports_reasoning: bool | None = None
    #: 上面两项若为服务端推荐而非厂商明示，置 true，前端据此措辞
    estimated: bool = False


class LlmTestResult(BaseModel):
    """LLM 连接测试结果。"""

    ok: bool
    model: str | None = None
    latency_ms: int | None = None
    error: str | None = None
    #: 顺带探测到的模型规格，供前端一键填入「上下文窗口 / 最大输出」
    capability: ModelCapability | None = None


class EmbeddingTestRequest(BaseModel):
    """POST /settings/embedding/test 请求体：全部可选；缺省字段回落到已存配置。"""

    model_config = ConfigDict(extra="ignore")

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class EmbeddingTestResult(BaseModel):
    """Embedding 连接测试结果。"""

    ok: bool
    model: str | None = None
    dim: int | None = None
    latency_ms: int | None = None
    error: str | None = None


class EmbeddingUpdateResult(BaseModel):
    """PUT /settings/embedding 响应：dim 变化时 need_rebuild=true 提示需重建向量库。"""

    settings: EmbeddingSettings
    need_rebuild: bool = False


class EmbeddingReindexResult(BaseModel):
    """POST /settings/embedding/reindex 响应：重建向量索引的排队结果。

    重建 = 丢弃 oa_vec 虚拟表 + 把全部案例标记为待重新向量化（embedded=0），
    真正的向量化在案例库检索/入库流程中按需补齐。
    """

    ok: bool
    queued: int = 0                       # 被标记为待重新向量化的案例数
    dim: int = 0
    vec_available: bool = False           # sqlite-vec 扩展是否可用
    message: str = ""


class ImageGenTestRequest(BaseModel):
    """POST /settings/image-gen/test 请求体：全部可选；缺省字段回落到已存配置。"""

    model_config = ConfigDict(extra="ignore")

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    size: str | None = None
    prompt: str | None = None


class ImageGenTestResult(BaseModel):
    """图像生成测试结果；image_data_url 可直接作为 <img src> 预览。"""

    ok: bool
    model: str | None = None
    latency_ms: int | None = None
    image_url: str | None = None          # 端点返回 URL 时
    image_data_url: str | None = None     # 端点返回 b64_json 时（data:image/png;base64,…）
    error: str | None = None
