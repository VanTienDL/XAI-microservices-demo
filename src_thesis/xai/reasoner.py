"""Gọi LLM, validate JSON bằng Pydantic, retry nếu sai, đếm token.

Bốn ràng buộc từ mục 5 và 7.5 KLTN.md, đều đã cài đặt ở đây:
  - LLM output LUÔN validate bằng Pydantic, sai schema thì retry, không tin mù.
  - Trần 3 vòng retry mỗi ca.
  - Cache kết quả theo mã băm của snapshot, khỏi trả tiền hai lần cho cùng triệu chứng.
  - Ghi lại token dùng mỗi ca để đưa vào phần chi phí của báo cáo.

VÌ SAO DÙNG THƯ VIỆN OPENAI CHO CẢ HAI NHÀ CUNG CẤP:
Groq phục vụ theo đúng giao thức của OpenAI, chỉ khác địa chỉ máy chủ. Nên một bộ
code chạy được cả hai, đổi nhà cung cấp chỉ là đổi một dòng cấu hình. Nhờ vậy mới so
sánh được model rẻ với model mạnh trên cùng bộ dữ liệu — đúng yêu cầu mục 7.5 KLTN.md
và là một kết quả phụ đáng đưa vào báo cáo.

Hai tầng model:
  groq   — chạy thí nghiệm hàng loạt, miễn phí
  openai — demo trước hội đồng và bảng so sánh cuối
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from openai import APIConnectionError, APIStatusError, OpenAI
from pydantic import ValidationError

from src_thesis.xai.prompt_templates import (
    SYSTEM_PROMPT,
    build_retry_prompt,
    build_user_prompt,
    schema_instruction,
)
from src_thesis.xai.schema import Explanation

MAX_RETRIES = 3
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "llm_cache"

# Đổi số này mỗi khi sửa prompt, để cache cũ không bị dùng nhầm cho prompt mới.
# v6: them `preconditions` vao schema va luat tham so phai la gia tri cu the. Bo
# qua ten v5 vi ten do da dung cho goi prompt bi loai o phase 3.
PROMPT_VERSION = "v6"


@dataclass(frozen=True)
class Provider:
    """Một nhà cung cấp LLM. Giá tính bằng USD trên một triệu token."""

    name: str
    base_url: str | None
    api_key_env: str
    default_model: str
    input_price: float
    output_price: float


PROVIDERS: dict[str, Provider] = {
    # Tầng chạy loạt. Miễn phí nhưng có hạn mức số lượt mỗi phút và mỗi ngày,
    # nên phase 6 chạy dài có thể bị chặn giữa chừng — xử lý ở phần retry 429.
    "groq": Provider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        default_model=os.getenv("KLTN_GROQ_MODEL", "openai/gpt-oss-120b"),
        input_price=0.0,
        output_price=0.0,
    ),
    # Tầng demo và bảng so sánh cuối.
    "openai": Provider(
        name="openai",
        base_url=None,
        api_key_env="OPENAI_API_KEY",
        default_model=os.getenv("KLTN_OPENAI_MODEL", "gpt-4.1-mini"),
        input_price=0.40,
        output_price=1.60,
    ),
}

DEFAULT_PROVIDER = os.getenv("KLTN_PROVIDER", "groq")


@dataclass
class ReasoningResult:
    """Kết quả một lần chẩn đoán, kèm mọi thứ cần cho phần chi phí của báo cáo."""

    explanation: Explanation | None
    provider: str
    model: str
    attempts: int                    # số lần gọi API, tính cả retry
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    from_cache: bool = False
    used_strict_schema: bool = True
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.explanation is not None

    def cost_usd(self, provider: Provider) -> float:
        return (self.input_tokens * provider.input_price
                + self.output_tokens * provider.output_price) / 1_000_000

    def to_dict(self) -> dict:
        d = asdict(self)
        d["explanation"] = self.explanation.model_dump() if self.explanation else None
        return d


def strict_schema(model_cls: type) -> dict:
    """Đổi schema của Pydantic sang dạng nghiêm ngặt mà OpenAI chấp nhận.

    Ba yêu cầu của chế độ nghiêm ngặt: mọi object phải có `additionalProperties: false`,
    mọi thuộc tính phải nằm trong `required`, và không được có thuộc tính tự do.
    Pydantic không tự sinh đúng ba điều đó nên phải sửa lại bằng tay.
    """
    schema = model_cls.model_json_schema()

    def fix(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            # `default` làm OpenAI từ chối trong chế độ nghiêm ngặt
            node.pop("default", None)
            for v in node.values():
                fix(v)
        elif isinstance(node, list):
            for v in node:
                fix(v)

    fix(schema)
    return schema


class XaiReasoner:
    """Bộ chẩn đoán. Tạo một lần rồi dùng lại cho cả loạt thí nghiệm."""

    def __init__(
        self,
        provider: str = DEFAULT_PROVIDER,
        model: str | None = None,
        max_retries: int = MAX_RETRIES,
        use_cache: bool = True,
        include_example: bool = True,
        max_tokens: int = 4000,
        temperature: float = 0.0,
        max_rate_limit_waits: int = 12,
    ):
        if provider not in PROVIDERS:
            raise ValueError(
                f"khong biet nha cung cap '{provider}'. "
                f"Chon mot trong: {', '.join(PROVIDERS)}"
            )
        self.provider = PROVIDERS[provider]
        api_key = os.getenv(self.provider.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"chua co khoa API. Dat {self.provider.api_key_env} trong file .env "
                f"o thu muc goc repo (xem .env.example)."
            )
        self.client = OpenAI(api_key=api_key, base_url=self.provider.base_url)
        self.model = model or self.provider.default_model
        self.max_retries = max_retries
        # Quy cho han muc TACH RIENG khoi quy thu lai vi sai schema. Bi chan
        # han muc khong phai la model suy luan sai, khong dang bi tru luot.
        self.max_rate_limit_waits = max_rate_limit_waits
        self.use_cache = use_cache
        self.include_example = include_example
        self.max_tokens = max_tokens
        # temperature 0 để kết quả bớt dao động. KHÔNG làm nó hết dao động hoàn toàn,
        # nên mục 8 vẫn bắt buộc chạy 5 lần mỗi kịch bản.
        self.temperature = temperature
        self._schema = strict_schema(Explanation)
        # Nhà cung cấp nào không nhận schema nghiêm ngặt thì tự hạ xuống chế độ
        # "chỉ cần là JSON hợp lệ" và nhét schema vào prompt. Bật cờ này sau lần
        # đầu bị từ chối, khỏi thử lại vô ích ở mọi ca sau.
        self._strict_ok = True

    # ------------------------------------------------------------------

    def diagnose(self, snapshot_text: str, fingerprint: str | None = None) -> ReasoningResult:
        """Đọc đoạn text mô tả hệ thống, trả về lời giải thích đã validate."""
        key = fingerprint or hashlib.sha256(
            snapshot_text.encode("utf-8")
        ).hexdigest()[:16]

        if self.use_cache:
            cached = self._load_cache(key)
            if cached is not None:
                return cached

        started = time.time()
        result = self._call_with_retry(snapshot_text)
        result.latency_s = round(time.time() - started, 2)

        if self.use_cache and result.ok:
            self._save_cache(key, result)
        return result

    # ------------------------------------------------------------------

    def _response_format(self) -> dict:
        if self._strict_ok:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": "explanation",
                    "strict": True,
                    "schema": self._schema,
                },
            }
        return {"type": "json_object"}

    @staticmethod
    def _retry_after_seconds(err) -> float:
        """Doc header xem nha cung cap bao cho bao lau.

        Groq goi mien phi gioi han 8000 token MOI PHUT, ma mot lan chan doan ton
        khoang 5000 token vao cong 900 ra. Nghia la chi lot dung mot luot moi phut.
        Cho cung mot con so 20 giay thi lan nao cung bi chan tiep va het luot oan.
        Con so dung nam san trong header, phai doc no thay vi doan.
        """
        headers = getattr(getattr(err, "response", None), "headers", None) or {}
        for key in ("retry-after", "x-ratelimit-reset-tokens",
                    "x-ratelimit-reset-requests"):
            raw = headers.get(key)
            if not raw:
                continue
            try:
                return min(float(str(raw).rstrip("s")), 120.0)
            except ValueError:
                continue
        return 60.0

    def _call_with_retry(self, snapshot_text: str) -> ReasoningResult:
        system = SYSTEM_PROMPT
        if not self._strict_ok:
            system += chr(10) + chr(10) + schema_instruction(self._schema)

        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": build_user_prompt(snapshot_text,
                                                          self.include_example)},
        ]
        result = ReasoningResult(explanation=None, provider=self.provider.name,
                                 model=self.model, attempts=0)
        attempt = 0
        rate_waits = 0

        while attempt < self.max_retries:
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    response_format=self._response_format(),
                )
            except APIStatusError as e:
                body = str(getattr(e, "message", e))
                # Nha cung cap khong ho tro schema nghiem ngat -> ha cap mot lan roi
                # thu lai ngay. KHONG tinh la mot luot suy luan.
                if self._strict_ok and e.status_code == 400 and (
                    "json_schema" in body or "response_format" in body
                ):
                    result.used_strict_schema = False
                    self._strict_ok = False
                    messages[0]["content"] = (
                        SYSTEM_PROMPT + chr(10) + chr(10)
                        + schema_instruction(self._schema)
                    )
                    result.errors.append(
                        "khong ho tro json_schema, ha xuong json_object")
                    continue
                if e.status_code == 413:
                    # 413 "Request too large": prompt cong max_tokens vuot tran
                    # token MOI PHUT cua nha cung cap. Groq goi mien phi cho 8000,
                    # ma prompt cua agent khoang 6000 cong max_tokens 4000 la vuot.
                    #
                    # KHAC 429 O CHO: cho bao lau cung khong het, vi day khong phai
                    # "dung qua nhanh" ma la "MOT request nay da qua to". Phai thu
                    # nho lai, va neu khong nho duoc nua thi bo cuoc that.
                    if self.max_tokens > 1200:
                        self.max_tokens = max(1200, self.max_tokens // 2)
                        result.errors.append(
                            f"API 413 qua to, ha max_tokens xuong {self.max_tokens}")
                        print(f"    [413] request qua to, ha max_tokens xuong "
                              f"{self.max_tokens}", flush=True)
                        continue
                    attempt += 1
                    result.attempts = attempt
                    result.errors.append(
                        "API 413: request van qua to du da ha max_tokens xuong "
                        f"{self.max_tokens}. Prompt qua dai so voi tran token cua "
                        f"nha cung cap — doi sang --provider openai." + body[:200])
                    break
                if e.status_code == 429:
                    rate_waits += 1
                    if rate_waits > self.max_rate_limit_waits:
                        result.errors.append(
                            f"bi chan han muc {rate_waits} lan lien tiep, bo cuoc: "
                            + body[:400])
                        break
                    wait = self._retry_after_seconds(e)
                    print(f"    [han muc] bi chan, cho {wait:.0f}s "
                          f"(lan {rate_waits}/{self.max_rate_limit_waits})",
                          flush=True)
                    time.sleep(wait)
                    continue
                attempt += 1
                result.attempts = attempt
                result.errors.append(f"API {e.status_code}: {body[:400]}")
                if e.status_code < 500:
                    break
                continue
            except APIConnectionError as e:
                result.errors.append(f"mat ket noi: {e}")
                time.sleep(5)
                continue

            attempt += 1
            result.attempts = attempt

            usage = getattr(response, "usage", None)
            if usage:
                result.input_tokens += usage.prompt_tokens or 0
                result.output_tokens += usage.completion_tokens or 0

            raw = (response.choices[0].message.content or "").strip()
            try:
                result.explanation = Explanation.model_validate_json(raw)
                return result
            except ValidationError as ve:
                err = str(ve)
            except Exception as ex:
                err = f"khong phai JSON hop le: {ex}"

            result.errors.append(f"lan {attempt}: {err[:300]}")
            # Nhoi lai kem dung thong bao loi cua Pydantic - model sua dung hon han
            # khi thay cho sai cu the thay vi chi bi bao "sai roi lam lai".
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": build_retry_prompt(raw, err)},
            ]

        return result

    # ------------------------------------------------------------------
    # Cache theo mã băm — mục 7.5 KLTN.md
    # ------------------------------------------------------------------

    def _cache_path(self, key: str) -> Path:
        safe_model = self.model.replace("/", "_")
        return CACHE_DIR / f"{PROMPT_VERSION}_{self.provider.name}_{safe_model}_{key}.json"

    def _load_cache(self, key: str) -> ReasoningResult | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            exp = Explanation.model_validate(d["explanation"])
        except Exception:
            return None
        return ReasoningResult(
            explanation=exp,
            provider=d.get("provider", self.provider.name),
            model=d.get("model", self.model),
            attempts=d.get("attempts", 1),
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            latency_s=d.get("latency_s", 0.0),
            from_cache=True,
        )

    def _save_cache(self, key: str, result: ReasoningResult) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._cache_path(key).write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------

    def estimate_prompt_chars(self, snapshot_text: str) -> int:
        """Độ dài prompt tính bằng ký tự.

        Không có API đếm token dùng chung cho cả hai nhà cung cấp, nên ước lượng
        theo ký tự. Con số token THẬT lấy từ `usage` của lần gọi đầu tiên — dùng
        con số đó cho bảng chi phí trong báo cáo, đừng dùng ước lượng này.
        """
        return len(SYSTEM_PROMPT) + len(
            build_user_prompt(snapshot_text, self.include_example)
        )
