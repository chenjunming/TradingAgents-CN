from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional


_session_id_var: ContextVar[Optional[str]] = ContextVar("ta_token_session_id", default=None)
_analysis_type_var: ContextVar[str] = ContextVar("ta_token_analysis_type", default="stock_analysis")


def get_current_session_id() -> Optional[str]:
    return _session_id_var.get()


def get_current_analysis_type() -> str:
    return _analysis_type_var.get()


@contextmanager
def token_usage_context(session_id: Optional[str], analysis_type: str = "stock_analysis"):
    session_token = _session_id_var.set(session_id)
    analysis_token = _analysis_type_var.set(analysis_type or "stock_analysis")
    try:
        yield
    finally:
        _session_id_var.reset(session_token)
        _analysis_type_var.reset(analysis_token)
