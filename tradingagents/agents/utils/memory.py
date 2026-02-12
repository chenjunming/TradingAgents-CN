import asyncio
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tradingagents.utils.logging_init import get_logger

logger = get_logger("agents.utils.memory")

# OpenMemory 在导入阶段会读取配置，这里先确保有默认持久化路径。
if not os.getenv("OM_DB_URL") and not os.getenv("OM_DB_PATH"):
    _default_db = (
        Path(os.getenv("OPENMEMORY_DB_PATH", "data/openmemory/openmemory.db"))
        .expanduser()
        .resolve()
    )
    _default_db.parent.mkdir(parents=True, exist_ok=True)
    os.environ["OM_DB_URL"] = f"sqlite:///{_default_db}"

try:
    from openmemory.client import Memory as OpenMemoryClient
    _OPENMEMORY_IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - import error path
    OpenMemoryClient = None
    _OPENMEMORY_IMPORT_ERROR = e


class FinancialSituationMemory:
    """
    OpenMemory-backed long-term memory.

    Compatibility notes:
    - Keeps the existing class/method names used by the agent graph.
    - Stores each role in an isolated OpenMemory user namespace.
    """

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        if OpenMemoryClient is None:
            raise RuntimeError(
                f"OpenMemory 未安装或不可用: {_OPENMEMORY_IMPORT_ERROR}. "
                "请安装 `openmemory-py`。"
            )

        self.name = name
        self.config = config or {}
        self.llm_provider = str(self.config.get("llm_provider", "unknown")).lower()

        namespace = (
            self.config.get("openmemory_user_id")
            or os.getenv("OPENMEMORY_USER_ID")
            or "tradingagents"
        )
        # 角色隔离，避免不同智能体互相污染记忆
        self.user_id = f"{namespace}:{name}"
        self._storage_prefix = f"[tradingagents_memory_role={self.name}] "

        self.client = OpenMemoryClient(user=self.user_id)
        logger.info(f"🧠 [OpenMemory] 初始化完成: role={self.name}, user_id={self.user_id}")

    def _run_async(self, coro):
        """
        Run an async coroutine from sync code safely.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        result_box: Dict[str, Any] = {}
        error_box: Dict[str, Exception] = {}

        def _runner():
            try:
                result_box["result"] = asyncio.run(coro)
            except Exception as e:  # pragma: no cover - defensive branch
                error_box["error"] = e

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()

        if "error" in error_box:
            raise error_box["error"]
        return result_box.get("result")

    def add_situations(self, situations_and_advice: List[Tuple[str, str]]):
        """
        Add financial situations and advice into OpenMemory.
        """
        if not situations_and_advice:
            return

        for situation, recommendation in situations_and_advice:
            situation_text = (situation or "").strip()
            recommendation_text = (recommendation or "").strip()
            if not situation_text:
                continue

            stored_content = f"{self._storage_prefix}{situation_text}"
            meta = {
                "recommendation": recommendation_text,
                "memory_role": self.name,
                "llm_provider": self.llm_provider,
            }

            try:
                self._run_async(
                    self.client.add(
                        content=stored_content,
                        user_id=self.user_id,
                        meta=meta,
                        tags=[self.name, "tradingagents"],
                    )
                )
            except Exception as e:
                logger.warning(f"⚠️ [OpenMemory] 写入失败: role={self.name}, error={e}")

    def get_memories(self, current_situation: str, n_matches: int = 1) -> List[Dict[str, Any]]:
        """
        Retrieve similar memories from OpenMemory.
        """
        query = (current_situation or "").strip()
        if not query:
            return []

        query_text = f"{self._storage_prefix}{query}"
        try:
            raw_results = self._run_async(
                self.client.search(
                    query=query_text,
                    user_id=self.user_id,
                    limit=max(1, int(n_matches)),
                )
            )
        except Exception as e:
            logger.warning(f"⚠️ [OpenMemory] 检索失败: role={self.name}, error={e}")
            return []

        memories: List[Dict[str, Any]] = []
        for item in raw_results or []:
            content = str(item.get("content", "")).strip()
            if content.startswith(self._storage_prefix):
                content = content[len(self._storage_prefix):].strip()
            metadata = item.get("metadata", {}) or {}
            recommendation = str(metadata.get("recommendation", "")).strip()

            # OpenMemory uses score (higher is better). Keep legacy fields for compatibility.
            score = item.get("score", 0.0)
            try:
                similarity = float(score)
            except (TypeError, ValueError):
                similarity = 0.0
            similarity = max(0.0, min(1.0, similarity))

            memories.append(
                {
                    "situation": content,
                    "recommendation": recommendation,
                    "similarity": similarity,
                    "distance": 1.0 - similarity,
                    "id": item.get("id"),
                }
            )

        return memories

    # ---- Compatibility helpers kept for existing call sites / diagnostics ----
    def get_embedding(self, text: str):
        logger.debug("OpenMemory后端不直接暴露embedding，返回空列表")
        return []

    def get_embedding_config_status(self):
        return {
            "enabled": True,
            "provider": "openmemory",
            "client_status": "ENABLED",
            "user_id": self.user_id,
        }

    def get_last_text_info(self):
        return None

    def get_cache_info(self):
        count = None
        try:
            # history 为同步接口
            history = self.client.history(user_id=self.user_id, limit=1000, offset=0)
            count = len(history)
        except Exception:
            pass

        return {
            "backend": "openmemory",
            "collection_count": count,
            "provider": self.llm_provider,
            "user_id": self.user_id,
        }
