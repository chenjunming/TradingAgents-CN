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


def create_neutral_debator(llm):
    def neutral_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        neutral_history = risk_debate_state.get("neutral_history", "")

        current_risky_response = risk_debate_state.get("current_risky_response", "")
        current_safe_response = risk_debate_state.get("current_safe_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]
        history_for_prompt = _tail_text(history, _MAX_HISTORY_CHARS)
        current_risky_for_prompt = _tail_text(current_risky_response, _MAX_LAST_RESPONSE_CHARS)
        current_safe_for_prompt = _tail_text(current_safe_response, _MAX_LAST_RESPONSE_CHARS)
        market_for_prompt = _tail_text(market_research_report, _MAX_REPORT_CHARS)
        sentiment_for_prompt = _tail_text(sentiment_report, _MAX_REPORT_CHARS)
        news_for_prompt = _tail_text(news_report, _MAX_REPORT_CHARS)
        fundamentals_for_prompt = _tail_text(fundamentals_report, _MAX_REPORT_CHARS)
        trader_for_prompt = _tail_text(trader_decision, _MAX_REPORT_CHARS)

        # 📊 记录所有输入数据的长度，用于性能分析
        logger.info(f"📊 [Neutral Analyst] 输入数据长度统计:")
        logger.info(f"  - market_report: 原始{len(market_research_report):,} -> 输入{len(market_for_prompt):,} 字符 (~{len(market_for_prompt)//4:,} tokens)")
        logger.info(f"  - sentiment_report: 原始{len(sentiment_report):,} -> 输入{len(sentiment_for_prompt):,} 字符 (~{len(sentiment_for_prompt)//4:,} tokens)")
        logger.info(f"  - news_report: 原始{len(news_report):,} -> 输入{len(news_for_prompt):,} 字符 (~{len(news_for_prompt)//4:,} tokens)")
        logger.info(f"  - fundamentals_report: 原始{len(fundamentals_report):,} -> 输入{len(fundamentals_for_prompt):,} 字符 (~{len(fundamentals_for_prompt)//4:,} tokens)")
        logger.info(f"  - trader_decision: 原始{len(trader_decision):,} -> 输入{len(trader_for_prompt):,} 字符 (~{len(trader_for_prompt)//4:,} tokens)")
        logger.info(f"  - history: 原始{len(history):,} -> 输入{len(history_for_prompt):,} 字符 (~{len(history_for_prompt)//4:,} tokens)")
        logger.info(f"  - current_risky_response: 原始{len(current_risky_response):,} -> 输入{len(current_risky_for_prompt):,} 字符 (~{len(current_risky_for_prompt)//4:,} tokens)")
        logger.info(f"  - current_safe_response: 原始{len(current_safe_response):,} -> 输入{len(current_safe_for_prompt):,} 字符 (~{len(current_safe_for_prompt)//4:,} tokens)")

        # 计算总prompt长度
        total_prompt_length = (len(market_for_prompt) + len(sentiment_for_prompt) +
                              len(news_for_prompt) + len(fundamentals_for_prompt) +
                              len(trader_for_prompt) + len(history_for_prompt) +
                              len(current_risky_for_prompt) + len(current_safe_for_prompt))
        logger.info(f"  - 🚨 总Prompt长度: {total_prompt_length:,} 字符 (~{total_prompt_length//4:,} tokens)")

        prompt = f"""作为中性风险分析师，您的角色是提供平衡的视角，权衡交易员决策或计划的潜在收益和风险。您优先考虑全面的方法，评估上行和下行风险，同时考虑更广泛的市场趋势、潜在的经济变化和多元化策略。以下是交易员的决策：

{trader_for_prompt}

您的任务是挑战激进和安全分析师，指出每种观点可能过于乐观或过于谨慎的地方。使用以下数据来源的见解来支持调整交易员决策的温和、可持续策略：

市场研究报告：{market_for_prompt}
社交媒体情绪报告：{sentiment_for_prompt}
最新世界事务报告：{news_for_prompt}
公司基本面报告：{fundamentals_for_prompt}
以下是当前对话历史：{history_for_prompt} 以下是激进分析师的最后回应：{current_risky_for_prompt} 以下是安全分析师的最后回应：{current_safe_for_prompt}。如果其他观点没有回应，请不要虚构，只需提出您的观点。

通过批判性地分析双方来积极参与，解决激进和保守论点中的弱点，倡导更平衡的方法。挑战他们的每个观点，说明为什么适度风险策略可能提供两全其美的效果，既提供增长潜力又防范极端波动。专注于辩论而不是简单地呈现数据，旨在表明平衡的观点可以带来最可靠的结果。请用中文以对话方式输出，就像您在说话一样，不使用任何特殊格式。请将回答控制在600字以内。"""

        logger.info(f"⏱️ [Neutral Analyst] 开始调用LLM...")
        llm_start_time = time.time()

        response = llm.invoke(prompt)

        llm_elapsed = time.time() - llm_start_time
        logger.info(f"⏱️ [Neutral Analyst] LLM调用完成，耗时: {llm_elapsed:.2f}秒")
        logger.info(f"📝 [Neutral Analyst] 响应长度: {len(response.content):,} 字符")

        argument = f"Neutral Analyst: {response.content}"

        new_count = risk_debate_state["count"] + 1
        logger.info(f"⚖️ [中性风险分析师] 发言完成，计数: {risk_debate_state['count']} -> {new_count}")

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "risky_history": risk_debate_state.get("risky_history", ""),
            "safe_history": risk_debate_state.get("safe_history", ""),
            "neutral_history": neutral_history + "\n" + argument,
            "latest_speaker": "Neutral",
            "current_risky_response": risk_debate_state.get(
                "current_risky_response", ""
            ),
            "current_safe_response": risk_debate_state.get("current_safe_response", ""),
            "current_neutral_response": argument,
            "count": new_count,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return neutral_node
