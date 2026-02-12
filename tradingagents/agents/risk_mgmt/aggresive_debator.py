import time
import json

# 导入统一日志系统
from tradingagents.utils.logging_init import get_logger
logger = get_logger("default")
from tradingagents.agents.utils.prompt_contract import (
    get_anti_repetition_rules,
    get_concise_contract,
    get_output_schema,
)


def create_risky_debator(llm):
    def risky_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        expression_profile = state.get("expression_profile", "balanced")
        history = risk_debate_state.get("history", "")
        risky_history = risk_debate_state.get("risky_history", "")

        current_safe_response = risk_debate_state.get("current_safe_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]
        history_for_prompt = history
        current_safe_for_prompt = current_safe_response
        current_neutral_for_prompt = current_neutral_response
        market_for_prompt = market_research_report
        sentiment_for_prompt = sentiment_report
        news_for_prompt = news_report
        fundamentals_for_prompt = fundamentals_report
        trader_for_prompt = trader_decision

        # 📊 记录输入数据长度
        logger.info(f"📊 [Risky Analyst] 输入数据长度统计:")
        logger.info(f"  - market_report: 原始{len(market_research_report):,} -> 输入{len(market_for_prompt):,} 字符")
        logger.info(f"  - sentiment_report: 原始{len(sentiment_report):,} -> 输入{len(sentiment_for_prompt):,} 字符")
        logger.info(f"  - news_report: 原始{len(news_report):,} -> 输入{len(news_for_prompt):,} 字符")
        logger.info(f"  - fundamentals_report: 原始{len(fundamentals_report):,} -> 输入{len(fundamentals_for_prompt):,} 字符")
        logger.info(f"  - trader_decision: 原始{len(trader_decision):,} -> 输入{len(trader_for_prompt):,} 字符")
        logger.info(f"  - history: 原始{len(history):,} -> 输入{len(history_for_prompt):,} 字符")
        total_length = (len(market_for_prompt) + len(sentiment_for_prompt) +
                       len(news_for_prompt) + len(fundamentals_for_prompt) +
                       len(trader_for_prompt) + len(history_for_prompt) +
                       len(current_safe_for_prompt) + len(current_neutral_for_prompt))
        logger.info(f"  - 总Prompt长度: {total_length:,} 字符 (~{total_length//4:,} tokens)")

        concise_contract = get_concise_contract("aggresive_debator", expression_profile)
        output_schema = get_output_schema("aggresive_debator")
        anti_repetition_rules = get_anti_repetition_rules()

        prompt = f"""你是激进风险辩手，主张在可控前提下追求高回报机会。

{concise_contract}
角色输出模板：{output_schema}
{anti_repetition_rules}

你的任务：直接回应保守与中性辩手观点，指出其过度保守之处，并给出数据化反驳。
以下是交易员方案：

{trader_for_prompt}

请基于以下输入构建你的辩论：

市场研究报告：{market_for_prompt}
社交媒体情绪报告：{sentiment_for_prompt}
最新世界事务报告：{news_for_prompt}
公司基本面报告：{fundamentals_for_prompt}
对话历史：{history_for_prompt}
保守辩手最新观点：{current_safe_for_prompt}
中性辩手最新观点：{current_neutral_for_prompt}

请用中文输出，重点体现为何高回报路径更优，同时明确关键风险前提。"""

        logger.info(f"⏱️ [Risky Analyst] 开始调用LLM...")
        import time
        llm_start_time = time.time()

        response = llm.invoke(prompt)

        llm_elapsed = time.time() - llm_start_time
        logger.info(f"⏱️ [Risky Analyst] LLM调用完成，耗时: {llm_elapsed:.2f}秒")

        argument = f"Risky Analyst: {response.content}"

        new_count = risk_debate_state["count"] + 1
        logger.info(f"🔥 [激进风险分析师] 发言完成，计数: {risk_debate_state['count']} -> {new_count}")

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "risky_history": risky_history + "\n" + argument,
            "safe_history": risk_debate_state.get("safe_history", ""),
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Risky",
            "current_risky_response": argument,
            "current_safe_response": risk_debate_state.get("current_safe_response", ""),
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": new_count,
        }

        return {"risk_debate_state": new_risk_debate_state}

    return risky_node
