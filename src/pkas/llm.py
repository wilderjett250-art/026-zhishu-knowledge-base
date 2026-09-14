import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pkas.config import Settings, get_settings
from pkas.repository import Repository

Transport = Callable[[dict[str, Any]], dict[str, Any]]
Validator = Callable[[dict[str, Any]], dict[str, Any]]

PRICE_PER_MILLION_USD = {
    "deepseek-v4-flash": {"cache_hit": 0.0028, "cache_miss": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"cache_hit": 0.003625, "cache_miss": 0.435, "output": 0.87},
}


class LLMError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        recorded: bool = False,
        usage: dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.recorded = recorded
        self.usage = usage or {}


class LLMUnavailableError(LLMError):
    def __init__(self) -> None:
        super().__init__(
            "DeepSeek API 尚未配置。请在本机环境中设置 PKAS_DEEPSEEK_API_KEY。",
            code="deepseek_not_configured",
        )


class LLMBudgetExceededError(LLMError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="daily_token_budget_exceeded")


@dataclass(frozen=True, slots=True)
class LLMResult:
    content: dict[str, Any]
    model: str
    usage: dict[str, int]
    estimated_cost_usd: float
    application_cache_hit: bool


class DeepSeekGateway:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        repository: Repository | None = None,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self.repository = repository or Repository()
        self.transport = transport or self._http_transport
        self.sleeper = sleeper

    def complete_json(
        self,
        *,
        task_type: str,
        system_prompt: str,
        payload: dict[str, Any],
        agent_run_id: str | None = None,
        complexity: str = "simple",
        prompt_version: str = "v1",
        max_tokens: int = 1200,
        use_cache: bool = True,
        validator: Validator | None = None,
        validation_hint: str = "",
        min_tokens: int | None = None,
    ) -> LLMResult:
        model, thinking_enabled = self._route(complexity)
        user_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        request_hash = self._request_hash(
            model=model,
            thinking_enabled=thinking_enabled,
            prompt_version=prompt_version,
            system_prompt=system_prompt,
            user_content=user_content,
        )
        if use_cache:
            cached = self.repository.get_llm_cache(request_hash)
            if cached:
                cached_content = cached["response"]
                if validator is not None:
                    try:
                        cached_content = validator(cached_content)
                    except Exception:
                        cached = None
                if cached is None:
                    cached_content = {}
            if cached:
                usage = self._empty_usage()
                self.repository.record_llm_call(
                    agent_run_id=agent_run_id,
                    task_type=task_type,
                    request_hash=request_hash,
                    model=model,
                    thinking_mode="enabled" if thinking_enabled else "disabled",
                    status="completed",
                    usage=usage,
                    application_cache_hit=True,
                )
                return LLMResult(
                    content=cached_content,
                    model=model,
                    usage=usage,
                    estimated_cost_usd=0.0,
                    application_cache_hit=True,
                )

        if not self.settings.deepseek_enabled:
            raise LLMUnavailableError()

        request_payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
        }
        last_error: LLMError | None = None
        for attempt in range(2):
            try:
                request_payload["max_tokens"] = self._bounded_max_tokens(
                    system_prompt,
                    json.dumps(
                        request_payload["messages"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    max_tokens,
                    min_tokens=min_tokens,
                )
                raw = self.transport(request_payload)
                content, usage = self._parse_response(raw)
                estimated_cost = self._estimate_cost(model, usage)
                if validator is not None:
                    try:
                        content = validator(content)
                    except Exception as exc:
                        validation_error = LLMError(
                            "DeepSeek JSON 未通过业务字段校验。",
                            code="semantic_validation_error",
                            retryable=attempt == 0,
                            recorded=True,
                        )
                        self.repository.record_llm_call(
                            agent_run_id=agent_run_id,
                            task_type=task_type,
                            request_hash=request_hash,
                            model=model,
                            thinking_mode="enabled" if thinking_enabled else "disabled",
                            status="failed",
                            usage=usage,
                            estimated_cost_usd=estimated_cost,
                            error_code=validation_error.code,
                        )
                        if attempt == 0:
                            request_payload["messages"] = [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_content},
                                {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        content,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    )[:4000],
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        "上一个 JSON 未通过字段校验，请只修正 JSON 结构和缺失字段。"
                                        f"校验错误：{str(exc)[:300]}。{validation_hint[:500]}"
                                    ),
                                },
                            ]
                            last_error = validation_error
                            continue
                        raise validation_error from exc
                self.repository.record_llm_call(
                    agent_run_id=agent_run_id,
                    task_type=task_type,
                    request_hash=request_hash,
                    model=model,
                    thinking_mode="enabled" if thinking_enabled else "disabled",
                    status="completed",
                    usage=usage,
                    estimated_cost_usd=estimated_cost,
                )
                if use_cache:
                    self.repository.put_llm_cache(
                        cache_key=request_hash,
                        model=model,
                        prompt_version=prompt_version,
                        response=content,
                        usage=usage,
                    )
                return LLMResult(
                    content=content,
                    model=model,
                    usage=usage,
                    estimated_cost_usd=estimated_cost,
                    application_cache_hit=False,
                )
            except LLMError as exc:
                last_error = exc
                if exc.usage and not exc.recorded:
                    self.repository.record_llm_call(
                        agent_run_id=agent_run_id,
                        task_type=task_type,
                        request_hash=request_hash,
                        model=model,
                        thinking_mode="enabled" if thinking_enabled else "disabled",
                        status="failed",
                        usage=exc.usage,
                        estimated_cost_usd=self._estimate_cost(model, exc.usage),
                        error_code=exc.code,
                    )
                    exc.recorded = True
                if attempt == 0 and exc.retryable:
                    self.sleeper(0.5)
                    continue
                if not exc.recorded:
                    self.repository.record_llm_call(
                        agent_run_id=agent_run_id,
                        task_type=task_type,
                        request_hash=request_hash,
                        model=model,
                        thinking_mode="enabled" if thinking_enabled else "disabled",
                        status="failed",
                        error_code=exc.code,
                    )
                raise
        assert last_error is not None
        raise last_error

    def _route(self, complexity: str) -> tuple[str, bool]:
        if complexity == "complex":
            return self.settings.deepseek_pro_model, True
        return self.settings.deepseek_flash_model, False

    def _bounded_max_tokens(
        self,
        system_prompt: str,
        user_content: str,
        requested_max_tokens: int,
        *,
        min_tokens: int | None = None,
    ) -> int:
        today = datetime.now(UTC).date().isoformat()
        usage = self.repository.llm_usage_since(today)
        used_input = int(usage["cache_hit_tokens"]) + int(usage["cache_miss_tokens"])
        estimated_input = max(1, (len(system_prompt) + len(user_content)) // 2)
        if used_input + estimated_input > self.settings.agent_daily_input_token_budget:
            raise LLMBudgetExceededError("已达到 Agent 每日输入 Token 预算，任务已暂停。")
        remaining_output = self.settings.agent_daily_output_token_budget - int(
            usage["output_tokens"]
        )
        required_minimum = max(
            self.settings.agent_min_output_tokens_per_call,
            min_tokens or 0,
        )
        if remaining_output < required_minimum:
            raise LLMBudgetExceededError("已达到 Agent 每日输出 Token 预算，任务已暂停。")
        return min(requested_max_tokens, remaining_output)

    def _http_transport(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = self.settings.deepseek_api_key
        if not key:
            raise LLMUnavailableError()
        endpoint = f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.settings.deepseek_timeout_seconds,
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            raise LLMError(
                f"DeepSeek API 返回 HTTP {exc.code}。",
                code=f"http_{exc.code}",
                retryable=retryable,
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(
                "DeepSeek API 网络连接失败。",
                code="network_error",
                retryable=True,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMError(
                "DeepSeek API 返回了无法解析的响应。",
                code="invalid_response",
                retryable=True,
            ) from exc

    @classmethod
    def _parse_response(cls, raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        raw_usage = raw.get("usage") or {}
        details = raw_usage.get("completion_tokens_details") or {}
        usage = {
            "prompt_cache_hit_tokens": int(raw_usage.get("prompt_cache_hit_tokens", 0)),
            "prompt_cache_miss_tokens": int(
                raw_usage.get("prompt_cache_miss_tokens", raw_usage.get("prompt_tokens", 0))
            ),
            "output_tokens": int(raw_usage.get("completion_tokens", 0)),
            "reasoning_tokens": int(details.get("reasoning_tokens", 0)),
        }
        try:
            message = raw["choices"][0]["message"]
            text = message.get("content") or ""
            if not text.strip():
                raise LLMError(
                    "DeepSeek JSON 输出为空。",
                    code="empty_content",
                    retryable=True,
                    usage=usage,
                )
            content = json.loads(text)
            if not isinstance(content, dict):
                raise LLMError(
                    "DeepSeek JSON 输出不是对象。",
                    code="invalid_json_shape",
                    retryable=True,
                    usage=usage,
                )
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError(
                "DeepSeek JSON 输出无法通过结构校验。",
                code="invalid_json",
                retryable=True,
                usage=usage,
            ) from exc
        return content, usage

    @staticmethod
    def _request_hash(
        *,
        model: str,
        thinking_enabled: bool,
        prompt_version: str,
        system_prompt: str,
        user_content: str,
    ) -> str:
        value = "\n".join(
            [model, str(thinking_enabled), prompt_version, system_prompt, user_content]
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _empty_usage() -> dict[str, int]:
        return {
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
        }

    @staticmethod
    def _estimate_cost(model: str, usage: dict[str, int]) -> float:
        prices = PRICE_PER_MILLION_USD.get(model, PRICE_PER_MILLION_USD["deepseek-v4-flash"])
        cost = (
            usage["prompt_cache_hit_tokens"] * prices["cache_hit"]
            + usage["prompt_cache_miss_tokens"] * prices["cache_miss"]
            + usage["output_tokens"] * prices["output"]
        ) / 1_000_000
        return round(cost, 8)
