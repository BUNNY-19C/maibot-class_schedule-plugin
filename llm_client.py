"""硅基流动 API 客户端：公式识别(VLM)、结构化整理(LLM)、向量召回(Embedding)、精排(Rerank)。

设计要点（都是关键决策，注释说明原因）：

- **地址校验与课表抓取同一套**：base 必须是公网 https，走 :func:`netutil.validate_url`，
  不因为"是我们主动调的 API"就开后门；
- **超时 + 指数退避重试**：默认 30 秒、失败重试 2 次(1s/2s)——网络抖动不该让
  整条笔记流水线失败，但也不该无限重试；
- **API Key 只从参数进来，绝不进日志/异常信息**：调用失败时只带响应体的摘要；
- **token 用量随每次调用返回**，由调用方累计——用量监控的数据源在这层拿；
- 本模块不认识"笔记"，只认识 HTTP：公式归一化、打标等业务都在上层。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Callable

from .netutil import UnsafeUrlError, validate_url

DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 2
BACKOFF_BASE_SECONDS = 1.0
#: 重试次数上限：再高只会放大故障时的等待时间
MAX_RETRIES_LIMIT = 5


class CloudError(RuntimeError):
    """云端调用失败（鉴权、限流、网络、格式都归到这里，携带可读原因）。"""


class NotConfiguredError(CloudError):
    """没配 API Key——调用方应当降级（跳过识别/只用全文检索）而不是报错给用户。"""


class SiliconFlowClient:
    """硅基流动兼容 OpenAI 格式的最小客户端。

    ``transport`` 参数是测试注入点：单元测试替换它来断言请求形状与重试行为，
    不碰真实网络。
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.siliconflow.cn/v1",
        timeout_seconds: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_RETRIES,
        transport: Callable[[str, bytes, dict[str, str]], bytes] | None = None,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._timeout = max(5, int(timeout_seconds))
        self._max_retries = max(0, min(MAX_RETRIES_LIMIT, int(max_retries)))
        self._transport = transport or self._transport_with_timeout

    # ── 基础 ──────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._base_url.startswith("https://"))

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 2000,
        temperature: float = 0.3,
        response_json: bool = False,
    ) -> dict[str, Any]:
        """文本对话（结构化整理、打标用）。返回 ``{text, prompt_tokens, completion_tokens}``。"""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if response_json:
            payload["response_format"] = {"type": "json_object"}
        return await self._completion(payload)

    async def vision(
        self,
        *,
        model: str,
        image_base64: str,
        prompt: str,
        image_suffix: str = "png",
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        """图片 + 提示词（公式识别用）。图片以 data URL 内嵌，不发本地路径。"""
        mime = "image/jpeg" if image_suffix in (".jpg", ".jpeg") else f"image/{image_suffix.lstrip('.')}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_base64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        return await self.chat(model=model, messages=messages, max_tokens=max_tokens)

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        """批量向量化。返回与输入同序的向量列表。"""
        data = await self._request("/embeddings", {"model": model, "input": texts})
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        vectors = [list(map(float, item.get("embedding") or [])) for item in items]
        if len(vectors) != len(texts):
            raise CloudError("embedding 返回数量与输入不一致")
        return vectors

    async def rerank(
        self, *, model: str, query: str, documents: list[str], top_n: int = 8
    ) -> list[dict[str, Any]]:
        """精排：返回 [{index, relevance_score}]，按相关度降序。"""
        data = await self._request(
            "/rerank",
            {"model": model, "query": query, "documents": documents, "top_n": top_n},
        )
        results = data.get("results") or []
        return [
            {
                "index": int(item.get("index", -1)),
                "relevance_score": float(item.get("relevance_score", 0.0)),
            }
            for item in results
        ]

    # ── 内部 ──────────────────────────────────────────────

    async def _completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = await self._request("/chat/completions", payload)
        choices = data.get("choices") or []
        if not choices:
            raise CloudError("模型返回为空 choices")
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "").strip()
        usage = data.get("usage") or {}
        return {
            "text": content,
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        }

    async def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise NotConfiguredError("未配置云端 API Key")
        url = self._base_url + path
        # 每次请求前重新校验：base_url 来自用户配置，与抓取课表同一纪律
        try:
            validate_url(url)
        except UnsafeUrlError as exc:
            raise CloudError(f"API 地址不被允许：{exc}") from exc
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            # 异常信息里永远不出现这个值；日志层也不打印 headers
            "Authorization": f"Bearer {self._api_key}",
        }
        last_error = ""
        for attempt in range(self._max_retries + 1):
            if attempt:
                # 指数退避：1s、2s、4s……上限三次足够扛抖动
                await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            try:
                raw = await asyncio.to_thread(self._transport, url, body, headers)
                return json.loads(raw.decode("utf-8"))
            except CloudError as exc:
                # 鉴权/参数类错误重试无意义，直接抛出
                if _is_permanent(str(exc)):
                    raise
                last_error = str(exc)
            except json.JSONDecodeError as exc:
                last_error = f"响应不是合法 JSON：{exc}"
            except OSError as exc:  # 网络错误（含 urllib.URLError/超时）
                last_error = f"网络请求失败: {exc}"
        raise CloudError(
            f"云端调用失败（已重试 {self._max_retries} 次）：{last_error or '未知原因'}"
        )

    def _transport_with_timeout(self, url: str, body: bytes, headers: dict[str, str]) -> bytes:
        """同步 HTTP 一次调用；错误转成 CloudError 且不携带请求头（Key 不外泄）。"""
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        opener = urllib.request.build_opener()
        try:
            with opener.open(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            snippet = ""
            try:
                snippet = exc.read().decode("utf-8", errors="replace")[:200]
            except OSError:
                pass
            raise CloudError(f"HTTP {exc.code} {snippet}") from exc
        except urllib.error.URLError as exc:
            raise CloudError(f"网络错误: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise CloudError(f"请求超时/中断: {exc}") from exc


def _is_permanent(reason: str) -> bool:
    """4xx 里除 429（限流值得重试）外都不重试：参数错再多次也不会变对。"""
    return any(f"HTTP {code}" in reason for code in (400, 401, 403, 404, 422))
