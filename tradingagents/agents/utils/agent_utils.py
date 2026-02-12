from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage, AIMessage
from typing import List
from typing import Annotated
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import RemoveMessage
from langchain_core.tools import tool
from datetime import date, timedelta, datetime
import functools
import pandas as pd
import os
import json
import hashlib
from dateutil.relativedelta import relativedelta
from langchain_openai import ChatOpenAI
import tradingagents.dataflows.interface as interface
from tradingagents.default_config import DEFAULT_CONFIG
from langchain_core.messages import HumanMessage

# 导入统一日志系统和工具日志装饰器
from tradingagents.utils.logging_init import get_logger
from tradingagents.utils.tool_logging import log_tool_call, log_analysis_step

# 导入日志模块
from tradingagents.utils.logging_manager import get_logger
logger = get_logger('agents')


def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]
        
        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]
        
        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")
        
        return {"messages": removal_operations + [placeholder]}
    
    return delete_messages


class Toolkit:
    _config = DEFAULT_CONFIG.copy()

    @classmethod
    def update_config(cls, config):
        """Update the class-level configuration."""
        cls._config.update(config)

    @property
    def config(self):
        """Access the configuration."""
        return self._config

    def __init__(self, config=None):
        if config:
            self.update_config(config)

    @staticmethod
    @tool
    def get_reddit_news(
        curr_date: Annotated[str, "Date you want to get news for in yyyy-mm-dd format"],
    ) -> str:
        """
        Retrieve global news from Reddit within a specified time frame.
        Args:
            curr_date (str): Date you want to get news for in yyyy-mm-dd format
        Returns:
            str: A formatted dataframe containing the latest global news from Reddit in the specified time frame.
        """
        
        global_news_result = interface.get_reddit_global_news(curr_date, 7, 5)

        return global_news_result

    @staticmethod
    @tool
    def get_finnhub_news(
        ticker: Annotated[
            str,
            "Search query of a company, e.g. 'AAPL, TSM, etc.",
        ],
        start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
        end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    ):
        """
        Retrieve the latest news about a given stock from Finnhub within a date range
        Args:
            ticker (str): Ticker of a company. e.g. AAPL, TSM
            start_date (str): Start date in yyyy-mm-dd format
            end_date (str): End date in yyyy-mm-dd format
        Returns:
            str: A formatted dataframe containing news about the company within the date range from start_date to end_date
        """

        end_date_str = end_date

        end_date = datetime.strptime(end_date, "%Y-%m-%d")
        start_date = datetime.strptime(start_date, "%Y-%m-%d")
        look_back_days = (end_date - start_date).days

        finnhub_news_result = interface.get_finnhub_news(
            ticker, end_date_str, look_back_days
        )

        return finnhub_news_result

    @staticmethod
    @tool
    def get_reddit_stock_info(
        ticker: Annotated[
            str,
            "Ticker of a company. e.g. AAPL, TSM",
        ],
        curr_date: Annotated[str, "Current date you want to get news for"],
    ) -> str:
        """
        Retrieve the latest news about a given stock from Reddit, given the current date.
        Args:
            ticker (str): Ticker of a company. e.g. AAPL, TSM
            curr_date (str): current date in yyyy-mm-dd format to get news for
        Returns:
            str: A formatted dataframe containing the latest news about the company on the given date
        """

        stock_news_results = interface.get_reddit_company_news(ticker, curr_date, 7, 5)

        return stock_news_results

    @staticmethod
    @tool
    def get_chinese_social_sentiment(
        ticker: Annotated[str, "Ticker of a company. e.g. AAPL, TSM"],
        curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    ) -> str:
        """
        获取中国社交媒体和财经平台上关于特定股票的情绪分析和讨论热度。
        整合雪球、东方财富股吧、新浪财经等中国本土平台的数据。
        Args:
            ticker (str): 股票代码，如 AAPL, TSM
            curr_date (str): 当前日期，格式为 yyyy-mm-dd
        Returns:
            str: 包含中国投资者情绪分析、讨论热度、关键观点的格式化报告
        """
        try:
            # 这里可以集成多个中国平台的数据
            chinese_sentiment_results = interface.get_chinese_social_sentiment(ticker, curr_date)
            return chinese_sentiment_results
        except Exception as e:
            # 如果中国平台数据获取失败，回退到原有的Reddit数据
            return interface.get_reddit_company_news(ticker, curr_date, 7, 5)

    @staticmethod
    # @tool  # 已移除：请使用 get_stock_fundamentals_unified 或 get_stock_market_data_unified
    def get_china_stock_data(
        stock_code: Annotated[str, "中国股票代码，如 000001(平安银行), 600519(贵州茅台)"],
        start_date: Annotated[str, "开始日期，格式 yyyy-mm-dd"],
        end_date: Annotated[str, "结束日期，格式 yyyy-mm-dd"],
    ) -> str:
        """
        获取中国A股实时和历史数据，通过Tushare等高质量数据源提供专业的股票数据。
        支持实时行情、历史K线、技术指标等全面数据，自动使用最佳数据源。
        Args:
            stock_code (str): 中国股票代码，如 000001(平安银行), 600519(贵州茅台)
            start_date (str): 开始日期，格式 yyyy-mm-dd
            end_date (str): 结束日期，格式 yyyy-mm-dd
        Returns:
            str: 包含实时行情、历史数据、技术指标的完整股票分析报告
        """
        try:
            logger.debug(f"📊 [DEBUG] ===== agent_utils.get_china_stock_data 开始调用 =====")
            logger.debug(f"📊 [DEBUG] 参数: stock_code={stock_code}, start_date={start_date}, end_date={end_date}")

            from tradingagents.dataflows.interface import get_china_stock_data_unified
            logger.debug(f"📊 [DEBUG] 成功导入统一数据源接口")

            logger.debug(f"📊 [DEBUG] 正在调用统一数据源接口...")
            result = get_china_stock_data_unified(stock_code, start_date, end_date)

            logger.debug(f"📊 [DEBUG] 统一数据源接口调用完成")
            logger.debug(f"📊 [DEBUG] 返回结果类型: {type(result)}")
            logger.debug(f"📊 [DEBUG] 返回结果长度: {len(result) if result else 0}")
            logger.debug(f"📊 [DEBUG] 返回结果前200字符: {str(result)[:200]}...")
            logger.debug(f"📊 [DEBUG] ===== agent_utils.get_china_stock_data 调用结束 =====")

            return result
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            logger.error(f"❌ [DEBUG] ===== agent_utils.get_china_stock_data 异常 =====")
            logger.error(f"❌ [DEBUG] 错误类型: {type(e).__name__}")
            logger.error(f"❌ [DEBUG] 错误信息: {str(e)}")
            logger.error(f"❌ [DEBUG] 详细堆栈:")
            print(error_details)
            logger.error(f"❌ [DEBUG] ===== 异常处理结束 =====")
            return f"中国股票数据获取失败: {str(e)}。请检查网络连接或稍后重试。"

    @staticmethod
    @tool
    def get_china_market_overview(
        curr_date: Annotated[str, "当前日期，格式 yyyy-mm-dd"],
    ) -> str:
        """
        获取中国股市整体概览，包括主要指数的实时行情。
        涵盖上证指数、深证成指、创业板指、科创50等主要指数。
        Args:
            curr_date (str): 当前日期，格式 yyyy-mm-dd
        Returns:
            str: 包含主要指数实时行情的市场概览报告
        """
        try:
            # 使用Tushare获取主要指数数据
            from tradingagents.dataflows.providers.china.tushare import get_tushare_adapter

            adapter = get_tushare_adapter()


            # 使用Tushare获取主要指数信息
            # 这里可以扩展为获取具体的指数数据
            return f"""# 中国股市概览 - {curr_date}

## 📊 主要指数
- 上证指数: 数据获取中...
- 深证成指: 数据获取中...
- 创业板指: 数据获取中...
- 科创50: 数据获取中...

## 💡 说明
市场概览功能正在从TDX迁移到Tushare，完整功能即将推出。
当前可以使用股票数据获取功能分析个股。

数据来源: Tushare专业数据源
更新时间: {curr_date}
"""

        except Exception as e:
            return f"中国市场概览获取失败: {str(e)}。正在从TDX迁移到Tushare数据源。"

    @staticmethod
    @tool
    def get_YFin_data(
        symbol: Annotated[str, "ticker symbol of the company"],
        start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
        end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    ) -> str:
        """
        Retrieve the stock price data for a given ticker symbol from Yahoo Finance.
        Args:
            symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
            start_date (str): Start date in yyyy-mm-dd format
            end_date (str): End date in yyyy-mm-dd format
        Returns:
            str: A formatted dataframe containing the stock price data for the specified ticker symbol in the specified date range.
        """

        result_data = interface.get_YFin_data(symbol, start_date, end_date)

        return result_data

    @staticmethod
    @tool
    def get_YFin_data_online(
        symbol: Annotated[str, "ticker symbol of the company"],
        start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
        end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    ) -> str:
        """
        Retrieve the stock price data for a given ticker symbol from Yahoo Finance.
        Args:
            symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
            start_date (str): Start date in yyyy-mm-dd format
            end_date (str): End date in yyyy-mm-dd format
        Returns:
            str: A formatted dataframe containing the stock price data for the specified ticker symbol in the specified date range.
        """

        result_data = interface.get_YFin_data_online(symbol, start_date, end_date)

        return result_data

    @staticmethod
    @tool
    def get_stockstats_indicators_report(
        symbol: Annotated[str, "ticker symbol of the company"],
        indicator: Annotated[
            str, "technical indicator to get the analysis and report of"
        ],
        curr_date: Annotated[
            str, "The current trading date you are trading on, YYYY-mm-dd"
        ],
        look_back_days: Annotated[int, "how many days to look back"] = 30,
    ) -> str:
        """
        Retrieve stock stats indicators for a given ticker symbol and indicator.
        Args:
            symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
            indicator (str): Technical indicator to get the analysis and report of
            curr_date (str): The current trading date you are trading on, YYYY-mm-dd
            look_back_days (int): How many days to look back, default is 30
        Returns:
            str: A formatted dataframe containing the stock stats indicators for the specified ticker symbol and indicator.
        """

        result_stockstats = interface.get_stock_stats_indicators_window(
            symbol, indicator, curr_date, look_back_days, False
        )

        return result_stockstats

    @staticmethod
    @tool
    def get_stockstats_indicators_report_online(
        symbol: Annotated[str, "ticker symbol of the company"],
        indicator: Annotated[
            str, "technical indicator to get the analysis and report of"
        ],
        curr_date: Annotated[
            str, "The current trading date you are trading on, YYYY-mm-dd"
        ],
        look_back_days: Annotated[int, "how many days to look back"] = 30,
    ) -> str:
        """
        Retrieve stock stats indicators for a given ticker symbol and indicator.
        Args:
            symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
            indicator (str): Technical indicator to get the analysis and report of
            curr_date (str): The current trading date you are trading on, YYYY-mm-dd
            look_back_days (int): How many days to look back, default is 30
        Returns:
            str: A formatted dataframe containing the stock stats indicators for the specified ticker symbol and indicator.
        """

        result_stockstats = interface.get_stock_stats_indicators_window(
            symbol, indicator, curr_date, look_back_days, True
        )

        return result_stockstats

    @staticmethod
    @tool
    def get_finnhub_company_insider_sentiment(
        ticker: Annotated[str, "ticker symbol for the company"],
        curr_date: Annotated[
            str,
            "current date of you are trading at, yyyy-mm-dd",
        ],
    ):
        """
        Retrieve insider sentiment information about a company (retrieved from public SEC information) for the past 30 days
        Args:
            ticker (str): ticker symbol of the company
            curr_date (str): current date you are trading at, yyyy-mm-dd
        Returns:
            str: a report of the sentiment in the past 30 days starting at curr_date
        """

        data_sentiment = interface.get_finnhub_company_insider_sentiment(
            ticker, curr_date, 30
        )

        return data_sentiment

    @staticmethod
    @tool
    def get_finnhub_company_insider_transactions(
        ticker: Annotated[str, "ticker symbol"],
        curr_date: Annotated[
            str,
            "current date you are trading at, yyyy-mm-dd",
        ],
    ):
        """
        Retrieve insider transaction information about a company (retrieved from public SEC information) for the past 30 days
        Args:
            ticker (str): ticker symbol of the company
            curr_date (str): current date you are trading at, yyyy-mm-dd
        Returns:
            str: a report of the company's insider transactions/trading information in the past 30 days
        """

        data_trans = interface.get_finnhub_company_insider_transactions(
            ticker, curr_date, 30
        )

        return data_trans

    @staticmethod
    @tool
    def get_simfin_balance_sheet(
        ticker: Annotated[str, "ticker symbol"],
        freq: Annotated[
            str,
            "reporting frequency of the company's financial history: annual/quarterly",
        ],
        curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
    ):
        """
        Retrieve the most recent balance sheet of a company
        Args:
            ticker (str): ticker symbol of the company
            freq (str): reporting frequency of the company's financial history: annual / quarterly
            curr_date (str): current date you are trading at, yyyy-mm-dd
        Returns:
            str: a report of the company's most recent balance sheet
        """

        data_balance_sheet = interface.get_simfin_balance_sheet(ticker, freq, curr_date)

        return data_balance_sheet

    @staticmethod
    @tool
    def get_simfin_cashflow(
        ticker: Annotated[str, "ticker symbol"],
        freq: Annotated[
            str,
            "reporting frequency of the company's financial history: annual/quarterly",
        ],
        curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
    ):
        """
        Retrieve the most recent cash flow statement of a company
        Args:
            ticker (str): ticker symbol of the company
            freq (str): reporting frequency of the company's financial history: annual / quarterly
            curr_date (str): current date you are trading at, yyyy-mm-dd
        Returns:
                str: a report of the company's most recent cash flow statement
        """

        data_cashflow = interface.get_simfin_cashflow(ticker, freq, curr_date)

        return data_cashflow

    @staticmethod
    @tool
    def get_simfin_income_stmt(
        ticker: Annotated[str, "ticker symbol"],
        freq: Annotated[
            str,
            "reporting frequency of the company's financial history: annual/quarterly",
        ],
        curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
    ):
        """
        Retrieve the most recent income statement of a company
        Args:
            ticker (str): ticker symbol of the company
            freq (str): reporting frequency of the company's financial history: annual / quarterly
            curr_date (str): current date you are trading at, yyyy-mm-dd
        Returns:
                str: a report of the company's most recent income statement
        """

        data_income_stmt = interface.get_simfin_income_statements(
            ticker, freq, curr_date
        )

        return data_income_stmt

    @staticmethod
    @tool
    def get_google_news(
        query: Annotated[str, "Query to search with"],
        curr_date: Annotated[str, "Curr date in yyyy-mm-dd format"],
    ):
        """
        Retrieve the latest news from Google News based on a query and date range.
        Args:
            query (str): Query to search with
            curr_date (str): Current date in yyyy-mm-dd format
            look_back_days (int): How many days to look back
        Returns:
            str: A formatted string containing the latest news from Google News based on the query and date range.
        """

        google_news_results = interface.get_google_news(query, curr_date, 7)

        return google_news_results

    @staticmethod
    @tool
    def get_realtime_stock_news(
        ticker: Annotated[str, "Ticker of a company. e.g. AAPL, TSM"],
        curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    ) -> str:
        """
        获取股票的实时新闻分析，解决传统新闻源的滞后性问题。
        整合多个专业财经API，提供15-30分钟内的最新新闻。
        支持多种新闻源轮询机制，优先使用实时新闻聚合器，失败时自动尝试备用新闻源。
        对于A股和港股，会优先使用中文财经新闻源（如东方财富）。
        
        Args:
            ticker (str): 股票代码，如 AAPL, TSM, 600036.SH
            curr_date (str): 当前日期，格式为 yyyy-mm-dd
        Returns:
            str: 包含实时新闻分析、紧急程度评估、时效性说明的格式化报告
        """
        from tradingagents.dataflows.realtime_news_utils import get_realtime_stock_news
        return get_realtime_stock_news(ticker, curr_date, hours_back=6)

    @staticmethod
    @tool
    def get_stock_news_openai(
        ticker: Annotated[str, "the company's ticker"],
        curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    ):
        """
        Retrieve the latest news about a given stock by using OpenAI's news API.
        Args:
            ticker (str): Ticker of a company. e.g. AAPL, TSM
            curr_date (str): Current date in yyyy-mm-dd format
        Returns:
            str: A formatted string containing the latest news about the company on the given date.
        """

        openai_news_results = interface.get_stock_news_openai(ticker, curr_date)

        return openai_news_results

    @staticmethod
    @tool
    def get_global_news_openai(
        curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    ):
        """
        Retrieve the latest macroeconomics news on a given date using OpenAI's macroeconomics news API.
        Args:
            curr_date (str): Current date in yyyy-mm-dd format
        Returns:
            str: A formatted string containing the latest macroeconomic news on the given date.
        """

        openai_news_results = interface.get_global_news_openai(curr_date)

        return openai_news_results

    @staticmethod
    # @tool  # 已移除：请使用 get_stock_fundamentals_unified
    def get_fundamentals_openai(
        ticker: Annotated[str, "the company's ticker"],
        curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    ):
        """
        Retrieve the latest fundamental information about a given stock on a given date by using OpenAI's news API.
        Args:
            ticker (str): Ticker of a company. e.g. AAPL, TSM
            curr_date (str): Current date in yyyy-mm-dd format
        Returns:
            str: A formatted string containing the latest fundamental information about the company on the given date.
        """
        logger.debug(f"📊 [DEBUG] get_fundamentals_openai 被调用: ticker={ticker}, date={curr_date}")

        # 检查是否为中国股票
        import re
        if re.match(r'^\d{6}$', str(ticker)):
            logger.debug(f"📊 [DEBUG] 检测到中国A股代码: {ticker}")
            # 使用统一接口获取中国股票名称
            try:
                from tradingagents.dataflows.interface import get_china_stock_info_unified
                stock_info = get_china_stock_info_unified(ticker)

                # 解析股票名称
                if "股票名称:" in stock_info:
                    company_name = stock_info.split("股票名称:")[1].split("\n")[0].strip()
                else:
                    company_name = f"股票代码{ticker}"

                logger.debug(f"📊 [DEBUG] 中国股票名称映射: {ticker} -> {company_name}")
            except Exception as e:
                logger.error(f"⚠️ [DEBUG] 从统一接口获取股票名称失败: {e}")
                company_name = f"股票代码{ticker}"

            # 修改查询以包含正确的公司名称
            modified_query = f"{company_name}({ticker})"
            logger.debug(f"📊 [DEBUG] 修改后的查询: {modified_query}")
        else:
            logger.debug(f"📊 [DEBUG] 检测到非中国股票: {ticker}")
            modified_query = ticker

        try:
            openai_fundamentals_results = interface.get_fundamentals_openai(
                modified_query, curr_date
            )
            logger.debug(f"📊 [DEBUG] OpenAI基本面分析结果长度: {len(openai_fundamentals_results) if openai_fundamentals_results else 0}")
            return openai_fundamentals_results
        except Exception as e:
            logger.error(f"❌ [DEBUG] OpenAI基本面分析失败: {str(e)}")
            return f"基本面分析失败: {str(e)}"

    @staticmethod
    # @tool  # 已移除：请使用 get_stock_fundamentals_unified
    def get_china_fundamentals(
        ticker: Annotated[str, "中国A股股票代码，如600036"],
        curr_date: Annotated[str, "当前日期，格式为yyyy-mm-dd"],
    ):
        """
        获取中国A股股票的基本面信息，使用中国股票数据源。
        Args:
            ticker (str): 中国A股股票代码，如600036, 000001
            curr_date (str): 当前日期，格式为yyyy-mm-dd
        Returns:
            str: 包含股票基本面信息的格式化字符串
        """
        logger.debug(f"📊 [DEBUG] get_china_fundamentals 被调用: ticker={ticker}, date={curr_date}")

        # 检查是否为中国股票
        import re
        if not re.match(r'^\d{6}$', str(ticker)):
            return f"错误：{ticker} 不是有效的中国A股代码格式"

        try:
            # 使用统一数据源接口获取股票数据（默认Tushare，支持备用数据源）
            from tradingagents.dataflows.interface import get_china_stock_data_unified
            logger.debug(f"📊 [DEBUG] 正在获取 {ticker} 的股票数据...")

            # 获取最近30天的数据用于基本面分析
            from datetime import datetime, timedelta
            end_date = datetime.strptime(curr_date, '%Y-%m-%d')
            start_date = end_date - timedelta(days=30)

            stock_data = get_china_stock_data_unified(
                ticker,
                start_date.strftime('%Y-%m-%d'),
                end_date.strftime('%Y-%m-%d')
            )

            logger.debug(f"📊 [DEBUG] 股票数据获取完成，长度: {len(stock_data) if stock_data else 0}")

            if not stock_data or "获取失败" in stock_data or "❌" in stock_data:
                return f"无法获取股票 {ticker} 的基本面数据：{stock_data}"

            # 调用真正的基本面分析
            from tradingagents.dataflows.optimized_china_data import OptimizedChinaDataProvider

            # 创建分析器实例
            analyzer = OptimizedChinaDataProvider()

            # 生成真正的基本面分析报告
            fundamentals_report = analyzer._generate_fundamentals_report(ticker, stock_data)

            logger.debug(f"📊 [DEBUG] 中国基本面分析报告生成完成")
            logger.debug(f"📊 [DEBUG] get_china_fundamentals 结果长度: {len(fundamentals_report)}")

            return fundamentals_report

        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            logger.error(f"❌ [DEBUG] get_china_fundamentals 失败:")
            logger.error(f"❌ [DEBUG] 错误: {str(e)}")
            logger.error(f"❌ [DEBUG] 堆栈: {error_details}")
            return f"中国股票基本面分析失败: {str(e)}"

    @staticmethod
    # @tool  # 已移除：请使用 get_stock_fundamentals_unified 或 get_stock_market_data_unified
    def get_hk_stock_data_unified(
        symbol: Annotated[str, "港股代码，如：0700.HK、9988.HK等"],
        start_date: Annotated[str, "开始日期，格式：YYYY-MM-DD"],
        end_date: Annotated[str, "结束日期，格式：YYYY-MM-DD"]
    ) -> str:
        """
        获取港股数据的统一接口，优先使用AKShare数据源，备用Yahoo Finance

        Args:
            symbol: 港股代码 (如: 0700.HK)
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)

        Returns:
            str: 格式化的港股数据
        """
        logger.debug(f"🇭🇰 [DEBUG] get_hk_stock_data_unified 被调用: symbol={symbol}, start_date={start_date}, end_date={end_date}")

        try:
            from tradingagents.dataflows.interface import get_hk_stock_data_unified

            result = get_hk_stock_data_unified(symbol, start_date, end_date)

            logger.debug(f"🇭🇰 [DEBUG] 港股数据获取完成，长度: {len(result) if result else 0}")

            return result

        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            logger.error(f"❌ [DEBUG] get_hk_stock_data_unified 失败:")
            logger.error(f"❌ [DEBUG] 错误: {str(e)}")
            logger.error(f"❌ [DEBUG] 堆栈: {error_details}")
            return f"港股数据获取失败: {str(e)}"

    @staticmethod
    @tool
    @log_tool_call(tool_name="get_stock_fundamentals_unified", log_args=True)
    def get_stock_fundamentals_unified(
        ticker: Annotated[str, "股票代码（支持A股、港股、美股）"],
        start_date: Annotated[str, "开始日期，格式：YYYY-MM-DD"] = None,
        end_date: Annotated[str, "结束日期，格式：YYYY-MM-DD"] = None,
        curr_date: Annotated[str, "当前日期，格式：YYYY-MM-DD"] = None
    ) -> str:
        """
        统一的股票基本面分析工具
        自动识别股票类型（A股、港股、美股）并调用相应的数据源
        支持基于分析级别的数据获取策略

        Args:
            ticker: 股票代码（如：000001、0700.HK、AAPL）
            start_date: 开始日期（可选，格式：YYYY-MM-DD）
            end_date: 结束日期（可选，格式：YYYY-MM-DD）
            curr_date: 当前日期（可选，格式：YYYY-MM-DD）

        Returns:
            str: 基本面分析数据和报告
        """
        logger.info(f"📊 [统一基本面工具] 分析股票: {ticker}")

        # 🔧 获取分析级别配置，支持基于级别的数据获取策略
        research_depth = Toolkit._config.get('research_depth', '标准')
        logger.info(f"🔧 [分析级别] 当前分析级别: {research_depth}")
        
        # 数字等级到中文等级的映射
        numeric_to_chinese = {
            1: "快速",
            2: "基础", 
            3: "标准",
            4: "深度",
            5: "全面"
        }
        
        # 标准化研究深度：支持数字输入
        if isinstance(research_depth, (int, float)):
            research_depth = int(research_depth)
            if research_depth in numeric_to_chinese:
                chinese_depth = numeric_to_chinese[research_depth]
                logger.info(f"🔢 [等级转换] 数字等级 {research_depth} → 中文等级 '{chinese_depth}'")
                research_depth = chinese_depth
            else:
                logger.warning(f"⚠️ 无效的数字等级: {research_depth}，使用默认标准分析")
                research_depth = "标准"
        elif isinstance(research_depth, str):
            # 如果是字符串形式的数字，转换为整数
            if research_depth.isdigit():
                numeric_level = int(research_depth)
                if numeric_level in numeric_to_chinese:
                    chinese_depth = numeric_to_chinese[numeric_level]
                    logger.info(f"🔢 [等级转换] 字符串数字 '{research_depth}' → 中文等级 '{chinese_depth}'")
                    research_depth = chinese_depth
                else:
                    logger.warning(f"⚠️ 无效的字符串数字等级: {research_depth}，使用默认标准分析")
                    research_depth = "标准"
            # 如果已经是中文等级，直接使用
            elif research_depth in ["快速", "基础", "标准", "深度", "全面"]:
                logger.info(f"📝 [等级确认] 使用中文等级: '{research_depth}'")
            else:
                logger.warning(f"⚠️ 未知的研究深度: {research_depth}，使用默认标准分析")
                research_depth = "标准"
        else:
            logger.warning(f"⚠️ 无效的研究深度类型: {type(research_depth)}，使用默认标准分析")
            research_depth = "标准"
        
        # 根据分析级别调整数据获取策略
        # 🔧 修正映射关系：data_depth 应该与 research_depth 保持一致
        if research_depth == "快速":
            # 快速分析：获取基础数据，减少数据源调用
            data_depth = "basic"
            logger.info(f"🔧 [分析级别] 快速分析模式：获取基础数据")
        elif research_depth == "基础":
            # 基础分析：获取标准数据
            data_depth = "standard"
            logger.info(f"🔧 [分析级别] 基础分析模式：获取标准数据")
        elif research_depth == "标准":
            # 标准分析：获取标准数据（不是full！）
            data_depth = "standard"
            logger.info(f"🔧 [分析级别] 标准分析模式：获取标准数据")
        elif research_depth == "深度":
            # 深度分析：获取完整数据
            data_depth = "full"
            logger.info(f"🔧 [分析级别] 深度分析模式：获取完整数据")
        elif research_depth == "全面":
            # 全面分析：获取最全面的数据，包含所有可用数据源
            data_depth = "comprehensive"
            logger.info(f"🔧 [分析级别] 全面分析模式：获取最全面数据")
        else:
            # 默认使用标准分析
            data_depth = "standard"
            logger.info(f"🔧 [分析级别] 未知级别，使用标准分析模式")

        # 添加详细的股票代码追踪日志
        logger.info(f"🔍 [股票代码追踪] 统一基本面工具接收到的原始股票代码: '{ticker}' (类型: {type(ticker)})")
        logger.info(f"🔍 [股票代码追踪] 股票代码长度: {len(str(ticker))}")
        logger.info(f"🔍 [股票代码追踪] 股票代码字符: {list(str(ticker))}")

        # 保存原始ticker用于对比
        original_ticker = ticker

        try:
            from tradingagents.utils.stock_utils import StockUtils
            from datetime import datetime, timedelta

            # 自动识别股票类型
            market_info = StockUtils.get_market_info(ticker)
            is_china = market_info['is_china']
            is_hk = market_info['is_hk']
            is_us = market_info['is_us']

            logger.info(f"🔍 [股票代码追踪] StockUtils.get_market_info 返回的市场信息: {market_info}")
            logger.info(f"📊 [统一基本面工具] 股票类型: {market_info['market_name']}")
            logger.info(f"📊 [统一基本面工具] 货币: {market_info['currency_name']} ({market_info['currency_symbol']})")

            # 检查ticker是否在处理过程中发生了变化
            if str(ticker) != str(original_ticker):
                logger.warning(f"🔍 [股票代码追踪] 警告：股票代码发生了变化！原始: '{original_ticker}' -> 当前: '{ticker}'")

            # 设置默认日期
            if not curr_date:
                curr_date = datetime.now().strftime('%Y-%m-%d')
        
            # 基本面分析优化：不需要大量历史数据，只需要当前价格和财务数据
            # 根据数据深度级别设置不同的分析模块数量，而非历史数据范围
            # 🔧 修正映射关系：analysis_modules 应该与 data_depth 保持一致
            if data_depth == "basic":  # 快速分析：基础模块
                analysis_modules = "basic"
                logger.info(f"📊 [基本面策略] 快速分析模式：获取基础财务指标")
            elif data_depth == "standard":  # 基础/标准分析：标准模块
                analysis_modules = "standard"
                logger.info(f"📊 [基本面策略] 标准分析模式：获取标准财务分析")
            elif data_depth == "full":  # 深度分析：完整模块
                analysis_modules = "full"
                logger.info(f"📊 [基本面策略] 深度分析模式：获取完整基本面分析")
            elif data_depth == "comprehensive":  # 全面分析：综合模块
                analysis_modules = "comprehensive"
                logger.info(f"📊 [基本面策略] 全面分析模式：获取综合基本面分析")
            else:
                analysis_modules = "standard"  # 默认标准分析
                logger.info(f"📊 [基本面策略] 默认模式：获取标准基本面分析")
            
            # 基本面分析策略：
            # 1. 获取10天数据（保证能拿到数据，处理周末/节假日）
            # 2. 只使用最近2天数据参与分析（仅需当前价格）
            days_to_fetch = 10  # 固定获取10天数据
            days_to_analyze = 2  # 只分析最近2天

            logger.info(f"📅 [基本面策略] 获取{days_to_fetch}天数据，分析最近{days_to_analyze}天")

            if not start_date:
                start_date = (datetime.now() - timedelta(days=days_to_fetch)).strftime('%Y-%m-%d')

            if not end_date:
                end_date = curr_date

            result_data = []

            if is_china:
                # 中国A股：基本面分析优化策略 - 只获取必要的当前价格和基本面数据
                logger.info(f"🇨🇳 [统一基本面工具] 处理A股数据，数据深度: {data_depth}...")
                logger.info(f"🔍 [股票代码追踪] 进入A股处理分支，ticker: '{ticker}'")
                logger.info(f"💡 [优化策略] 基本面分析只获取当前价格和财务数据，不获取历史日线数据")

                # 优化策略：基本面分析不需要大量历史日线数据
                # 只获取当前股价信息（最近1-2天即可）和基本面财务数据
                try:
                    # 获取最新股价信息（只需要最近1-2天的数据）
                    from datetime import datetime, timedelta
                    recent_end_date = curr_date
                    recent_start_date = (datetime.strptime(curr_date, '%Y-%m-%d') - timedelta(days=2)).strftime('%Y-%m-%d')

                    from tradingagents.dataflows.interface import get_china_stock_data_unified
                    logger.info(f"🔍 [股票代码追踪] 调用 get_china_stock_data_unified（仅获取最新价格），传入参数: ticker='{ticker}', start_date='{recent_start_date}', end_date='{recent_end_date}'")
                    current_price_data = get_china_stock_data_unified(ticker, recent_start_date, recent_end_date)

                    # 🔍 调试：打印返回数据的前500字符
                    logger.info(f"🔍 [基本面工具调试] A股价格数据返回长度: {len(current_price_data)}")
                    logger.info(f"🔍 [基本面工具调试] A股价格数据前500字符:\n{current_price_data[:500]}")

                    result_data.append(f"## A股当前价格信息\n{current_price_data}")
                except Exception as e:
                    logger.error(f"❌ [基本面工具调试] A股价格数据获取失败: {e}")
                    result_data.append(f"## A股当前价格信息\n获取失败: {e}")
                    current_price_data = ""

                try:
                    # 获取基本面财务数据（这是基本面分析的核心）
                    from tradingagents.dataflows.optimized_china_data import OptimizedChinaDataProvider
                    analyzer = OptimizedChinaDataProvider()
                    logger.info(f"🔍 [股票代码追踪] 调用 OptimizedChinaDataProvider._generate_fundamentals_report，传入参数: ticker='{ticker}', analysis_modules='{analysis_modules}'")

                    # 传递分析模块参数到基本面分析方法
                    fundamentals_data = analyzer._generate_fundamentals_report(ticker, current_price_data, analysis_modules)

                    # 🔍 调试：打印返回数据的前500字符
                    logger.info(f"🔍 [基本面工具调试] A股基本面数据返回长度: {len(fundamentals_data)}")
                    logger.info(f"🔍 [基本面工具调试] A股基本面数据前500字符:\n{fundamentals_data[:500]}")

                    result_data.append(f"## A股基本面财务数据\n{fundamentals_data}")
                except Exception as e:
                    logger.error(f"❌ [基本面工具调试] A股基本面数据获取失败: {e}")
                    result_data.append(f"## A股基本面财务数据\n获取失败: {e}")

            elif is_hk:
                # 港股：使用AKShare数据源，支持多重备用方案
                logger.info(f"🇭🇰 [统一基本面工具] 处理港股数据，数据深度: {data_depth}...")

                hk_data_success = False

                # 🔥 统一策略：所有级别都获取完整数据
                # 原因：提示词是统一的，如果数据不完整会导致LLM基于不存在的数据进行分析（幻觉）
                logger.info(f"🔍 [港股基本面] 统一策略：获取完整数据（忽略 data_depth 参数）")

                # 主要数据源：AKShare
                try:
                    from tradingagents.dataflows.interface import get_hk_stock_data_unified
                    hk_data = get_hk_stock_data_unified(ticker, start_date, end_date)

                    # 🔍 调试：打印返回数据的前500字符
                    logger.info(f"🔍 [基本面工具调试] 港股数据返回长度: {len(hk_data)}")
                    logger.info(f"🔍 [基本面工具调试] 港股数据前500字符:\n{hk_data[:500]}")

                    # 检查数据质量
                    if hk_data and len(hk_data) > 100 and "❌" not in hk_data:
                        result_data.append(f"## 港股数据\n{hk_data}")
                        hk_data_success = True
                        logger.info(f"✅ [统一基本面工具] 港股主要数据源成功")
                    else:
                        logger.warning(f"⚠️ [统一基本面工具] 港股主要数据源质量不佳")

                except Exception as e:
                    logger.error(f"❌ [基本面工具调试] 港股数据获取失败: {e}")

                # 备用方案：基础港股信息
                if not hk_data_success:
                    try:
                        from tradingagents.dataflows.interface import get_hk_stock_info_unified
                        hk_info = get_hk_stock_info_unified(ticker)

                        basic_info = f"""## 港股基础信息

**股票代码**: {ticker}
**股票名称**: {hk_info.get('name', f'港股{ticker}')}
**交易货币**: 港币 (HK$)
**交易所**: 香港交易所 (HKG)
**数据源**: {hk_info.get('source', '基础信息')}

⚠️ 注意：详细的价格和财务数据暂时无法获取，建议稍后重试或使用其他数据源。

**基本面分析建议**：
- 建议查看公司最新财报
- 关注港股市场整体走势
- 考虑汇率因素对投资的影响
"""
                        result_data.append(basic_info)
                        logger.info(f"✅ [统一基本面工具] 港股备用信息成功")

                    except Exception as e2:
                        # 最终备用方案
                        fallback_info = f"""## 港股信息（备用）

**股票代码**: {ticker}
**股票类型**: 港股
**交易货币**: 港币 (HK$)
**交易所**: 香港交易所 (HKG)

❌ 数据获取遇到问题: {str(e2)}

**建议**：
- 请稍后重试
- 或使用其他数据源
- 检查股票代码格式是否正确
"""
                        result_data.append(fallback_info)
                        logger.error(f"❌ [统一基本面工具] 港股所有数据源都失败: {e2}")

            else:
                # 美股：使用OpenAI/Finnhub数据源
                logger.info(f"🇺🇸 [统一基本面工具] 处理美股数据...")

                # 🔥 统一策略：所有级别都获取完整数据
                # 原因：提示词是统一的，如果数据不完整会导致LLM基于不存在的数据进行分析（幻觉）
                logger.info(f"🔍 [美股基本面] 统一策略：获取完整数据（忽略 data_depth 参数）")

                try:
                    from tradingagents.dataflows.interface import get_fundamentals_openai
                    us_data = get_fundamentals_openai(ticker, curr_date)
                    result_data.append(f"## 美股基本面数据\n{us_data}")
                    logger.info(f"✅ [统一基本面工具] 美股数据获取成功")
                except Exception as e:
                    result_data.append(f"## 美股基本面数据\n获取失败: {e}")
                    logger.error(f"❌ [统一基本面工具] 美股数据获取失败: {e}")

            # 组合所有数据
            combined_result = f"""# {ticker} 基本面分析数据

**股票类型**: {market_info['market_name']}
**货币**: {market_info['currency_name']} ({market_info['currency_symbol']})
**分析日期**: {curr_date}
**数据深度级别**: {data_depth}

{chr(10).join(result_data)}

---
*数据来源: 根据股票类型自动选择最适合的数据源*
"""

            # 添加详细的数据获取日志
            logger.info(f"📊 [统一基本面工具] ===== 数据获取完成摘要 =====")
            logger.info(f"📊 [统一基本面工具] 股票代码: {ticker}")
            logger.info(f"📊 [统一基本面工具] 股票类型: {market_info['market_name']}")
            logger.info(f"📊 [统一基本面工具] 数据深度级别: {data_depth}")
            logger.info(f"📊 [统一基本面工具] 获取的数据模块数量: {len(result_data)}")
            logger.info(f"📊 [统一基本面工具] 总数据长度: {len(combined_result)} 字符")
            
            # 记录每个数据模块的详细信息
            for i, data_section in enumerate(result_data, 1):
                section_lines = data_section.split('\n')
                section_title = section_lines[0] if section_lines else "未知模块"
                section_length = len(data_section)
                logger.info(f"📊 [统一基本面工具] 数据模块 {i}: {section_title} ({section_length} 字符)")
                
                # 如果数据包含错误信息，特别标记
                if "获取失败" in data_section or "❌" in data_section:
                    logger.warning(f"⚠️ [统一基本面工具] 数据模块 {i} 包含错误信息")
                else:
                    logger.info(f"✅ [统一基本面工具] 数据模块 {i} 获取成功")
            
            # 根据数据深度级别记录具体的获取策略
            if data_depth in ["basic", "standard"]:
                logger.info(f"📊 [统一基本面工具] 基础/标准级别策略: 仅获取核心价格数据和基础信息")
            elif data_depth in ["full", "detailed", "comprehensive"]:
                logger.info(f"📊 [统一基本面工具] 完整/详细/全面级别策略: 获取价格数据 + 基本面数据")
            else:
                logger.info(f"📊 [统一基本面工具] 默认策略: 获取完整数据")
            
            logger.info(f"📊 [统一基本面工具] ===== 数据获取摘要结束 =====")
            
            return combined_result

        except Exception as e:
            error_msg = f"统一基本面分析工具执行失败: {str(e)}"
            logger.error(f"❌ [统一基本面工具] {error_msg}")
            return error_msg

    @staticmethod
    @tool
    @log_tool_call(tool_name="get_stock_market_data_unified", log_args=True)
    def get_stock_market_data_unified(
        ticker: Annotated[str, "股票代码（支持A股、港股、美股）"],
        start_date: Annotated[str, "开始日期，格式：YYYY-MM-DD。注意：系统会自动扩展到配置的回溯天数（通常为365天），你只需要传递分析日期即可"],
        end_date: Annotated[str, "结束日期，格式：YYYY-MM-DD。通常与start_date相同，传递当前分析日期即可"]
    ) -> str:
        """
        统一的股票市场数据工具
        自动识别股票类型（A股、港股、美股）并调用相应的数据源获取价格和技术指标数据

        ⚠️ 重要：系统会自动扩展日期范围到配置的回溯天数（通常为365天），以确保技术指标计算有足够的历史数据。
        你只需要传递当前分析日期作为 start_date 和 end_date 即可，无需手动计算历史日期范围。

        Args:
            ticker: 股票代码（如：000001、0700.HK、AAPL）
            start_date: 开始日期（格式：YYYY-MM-DD）。传递当前分析日期即可，系统会自动扩展
            end_date: 结束日期（格式：YYYY-MM-DD）。传递当前分析日期即可

        Returns:
            str: 市场数据和技术分析报告

        示例：
            如果分析日期是 2025-11-09，传递：
            - ticker: "00700.HK"
            - start_date: "2025-11-09"
            - end_date: "2025-11-09"
            系统会自动获取 2024-11-09 到 2025-11-09 的365天历史数据
        """
        logger.info(f"📈 [统一市场工具] 分析股票: {ticker}")

        try:
            from tradingagents.utils.stock_utils import StockUtils

            # 自动识别股票类型
            market_info = StockUtils.get_market_info(ticker)
            is_china = market_info['is_china']
            is_hk = market_info['is_hk']
            is_us = market_info['is_us']

            logger.info(f"📈 [统一市场工具] 股票类型: {market_info['market_name']}")
            logger.info(f"📈 [统一市场工具] 货币: {market_info['currency_name']} ({market_info['currency_symbol']}")

            result_data = []

            # 三市场统一入口：内部按市场与配置优先级（LongPort优先）处理
            try:
                from tradingagents.dataflows.interface import get_stock_data_by_market
                market_data = get_stock_data_by_market(ticker, start_date, end_date)

                logger.info(f"🔍 [市场工具调试] 返回长度: {len(market_data)}")
                logger.info(f"🔍 [市场工具调试] 前500字符:\n{market_data[:500]}")

                section_title = "A股市场数据" if is_china else ("港股市场数据" if is_hk else "美股市场数据")
                result_data.append(f"## {section_title}\n{market_data}")
            except Exception as e:
                section_title = "A股市场数据" if is_china else ("港股市场数据" if is_hk else "美股市场数据")
                result_data.append(f"## {section_title}\n获取失败: {e}")

            # 组合所有数据
            combined_result = f"""# {ticker} 市场数据分析

**股票类型**: {market_info['market_name']}
**货币**: {market_info['currency_name']} ({market_info['currency_symbol']})
**分析期间**: {start_date} 至 {end_date}

{chr(10).join(result_data)}

---
*数据来源: 根据股票类型自动选择最适合的数据源*
"""

            logger.info(f"📈 [统一市场工具] 数据获取完成，总长度: {len(combined_result)}")
            return combined_result

        except Exception as e:
            error_msg = f"统一市场数据工具执行失败: {str(e)}"
            logger.error(f"❌ [统一市场工具] {error_msg}")
            return error_msg

    @staticmethod
    @tool
    @log_tool_call(tool_name="get_stock_news_unified", log_args=True)
    def get_stock_news_unified(
        ticker: Annotated[str, "股票代码（支持A股、港股、美股）"],
        curr_date: Annotated[str, "当前日期，格式：YYYY-MM-DD"]
    ) -> str:
        """
        统一的股票新闻工具
        自动识别股票类型（A股、港股、美股）并调用相应的新闻数据源

        Args:
            ticker: 股票代码（如：000001、0700.HK、AAPL）
            curr_date: 当前日期（格式：YYYY-MM-DD）

        Returns:
            str: 新闻分析报告
        """
        logger.info(f"📰 [统一新闻工具] 分析股票: {ticker}")

        try:
            from tradingagents.utils.stock_utils import StockUtils
            from datetime import datetime, timedelta

            # 自动识别股票类型
            market_info = StockUtils.get_market_info(ticker)
            is_china = market_info['is_china']
            is_hk = market_info['is_hk']
            is_us = market_info['is_us']

            logger.info(f"📰 [统一新闻工具] 股票类型: {market_info['market_name']}")

            # 计算新闻查询的日期范围
            end_date = datetime.strptime(curr_date, '%Y-%m-%d')
            start_date = end_date - timedelta(days=7)
            start_date_str = start_date.strftime('%Y-%m-%d')

            result_data = []

            if is_china or is_hk:
                # 中国A股和港股：使用AKShare东方财富新闻和Google新闻（中文搜索）
                logger.info(f"🇨🇳🇭🇰 [统一新闻工具] 处理中文新闻...")

                # 1. 尝试获取AKShare东方财富新闻
                try:
                    # 处理股票代码
                    clean_ticker = ticker.replace('.SH', '').replace('.SZ', '').replace('.SS', '')\
                                   .replace('.HK', '').replace('.XSHE', '').replace('.XSHG', '')
                    
                    logger.info(f"🇨🇳🇭🇰 [统一新闻工具] 尝试获取东方财富新闻: {clean_ticker}")

                    # 通过 AKShare Provider 获取新闻
                    from tradingagents.dataflows.providers.china.akshare import AKShareProvider

                    provider = AKShareProvider()

                    # 获取东方财富新闻
                    news_df = provider.get_stock_news_sync(symbol=clean_ticker)

                    if news_df is not None and not news_df.empty:
                        # 格式化东方财富新闻
                        em_news_items = []
                        for _, row in news_df.iterrows():
                            # AKShare 返回的字段名
                            news_title = row.get('新闻标题', '') or row.get('标题', '')
                            news_time = row.get('发布时间', '') or row.get('时间', '')
                            news_url = row.get('新闻链接', '') or row.get('链接', '')

                            news_item = f"- **{news_title}** [{news_time}]({news_url})"
                            em_news_items.append(news_item)
                        
                        # 添加到结果中
                        if em_news_items:
                            em_news_text = "\n".join(em_news_items)
                            result_data.append(f"## 东方财富新闻\n{em_news_text}")
                            logger.info(f"🇨🇳🇭🇰 [统一新闻工具] 成功获取{len(em_news_items)}条东方财富新闻")
                except Exception as em_e:
                    logger.error(f"❌ [统一新闻工具] 东方财富新闻获取失败: {em_e}")
                    result_data.append(f"## 东方财富新闻\n获取失败: {em_e}")

                # 2. 获取Google新闻作为补充
                try:
                    # 获取公司中文名称用于搜索
                    if is_china:
                        # A股使用股票代码搜索，添加更多中文关键词
                        clean_ticker = ticker.replace('.SH', '').replace('.SZ', '').replace('.SS', '')\
                                       .replace('.XSHE', '').replace('.XSHG', '')
                        search_query = f"{clean_ticker} 股票 公司 财报 新闻"
                        logger.info(f"🇨🇳 [统一新闻工具] A股Google新闻搜索关键词: {search_query}")
                    else:
                        # 港股使用代码搜索
                        search_query = f"{ticker} 港股"
                        logger.info(f"🇭🇰 [统一新闻工具] 港股Google新闻搜索关键词: {search_query}")

                    from tradingagents.dataflows.interface import get_google_news
                    news_data = get_google_news(search_query, curr_date)
                    result_data.append(f"## Google新闻\n{news_data}")
                    logger.info(f"🇨🇳🇭🇰 [统一新闻工具] 成功获取Google新闻")
                except Exception as google_e:
                    logger.error(f"❌ [统一新闻工具] Google新闻获取失败: {google_e}")
                    result_data.append(f"## Google新闻\n获取失败: {google_e}")

            else:
                # 美股：使用Finnhub新闻
                logger.info(f"🇺🇸 [统一新闻工具] 处理美股新闻...")

                try:
                    from tradingagents.dataflows.interface import get_finnhub_news
                    news_data = get_finnhub_news(ticker, start_date_str, curr_date)
                    result_data.append(f"## 美股新闻\n{news_data}")
                except Exception as e:
                    result_data.append(f"## 美股新闻\n获取失败: {e}")

            # 组合所有数据
            combined_result = f"""# {ticker} 新闻分析

**股票类型**: {market_info['market_name']}
**分析日期**: {curr_date}
**新闻时间范围**: {start_date_str} 至 {curr_date}

{chr(10).join(result_data)}

---
*数据来源: 根据股票类型自动选择最适合的新闻源*
"""

            logger.info(f"📰 [统一新闻工具] 数据获取完成，总长度: {len(combined_result)}")
            return combined_result

        except Exception as e:
            error_msg = f"统一新闻工具执行失败: {str(e)}"
            logger.error(f"❌ [统一新闻工具] {error_msg}")
            return error_msg

    @staticmethod
    @tool
    @log_tool_call(tool_name="get_stock_sentiment_unified", log_args=True)
    def get_stock_sentiment_unified(
        ticker: Annotated[str, "股票代码（支持A股、港股、美股）"],
        curr_date: Annotated[str, "当前日期，格式：YYYY-MM-DD"]
    ) -> str:
        """
        统一的股票情绪分析工具
        自动识别股票类型（A股、港股、美股）并调用相应的情绪数据源

        Args:
            ticker: 股票代码（如：000001、0700.HK、AAPL）
            curr_date: 当前日期（格式：YYYY-MM-DD）

        Returns:
            str: 情绪分析报告
        """
        logger.info(f"😊 [统一情绪工具] 分析股票: {ticker}")

        try:
            from tradingagents.utils.stock_utils import StockUtils

            # 自动识别股票类型
            market_info = StockUtils.get_market_info(ticker)
            is_china = market_info['is_china']
            is_hk = market_info['is_hk']
            is_us = market_info['is_us']

            logger.info(f"😊 [统一情绪工具] 股票类型: {market_info['market_name']}")

            result_data = []

            if is_china or is_hk:
                logger.info(f"🇨🇳🇭🇰 [统一情绪工具] 处理中文市场情绪...")
                import asyncio
                import re
                import threading

                positive_keywords = [
                    "利好", "上涨", "增长", "盈利", "突破", "创新高", "买入", "推荐",
                    "看好", "强势", "超预期", "中标", "签约", "合作", "分红", "回购"
                ]
                negative_keywords = [
                    "利空", "下跌", "亏损", "风险", "暴跌", "卖出", "警告", "下调",
                    "看空", "弱势", "低于预期", "减持", "商誉减值", "诉讼", "停牌", "退市"
                ]
                risk_keywords = [
                    "立案", "调查", "处罚", "退市", "暴跌", "跌停", "违约", "减值", "停牌"
                ]

                def _normalize_symbol_for_cn(source_name: str, raw_ticker: str) -> str:
                    symbol = str(raw_ticker).upper()
                    symbol = symbol.replace(".SH", "").replace(".SZ", "").replace(".SS", "")
                    symbol = symbol.replace(".XSHG", "").replace(".XSHE", "")
                    symbol = symbol.replace(".HK", "").replace(".HKG", "")
                    symbol = symbol.strip()
                    if source_name == "akshare" and symbol.isdigit():
                        return symbol.zfill(6 if len(symbol) <= 6 else len(symbol))
                    if source_name == "tushare" and symbol.isdigit() and len(symbol) <= 6:
                        return symbol.zfill(6)
                    return symbol

                def _score_text(text: str) -> float:
                    t = str(text or "").lower()
                    if not t:
                        return 0.0
                    pos = sum(1 for k in positive_keywords if k in t)
                    neg = sum(1 for k in negative_keywords if k in t)
                    if pos + neg == 0:
                        return 0.0
                    return max(-1.0, min(1.0, (pos - neg) / (pos + neg)))

                def _label_from_score(score: float) -> str:
                    if score > 0.25:
                        return "积极"
                    if score > 0.08:
                        return "偏积极"
                    if score < -0.25:
                        return "消极"
                    if score < -0.08:
                        return "偏消极"
                    return "中性"

                def _heat_from_count(count: int) -> str:
                    if count >= 30:
                        return "极高"
                    if count >= 15:
                        return "高"
                    if count >= 8:
                        return "中"
                    return "低"

                def _confidence_from_count(count: int, source_count: int, ai_used: bool) -> int:
                    base = 25 + min(45, count * 3)
                    source_bonus = min(20, max(0, source_count - 1) * 8)
                    ai_penalty = 8 if ai_used else 0
                    return int(max(5, min(95, base + source_bonus - ai_penalty)))

                def _trend_hint(scores: list[float]) -> str:
                    if len(scores) < 6:
                        return "样本不足"
                    recent = scores[:5]
                    previous = scores[5:10]
                    if not previous:
                        return "样本不足"
                    recent_avg = sum(recent) / len(recent)
                    prev_avg = sum(previous) / len(previous)
                    delta = recent_avg - prev_avg
                    if delta > 0.08:
                        return "升温"
                    if delta < -0.08:
                        return "降温"
                    return "持平"

                def _looks_unavailable_search_text(text: str) -> bool:
                    t = str(text or "").strip()
                    if not t:
                        return True
                    patterns = [
                        "我目前无法", "目前我无法", "无法进行实时搜索", "无法为您获取",
                        "无法直接搜索", "无法搜索社交媒体", "无法搜索", "请您自行",
                        "建议您自行", "联网搜索插件未开通", "ToolNotOpen", "web search"
                    ]
                    return any(p in t for p in patterns)

                def _normalize_news_item(item: dict, source_name: str) -> dict:
                    title = str(item.get("title", "")).strip()
                    content = str(item.get("content", "") or item.get("summary", "")).strip()
                    publish_time = str(item.get("publish_time", "")).strip()
                    source = str(item.get("source", "") or source_name).strip()
                    raw_score = item.get("sentiment_score")
                    keyword_score = _score_text(f"{title} {content}")
                    if raw_score is None:
                        provider_score = None
                        score = keyword_score
                    else:
                        try:
                            provider_score = float(raw_score)
                            score = provider_score * 0.65 + keyword_score * 0.35
                        except Exception:
                            provider_score = None
                            score = keyword_score
                    score = max(-1.0, min(1.0, score))
                    return {
                        "title": title or "无标题",
                        "content": content,
                        "publish_time": publish_time or "未知时间",
                        "source": source or source_name,
                        "sentiment_score": score,
                        "keyword_score": keyword_score,
                        "provider_score": provider_score,
                    }

                def _extract_json_block(text: str) -> str:
                    raw = str(text or "").strip()
                    if not raw:
                        return ""
                    start = raw.find("{")
                    end = raw.rfind("}")
                    if start != -1 and end != -1 and end > start:
                        return raw[start:end + 1]
                    return raw

                def _llm_score_news_batch(items: list[dict]) -> dict:
                    if not items:
                        return {"used": False, "scores": [], "note": "无样本"}
                    try:
                        from tradingagents.dataflows.cache import get_cache

                        cfg = interface.get_config()
                        model_name = cfg.get("quick_think_llm")
                        if not model_name:
                            return {"used": False, "scores": [], "note": "未配置模型"}
                        payload_items = []
                        for idx, x in enumerate(items[:15], start=1):
                            payload_items.append(
                                {
                                    "id": idx,
                                    "title": str(x.get("title", ""))[:200],
                                    "content": str(x.get("content", ""))[:500],
                                    "source": str(x.get("source", ""))[:50],
                                }
                            )

                        signature_items = []
                        for x in payload_items:
                            signature_items.append(
                                {
                                    "title": str(x.get("title", "")).strip(),
                                    "source": str(x.get("source", "")).strip(),
                                    "date": str(items[x["id"] - 1].get("publish_time", "")).strip()
                                }
                            )
                        signature_items = sorted(
                            signature_items, key=lambda d: (d.get("title", ""), d.get("source", ""), d.get("date", ""))
                        )
                        payload_signature = json.dumps(
                            signature_items, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                        )
                        payload_hash = hashlib.md5(payload_signature.encode("utf-8")).hexdigest()[:16]
                        cache_source = f"sentiment_llm_{payload_hash}"
                        cache = get_cache()

                        try:
                            cached_key = cache.find_cached_stock_data(
                                symbol=ticker,
                                start_date=curr_date,
                                end_date=curr_date,
                                data_source=cache_source,
                            )
                            if cached_key:
                                cached_raw = cache.load_stock_data(cached_key)
                                if isinstance(cached_raw, str) and cached_raw.strip():
                                    cached_obj = json.loads(cached_raw)
                                    cached_scores = cached_obj.get("scores", [])
                                    if isinstance(cached_scores, list):
                                        return {
                                            "used": any(x is not None for x in cached_scores),
                                            "scores": cached_scores,
                                            "summary": str(cached_obj.get("summary", "")),
                                            "note": "LLM评分缓存命中",
                                        }
                        except Exception:
                            pass

                        client = interface._build_openai_compatible_client(cfg, scene="sentiment_scoring")
                        prompt = (
                            "你是A股/港股舆情情绪分析器。"
                            "请对每条新闻输出情绪分数score（-1到1，保留2位小数），并给整体说明。"
                            "严格返回JSON，不要markdown。\n"
                            "JSON格式: "
                            "{\"scores\":[{\"id\":1,\"score\":0.1}],\"summary\":\"...\"}\n"
                            f"新闻样本: {payload_items}"
                        )
                        response = client.chat.completions.create(
                            model=model_name,
                            messages=[
                                {"role": "system", "content": "你是严谨的金融文本情绪分类器。"},
                                {"role": "user", "content": prompt},
                            ],
                            temperature=0.1,
                            max_tokens=900,
                        )
                        text = interface._extract_response_text(response)
                        json_text = _extract_json_block(text)
                        parsed = json.loads(json_text) if json_text else {}
                        score_items = parsed.get("scores", []) if isinstance(parsed, dict) else []
                        score_map = {}
                        for row in score_items:
                            try:
                                rid = int(row.get("id"))
                                val = float(row.get("score"))
                                score_map[rid] = max(-1.0, min(1.0, val))
                            except Exception:
                                continue

                        aligned_scores = []
                        for idx in range(1, len(payload_items) + 1):
                            aligned_scores.append(score_map.get(idx))

                        try:
                            cache_payload = {
                                "scores": aligned_scores,
                                "summary": str(parsed.get("summary", "")) if isinstance(parsed, dict) else "",
                                "created_at": datetime.utcnow().isoformat(),
                                "payload_hash": payload_hash,
                                "model": model_name,
                            }
                            cache.save_stock_data(
                                symbol=ticker,
                                data=json.dumps(cache_payload, ensure_ascii=False),
                                start_date=curr_date,
                                end_date=curr_date,
                                data_source=cache_source,
                            )
                        except Exception:
                            pass

                        return {
                            "used": any(x is not None for x in aligned_scores),
                            "scores": aligned_scores,
                            "summary": str(parsed.get("summary", "")) if isinstance(parsed, dict) else "",
                            "note": "LLM评分完成(已写缓存)",
                        }
                    except Exception as e:
                        return {"used": False, "scores": [], "note": f"LLM评分失败: {e}"}

                def _extract_ai_news_items(ai_text: str) -> list[dict]:
                    items = []
                    lines = [line.strip() for line in str(ai_text or "").splitlines() if line.strip()]
                    title_pattern = re.compile(r"^(?:[-*]\s*)?(?:\d+[.)、]\s*)?(?:标题[:：]\s*)?(.+)$")
                    date_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})")
                    source_pattern = re.compile(r"(?:来源|source)[:：]\s*([^\]|）)]+)", re.IGNORECASE)
                    for line in lines:
                        if len(line) < 8:
                            continue
                        if all(token not in line for token in ["来源", "source", "-", "：", ":"]) and len(line) > 120:
                            continue
                        match = title_pattern.match(line)
                        if not match:
                            continue
                        title = match.group(1).strip()
                        if not title or len(title) < 6:
                            continue
                        date_match = date_pattern.search(line)
                        source_match = source_pattern.search(line)
                        items.append(
                            {
                                "title": title[:180],
                                "content": line[:500],
                                "publish_time": date_match.group(1) if date_match else "未知时间",
                                "source": source_match.group(1).strip() if source_match else "AI联网搜索",
                                "sentiment_score": _score_text(line),
                            }
                        )
                        if len(items) >= 20:
                            break
                    return items

                def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
                    return max(low, min(high, value))

                def _to_float(value) -> float:
                    try:
                        if value is None:
                            return 0.0
                        return float(value)
                    except Exception:
                        return 0.0

                def _compute_behavior_factors(price_df) -> dict:
                    if price_df is None or len(price_df) < 6:
                        return {}

                    try:
                        local_df = price_df.copy()
                        if "date" in local_df.columns:
                            local_df = local_df.sort_values("date")
                        else:
                            local_df = local_df.sort_index()

                        closes = [_to_float(v) for v in local_df.get("close", pd.Series(dtype=float)).tolist() if _to_float(v) > 0]
                        volumes = [_to_float(v) for v in local_df.get("volume", pd.Series(dtype=float)).tolist()]

                        if len(closes) < 6:
                            return {}

                        latest_close = closes[-1]
                        prev_close = closes[-2]
                        close_5 = closes[-6] if len(closes) >= 6 else closes[0]

                        ret_1d = (latest_close / prev_close - 1.0) if prev_close > 0 else 0.0
                        ret_5d = (latest_close / close_5 - 1.0) if close_5 > 0 else 0.0

                        latest_volume = volumes[-1] if volumes else 0.0
                        recent_volumes = volumes[-6:-1] if len(volumes) >= 6 else volumes[:-1]
                        avg_volume_5 = (sum(recent_volumes) / len(recent_volumes)) if recent_volumes else 0.0
                        volume_ratio = (latest_volume / avg_volume_5) if avg_volume_5 > 0 else 1.0

                        returns = []
                        for idx in range(1, len(closes)):
                            prev = closes[idx - 1]
                            curr = closes[idx]
                            if prev > 0:
                                returns.append(curr / prev - 1.0)
                        recent_returns = returns[-10:] if len(returns) >= 10 else returns
                        volatility = float(pd.Series(recent_returns).std()) if recent_returns else 0.0

                        price_momentum_index = _clamp(50.0 + ret_5d * 500.0)
                        volume_impulse_index = _clamp(50.0 + (volume_ratio - 1.0) * 40.0)
                        volatility_pressure = _clamp(volatility * 1200.0)

                        behavior_risk_appetite = _clamp(
                            0.45 * price_momentum_index
                            + 0.35 * volume_impulse_index
                            + 0.20 * (100.0 - volatility_pressure)
                        )

                        if behavior_risk_appetite >= 65:
                            behavior_label = "risk_on"
                        elif behavior_risk_appetite <= 35:
                            behavior_label = "risk_off"
                        else:
                            behavior_label = "neutral"

                        return {
                            "price_momentum_index": int(round(price_momentum_index)),
                            "volume_impulse_index": int(round(volume_impulse_index)),
                            "volatility_pressure": int(round(volatility_pressure)),
                            "behavior_risk_appetite": int(round(behavior_risk_appetite)),
                            "behavior_label": behavior_label,
                            "ret_1d": ret_1d,
                            "ret_5d": ret_5d,
                            "volume_ratio": volume_ratio,
                        }
                    except Exception:
                        return {}

                def _compute_theme_dispersion(news_data: list[dict]) -> dict:
                    if not news_data:
                        return {
                            "theme_dispersion_index": 0,
                            "theme_entropy": 0.0,
                            "theme_balance": 0.0,
                            "dominant_themes": "无",
                            "theme_polarity_spread": 0.0,
                            "theme_count": 0,
                        }

                    theme_keywords = {
                        "政策监管": ["政策", "监管", "证监会", "央行", "国务院", "规则", "指导意见"],
                        "业绩基本面": ["业绩", "财报", "营收", "净利润", "分红", "回购", "估值"],
                        "资金交易": ["资金", "北向", "成交量", "换手", "增持", "减持", "主力"],
                        "行业景气": ["行业", "景气", "产业", "需求", "供给", "产能", "订单"],
                        "风险事件": ["风险", "诉讼", "处罚", "违约", "立案", "停牌", "退市", "减值"],
                    }
                    buckets = {k: {"scores": [], "count": 0} for k in theme_keywords.keys()}
                    buckets["其他"] = {"scores": [], "count": 0}

                    for item in news_data:
                        text = f"{item.get('title', '')} {item.get('content', '')}"
                        score = float(item.get("sentiment_score", 0.0))
                        matched_theme = None
                        for theme_name, kws in theme_keywords.items():
                            if any(k in text for k in kws):
                                matched_theme = theme_name
                                break
                        if not matched_theme:
                            matched_theme = "其他"
                        buckets[matched_theme]["scores"].append(score)
                        buckets[matched_theme]["count"] += 1

                    active_themes = {k: v for k, v in buckets.items() if v["count"] > 0}
                    total = sum(v["count"] for v in active_themes.values())
                    if total <= 0:
                        return {
                            "theme_dispersion_index": 0,
                            "theme_entropy": 0.0,
                            "theme_balance": 0.0,
                            "dominant_themes": "无",
                            "theme_polarity_spread": 0.0,
                            "theme_count": 0,
                        }

                    import math
                    theme_avg_scores = []
                    entropy_raw = 0.0
                    max_ratio = 0.0
                    ranked = sorted(active_themes.items(), key=lambda x: x[1]["count"], reverse=True)
                    for _, info in ranked:
                        ratio = info["count"] / total
                        max_ratio = max(max_ratio, ratio)
                        entropy_raw += -(ratio * math.log(ratio)) if ratio > 0 else 0.0
                        avg_score = sum(info["scores"]) / len(info["scores"]) if info["scores"] else 0.0
                        theme_avg_scores.append(avg_score)

                    theme_count = len(active_themes)
                    entropy_norm = 0.0
                    if theme_count > 1:
                        entropy_norm = entropy_raw / math.log(theme_count)
                    theme_balance = max(0.0, 1.0 - max_ratio)
                    polarity_spread = max(theme_avg_scores) - min(theme_avg_scores) if len(theme_avg_scores) > 1 else 0.0

                    theme_dispersion_index = _clamp(
                        polarity_spread * 55.0 + entropy_norm * 30.0 + theme_balance * 25.0
                    )
                    dominant = " / ".join([x[0] for x in ranked[:2]])

                    return {
                        "theme_dispersion_index": int(round(theme_dispersion_index)),
                        "theme_entropy": round(entropy_norm, 3),
                        "theme_balance": round(theme_balance, 3),
                        "dominant_themes": dominant,
                        "theme_polarity_spread": round(polarity_spread, 3),
                        "theme_count": theme_count,
                    }

                def _run_async(coro_factory):
                    try:
                        return asyncio.run(coro_factory())
                    except RuntimeError as rt_err:
                        if "running event loop" not in str(rt_err).lower():
                            raise
                        holder = {"value": None, "error": None}

                        def _runner():
                            loop = asyncio.new_event_loop()
                            try:
                                asyncio.set_event_loop(loop)
                                holder["value"] = loop.run_until_complete(coro_factory())
                            except Exception as e:
                                holder["error"] = e
                            finally:
                                loop.close()

                        th = threading.Thread(target=_runner, daemon=True)
                        th.start()
                        th.join(timeout=45)
                        if th.is_alive():
                            return None
                        if holder["error"] is not None:
                            raise holder["error"]
                        return holder["value"]

                news_items = []
                used_sources = []
                data_quality_notes = []
                source_errors = []
                ai_fallback_used = False
                llm_sentiment_summary = ""
                llm_notes = []

                has_tushare_token = bool(os.getenv("TUSHARE_TOKEN", "").strip())
                source_priority = ["tushare", "akshare"] if has_tushare_token else ["akshare", "tushare"]
                data_quality_notes.append(
                    f"TUSHARE_TOKEN检测: {'已配置' if has_tushare_token else '未配置'}，优先级: {' > '.join(source_priority)}"
                )
                behavior_factors = {}
                behavior_source = ""
                behavior_notes = []

                try:
                    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
                    start_dt = end_dt - timedelta(days=35)
                    start_date_behavior = start_dt.strftime("%Y-%m-%d")
                except Exception:
                    start_date_behavior = curr_date

                for source_name in source_priority:
                    if behavior_factors:
                        break
                    try:
                        if source_name == "tushare":
                            from tradingagents.dataflows.providers.china.tushare import get_tushare_provider

                            tushare_provider = get_tushare_provider()
                            if not tushare_provider or not tushare_provider.is_available():
                                behavior_notes.append("行为因子: Tushare不可用")
                                continue
                            price_df = _run_async(
                                lambda: tushare_provider.get_historical_data(
                                    symbol=ticker,
                                    start_date=start_date_behavior,
                                    end_date=curr_date,
                                    period="daily",
                                )
                            )
                            behavior_factors = _compute_behavior_factors(price_df)
                            if behavior_factors:
                                behavior_source = "Tushare"
                            else:
                                behavior_notes.append("行为因子: Tushare行情样本不足")
                        else:
                            from tradingagents.dataflows.providers.china.akshare import AKShareProvider

                            ak_provider = AKShareProvider()
                            ak_symbol = _normalize_symbol_for_cn("akshare", ticker)
                            price_df = _run_async(
                                lambda: ak_provider.get_historical_data(
                                    code=ak_symbol,
                                    start_date=start_date_behavior,
                                    end_date=curr_date,
                                    period="daily",
                                )
                            )
                            behavior_factors = _compute_behavior_factors(price_df)
                            if behavior_factors:
                                behavior_source = "AKShare"
                            else:
                                behavior_notes.append("行为因子: AKShare行情样本不足")
                    except Exception as behavior_error:
                        behavior_notes.append(f"行为因子: {source_name} 获取失败({behavior_error})")

                for source_name in source_priority:
                    if news_items:
                        break
                    try:
                        if source_name == "tushare":
                            from tradingagents.dataflows.providers.china.tushare import get_tushare_provider

                            tushare_provider = get_tushare_provider()
                            if not tushare_provider or not tushare_provider.is_available():
                                data_quality_notes.append("Tushare不可用或未完成连接，跳过。")
                                continue

                            ts_symbol = _normalize_symbol_for_cn("tushare", ticker)
                            raw_news = _run_async(
                                lambda: tushare_provider.get_stock_news(
                                    symbol=ts_symbol, limit=20, hours_back=168
                                )
                            )
                            raw_news = raw_news or []
                            normalized = [_normalize_news_item(item, "Tushare") for item in raw_news if isinstance(item, dict)]
                            if normalized:
                                news_items = normalized
                                used_sources.append("Tushare")
                                logger.info(f"✅ [统一情绪工具] Tushare命中新闻: {len(news_items)}")
                            else:
                                data_quality_notes.append("Tushare返回空新闻。")
                        else:
                            from tradingagents.dataflows.providers.china.akshare import AKShareProvider

                            ak_provider = AKShareProvider()
                            ak_symbol = _normalize_symbol_for_cn("akshare", ticker)
                            news_df = ak_provider.get_stock_news_sync(symbol=ak_symbol, limit=20)
                            normalized = []
                            if news_df is not None and not news_df.empty:
                                for _, row in news_df.head(20).iterrows():
                                    normalized.append(
                                        _normalize_news_item(
                                            {
                                                "title": row.get("新闻标题", "") or row.get("标题", ""),
                                                "content": row.get("新闻内容", "") or row.get("内容", ""),
                                                "summary": row.get("新闻摘要", "") or row.get("摘要", ""),
                                                "publish_time": row.get("发布时间", "") or row.get("时间", ""),
                                                "source": row.get("新闻来源", "") or row.get("文章来源", "") or "东方财富",
                                            },
                                            "AKShare",
                                        )
                                    )
                            if normalized:
                                news_items = normalized
                                used_sources.append("AKShare")
                                logger.info(f"✅ [统一情绪工具] AKShare命中新闻: {len(news_items)}")
                            else:
                                data_quality_notes.append("AKShare返回空新闻。")
                    except Exception as source_error:
                        source_errors.append(f"{source_name}: {source_error}")
                        data_quality_notes.append(f"{source_name} 获取失败，已尝试回退。")

                if not news_items:
                    try:
                        ai_news_text = interface.get_stock_news_openai(ticker, curr_date)
                        if ai_news_text and not _looks_unavailable_search_text(ai_news_text):
                            ai_items = _extract_ai_news_items(ai_news_text)
                            if not ai_items:
                                ai_items = [
                                    {
                                        "title": "AI联网新闻摘要",
                                        "content": ai_news_text[:1200],
                                        "publish_time": curr_date,
                                        "source": "AI联网搜索",
                                        "sentiment_score": _score_text(ai_news_text),
                                    }
                                ]
                            news_items = [_normalize_news_item(item, "AI联网搜索") for item in ai_items]
                            used_sources.append("AI联网搜索回退")
                            ai_fallback_used = True
                            data_quality_notes.append("主备新闻源均不可用，已使用AI联网搜索回退。")
                        else:
                            data_quality_notes.append("AI联网搜索返回不可用结果。")
                    except Exception as ai_error:
                        source_errors.append(f"ai_search: {ai_error}")
                        data_quality_notes.append("AI联网搜索失败。")

                if news_items:
                    llm_result = _llm_score_news_batch(news_items)
                    if llm_result.get("used"):
                        for idx, item in enumerate(news_items):
                            llm_score = None
                            if idx < len(llm_result.get("scores", [])):
                                llm_score = llm_result["scores"][idx]
                            if llm_score is None:
                                continue
                            provider_score = item.get("provider_score")
                            keyword_score = float(item.get("keyword_score", item.get("sentiment_score", 0.0)))
                            if provider_score is None:
                                merged = 0.75 * llm_score + 0.25 * keyword_score
                            else:
                                merged = 0.55 * llm_score + 0.30 * float(provider_score) + 0.15 * keyword_score
                            item["llm_score"] = llm_score
                            item["sentiment_score"] = max(-1.0, min(1.0, merged))
                        llm_sentiment_summary = str(llm_result.get("summary", "")).strip()
                        llm_notes.append("LLM批量情绪评分已启用（关键词仅作兜底）。")
                        if llm_result.get("note"):
                            llm_notes.append(str(llm_result.get("note")))
                    else:
                        llm_notes.append(str(llm_result.get("note", "LLM评分未启用")))

                if not news_items:
                    behavior_temp = behavior_factors.get("behavior_risk_appetite", 50)
                    behavior_divergence = behavior_factors.get("volatility_pressure", 0)
                    theme_metrics = _compute_theme_dispersion([])
                    behavior_momentum = "样本不足"
                    if behavior_factors:
                        ret_5d = behavior_factors.get("ret_5d", 0.0)
                        if ret_5d > 0.015:
                            behavior_momentum = "升温"
                        elif ret_5d < -0.015:
                            behavior_momentum = "降温"
                        else:
                            behavior_momentum = "持平"
                    failed_report = f"""
## 中文市场情绪分析

**股票**: {ticker} ({market_info['market_name']})
**分析日期**: {curr_date}

### 市场情绪概况
- 当前未获取到可用情绪样本，情绪面暂无法定量评估。

### 情绪指标
- 情绪温度(0-100): {int(behavior_temp)}
- 情绪方向: 中性
- 情绪分歧度(0-100): {int(round(behavior_divergence * 0.75 + theme_metrics.get('theme_dispersion_index', 0) * 0.25))}
- 主题分化度(0-100): {theme_metrics.get('theme_dispersion_index', 0)}
- 风险偏好(0-100): {int(behavior_temp)}
- 情绪动量: {behavior_momentum}
- confidence: 5

### 行为情绪因子
- 行情来源: {behavior_source or '不可用'}
- 价格动量指数(0-100): {behavior_factors.get('price_momentum_index', 50)}
- 量能冲击指数(0-100): {behavior_factors.get('volume_impulse_index', 50)}
- 波动压力指数(0-100): {behavior_factors.get('volatility_pressure', 0)}
- 主导主题: {theme_metrics.get('dominant_themes', '无')}

### 数据质量说明
- {'；'.join(data_quality_notes) if data_quality_notes else '未获取到数据源说明'}
- {'；'.join(behavior_notes) if behavior_notes else '行为因子无额外告警'}
- {'；'.join(llm_notes) if llm_notes else 'LLM情绪评分无额外说明'}
- 错误明细: {' | '.join(source_errors) if source_errors else '无'}
"""
                    result_data.append(failed_report)
                else:
                    scores = [float(item.get("sentiment_score", 0.0)) for item in news_items]
                    overall_score = sum(scores) / len(scores) if scores else 0.0
                    theme_metrics = _compute_theme_dispersion(news_items)
                    sentiment_label = _label_from_score(overall_score)
                    heat = _heat_from_count(len(news_items))
                    confidence = _confidence_from_count(len(news_items), len(used_sources), ai_fallback_used)
                    trend_hint = _trend_hint(scores)

                    text_all = " ".join(
                        f"{item.get('title', '')} {item.get('content', '')}" for item in news_items
                    )
                    positive_hits = sum(1 for k in positive_keywords if k in text_all)
                    negative_hits = sum(1 for k in negative_keywords if k in text_all)
                    risk_hits = sum(1 for k in risk_keywords if k in text_all)
                    sample_count = len(scores)
                    if sample_count > 1:
                        variance = sum((s - overall_score) ** 2 for s in scores) / sample_count
                        std_dev = variance ** 0.5
                    else:
                        std_dev = 0.0

                    text_temperature = int(max(0, min(100, 50 + overall_score * 50)))
                    sentiment_divergence = int(max(0, min(100, std_dev * 100)))
                    momentum_value = 0.0
                    if len(scores) >= 6:
                        momentum_value = (sum(scores[:5]) / 5) - (sum(scores[5:10]) / len(scores[5:10]))

                    text_risk_appetite = int(max(
                        0,
                        min(
                            100,
                            50 + (positive_hits - negative_hits) * 3 - risk_hits * 4 + overall_score * 15,
                        ),
                    ))
                    behavior_risk = int(behavior_factors.get("behavior_risk_appetite", text_risk_appetite))
                    sentiment_temperature = int(round(text_temperature * 0.6 + behavior_risk * 0.4))
                    risk_appetite = int(round(text_risk_appetite * 0.55 + behavior_risk * 0.45))
                    if behavior_factors:
                        sentiment_divergence = int(round(sentiment_divergence * 0.55 + behavior_factors.get("volatility_pressure", 0) * 0.20 + theme_metrics.get("theme_dispersion_index", 0) * 0.25))
                    else:
                        sentiment_divergence = int(round(sentiment_divergence * 0.75 + theme_metrics.get("theme_dispersion_index", 0) * 0.25))

                    source_diversity = len(set(item.get("source", "") for item in news_items if item.get("source")))

                    behavior_momentum_label = "样本不足"
                    if behavior_factors:
                        if behavior_factors.get("ret_5d", 0.0) > 0.015:
                            behavior_momentum_label = "升温"
                        elif behavior_factors.get("ret_5d", 0.0) < -0.015:
                            behavior_momentum_label = "降温"
                        else:
                            behavior_momentum_label = "持平"

                    signal_lines = [
                        f"- 情绪正负比: 正向{positive_hits} / 负向{negative_hits}",
                        f"- 风险事件密度: {risk_hits} 次关键词触发",
                        f"- 样本覆盖: {len(news_items)} 条，来源数 {source_diversity}",
                        f"- 文本动量: {trend_hint} ({momentum_value:+.2f})",
                        f"- 行为动量: {behavior_momentum_label}",
                        f"- 主导主题: {theme_metrics.get('dominant_themes', '无')} (主题数: {theme_metrics.get('theme_count', 0)})",
                        f"- 主题情绪价差: {theme_metrics.get('theme_polarity_spread', 0.0):.2f}",
                    ]
                    if llm_sentiment_summary:
                        signal_lines.append(f"- 模型情绪摘要: {llm_sentiment_summary[:120]}")

                    sentiment_summary = f"""
## 中文市场情绪分析

**股票**: {ticker} ({market_info['market_name']})
**分析日期**: {curr_date}
**数据来源**: {' -> '.join(used_sources)}

### 市场情绪概况
- 总体情绪: **{sentiment_label}**（overall_score: {overall_score:.2f}）
- 情绪温度: {sentiment_temperature}/100

### 情绪指标
- 情绪温度(0-100): {sentiment_temperature}
- 情绪方向: {sentiment_label}
- 情绪分歧度(0-100): {sentiment_divergence}
- 主题分化度(0-100): {theme_metrics.get('theme_dispersion_index', 0)}
- 风险偏好(0-100): {risk_appetite}
- 情绪动量: 文本{trend_hint} / 行为{behavior_momentum_label}
- 情绪基准分(overall_score): {overall_score:.2f}
- 热度分层: {heat}
- confidence: {confidence}

### 行为情绪因子
- 行情来源: {behavior_source or '不可用'}
- 价格动量指数(0-100): {behavior_factors.get('price_momentum_index', 50)}
- 量能冲击指数(0-100): {behavior_factors.get('volume_impulse_index', 50)}
- 波动压力指数(0-100): {behavior_factors.get('volatility_pressure', 0)}
- 行为风险偏好(0-100): {behavior_factors.get('behavior_risk_appetite', risk_appetite)}
- 主导主题: {theme_metrics.get('dominant_themes', '无')}
- 主题分布熵(0-1): {theme_metrics.get('theme_entropy', 0.0)}

### 情绪驱动因子摘要
{chr(10).join(signal_lines)}

### 数据质量说明
- {'；'.join(data_quality_notes) if data_quality_notes else '无异常'}
- {'；'.join(behavior_notes) if behavior_notes else '行为因子无额外告警'}
- {'；'.join(llm_notes) if llm_notes else 'LLM情绪评分无额外说明'}
- 错误明细: {' | '.join(source_errors) if source_errors else '无'}
"""
                    result_data.append(sentiment_summary)

            else:
                # 美股：使用Reddit情绪分析
                logger.info(f"🇺🇸 [统一情绪工具] 处理美股情绪...")

                try:
                    from tradingagents.dataflows.interface import get_reddit_sentiment

                    sentiment_data = get_reddit_sentiment(ticker, curr_date)
                    result_data.append(f"## 美股Reddit情绪\n{sentiment_data}")
                except Exception as e:
                    result_data.append(f"## 美股Reddit情绪\n获取失败: {e}")

            # 组合所有数据
            combined_result = f"""# {ticker} 情绪分析

**股票类型**: {market_info['market_name']}
**分析日期**: {curr_date}

{chr(10).join(result_data)}

---
*数据来源: 根据股票类型自动选择最适合的情绪数据源*
"""

            logger.info(f"😊 [统一情绪工具] 数据获取完成，总长度: {len(combined_result)}")
            return combined_result

        except Exception as e:
            error_msg = f"统一情绪分析工具执行失败: {str(e)}"
            logger.error(f"❌ [统一情绪工具] {error_msg}")
            return error_msg
