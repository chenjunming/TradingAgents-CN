from langchain_core.messages import AIMessage
import time
import json

# 导入统一日志系统
from tradingagents.utils.logging_init import get_logger
logger = get_logger("default")

_MAX_HISTORY_CHARS = 2400
_MAX_LAST_RESPONSE_CHARS = 1200
_MAX_REPORT_CHARS = 1800


def _tail_text(text: str, max_chars: int) -> str:
    if not text or len(text) <= max_chars:
        return text
    return f"...[已截断较早内容，仅保留最近{max_chars}字符]\n{text[-max_chars:]}"


def create_safe_debator(llm):
    def safe_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        safe_history = risk_debate_state.get("safe_history", "")

        current_risky_response = risk_debate_state.get("current_risky_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]
        history_for_prompt = _tail_text(history, _MAX_HISTORY_CHARS)
        current_risky_for_prompt = _tail_text(current_risky_response, _MAX_LAST_RESPONSE_CHARS)
        current_neutral_for_prompt = _tail_text(current_neutral_response, _MAX_LAST_RESPONSE_CHARS)
        market_for_prompt = _tail_text(market_research_report, _MAX_REPORT_CHARS)
        sentiment_for_prompt = _tail_text(sentiment_report, _MAX_REPORT_CHARS)
        news_for_prompt = _tail_text(news_report, _MAX_REPORT_CHARS)
        fundamentals_for_prompt = _tail_text(fundamentals_report, _MAX_REPORT_CHARS)
        trader_for_prompt = _tail_text(trader_decision, _MAX_REPORT_CHARS)

        # 📊 记录输入数据长度
        logger.info(f"📊 [Safe Analyst] 输入数据长度统计:")
        logger.info(f"  - market_report: 原始{len(market_research_report):,} -> 输入{len(market_for_prompt):,} 字符")
        logger.info(f"  - sentiment_report: 原始{len(sentiment_report):,} -> 输入{len(sentiment_for_prompt):,} 字符")
        logger.info(f"  - news_report: 原始{len(news_report):,} -> 输入{len(news_for_prompt):,} 字符")
        logger.info(f"  - fundamentals_report: 原始{len(fundamentals_report):,} -> 输入{len(fundamentals_for_prompt):,} 字符")
        logger.info(f"  - trader_decision: 原始{len(trader_decision):,} -> 输入{len(trader_for_prompt):,} 字符")
        logger.info(f"  - history: 原始{len(history):,} -> 输入{len(history_for_prompt):,} 字符")
        total_length = (len(market_for_prompt) + len(sentiment_for_prompt) +
                       len(news_for_prompt) + len(fundamentals_for_prompt) +
                       len(trader_for_prompt) + len(history_for_prompt) +
                       len(current_risky_for_prompt) + len(current_neutral_for_prompt))
        logger.info(f"  - 总Prompt长度: {total_length:,} 字符 (~{total_length//4:,} tokens)")

        prompt = f"""作为安全/保守风险分析师，您的主要目标是保护资产、最小化波动性，并确保稳定、可靠的增长。您优先考虑稳定性、安全性和风险缓解，仔细评估潜在损失、经济衰退和市场波动。在评估交易员的决策或计划时，请批判性地审查高风险要素，指出决策可能使公司面临不当风险的地方，以及更谨慎的替代方案如何能够确保长期收益。以下是交易员的决策：

{trader_for_prompt}

您的任务是积极反驳激进和中性分析师的论点，突出他们的观点可能忽视的潜在威胁或未能优先考虑可持续性的地方。直接回应他们的观点，利用以下数据来源为交易员决策的低风险方法调整建立令人信服的案例：

市场研究报告：{market_for_prompt}
社交媒体情绪报告：{sentiment_for_prompt}
最新世界事务报告：{news_for_prompt}
公司基本面报告：{fundamentals_for_prompt}
以下是当前对话历史：{history_for_prompt} 以下是激进分析师的最后回应：{current_risky_for_prompt} 以下是中性分析师的最后回应：{current_neutral_for_prompt}。如果其他观点没有回应，请不要虚构，只需提出您的观点。

通过质疑他们的乐观态度并强调他们可能忽视的潜在下行风险来参与讨论。解决他们的每个反驳点，展示为什么保守立场最终是公司资产最安全的道路。专注于辩论和批评他们的论点，证明低风险策略相对于他们方法的优势。请用中文以对话方式输出，就像您在说话一样，不使用任何特殊格式。请将回答控制在600字以内。"""

        logger.info(f"⏱️ [Safe Analyst] 开始调用LLM...")
        llm_start_time = time.time()

        response = llm.invoke(prompt)

        llm_elapsed = time.time() - llm_start_time
        logger.info(f"⏱️ [Safe Analyst] LLM调用完成，耗时: {llm_elapsed:.2f}秒")

        argument = f"Safe Analyst: {response.content}"

        new_count = risk_debate_state["count"] + 1
        logger.info(f"🛡️ [保守风险分析师] 发言完成，计数: {risk_debate_state['count']} -> {new_count}")

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "risky_history": risk_debate_state.get("risky_history", ""),
            "safe_history": safe_history + "\n" + argument,
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Safe",
            "current_risky_response": risk_debate_state.get(
                "current_risky_response", ""
            ),
            "current_safe_response": argument,
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": new_count,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return safe_node
