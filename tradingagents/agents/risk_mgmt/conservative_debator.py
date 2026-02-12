from langchain_core.messages import AIMessage
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


def create_safe_debator(llm):
    def safe_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        expression_profile = state.get("expression_profile", "balanced")
        history = risk_debate_state.get("history", "")
        safe_history = risk_debate_state.get("safe_history", "")

        current_risky_response = risk_debate_state.get("current_risky_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        trader_decision = state["trader_investment_plan"]
        history_for_prompt = history
        current_risky_for_prompt = current_risky_response
        current_neutral_for_prompt = current_neutral_response
        market_for_prompt = market_research_report
        sentiment_for_prompt = sentiment_report
        news_for_prompt = news_report
        fundamentals_for_prompt = fundamentals_report
        trader_for_prompt = trader_decision

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

        concise_contract = get_concise_contract("conservative_debator", expression_profile)
        output_schema = get_output_schema("conservative_debator")
        anti_repetition_rules = get_anti_repetition_rules()

        prompt = f"""你是保守风险辩手，目标是保护资产、控制回撤、保障稳定增长。

{concise_contract}
角色输出模板：{output_schema}
{anti_repetition_rules}

你的任务：批判交易员方案中的高风险环节，并直接反驳激进与中性观点中被低估的下行风险。
以下是交易员方案：

{trader_for_prompt}

请基于以下输入构建你的辩论：

市场研究报告：{market_for_prompt}
社交媒体情绪报告：{sentiment_for_prompt}
最新世界事务报告：{news_for_prompt}
公司基本面报告：{fundamentals_for_prompt}
对话历史：{history_for_prompt}
激进辩手最新观点：{current_risky_for_prompt}
中性辩手最新观点：{current_neutral_for_prompt}

请用中文输出，明确给出低风险替代路径及触发条件。"""

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
