from rag_modules.context_evidence import evidence_context
from rag_modules.generation_errors import GenerationError, usable_answer
from rag_modules.performance_monitor import timed, llm_create, submit_context
"""
生成集成模块
"""

import logging
import os
import time
from typing import List

from openai import OpenAI
from langchain_core.documents import Document

logger = logging.getLogger(__name__)

class GenerationIntegrationModule:
    """生成集成模块 - 负责答案生成"""

    def __init__(self, model_name: str = "kimi-k2-0711-preview", temperature: float = 0.1, max_tokens: int = 2048):
        """
        初始化生成集成模块
        """
        self.model_name = model_name
        self.temperature = temperature
        self.max_tokens = max_tokens
        
        # 统一的LLM客户端配置（支持所有兼容OpenAI格式的供应商）
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("请设置 OPENAI_API_KEY 环境变量")

        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.moonshot.cn/v1")

        # Shared by router and both retrieval engines. Diagnostic defaults prevent retry amplification.
        self.request_timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
        self.sdk_retries = int(os.getenv("LLM_MAX_RETRIES", "0"))
        self.stream_attempts = int(os.getenv("LLM_STREAM_ATTEMPTS", "1"))
        if self.request_timeout <= 0 or self.sdk_retries < 0 or self.stream_attempts < 1:
            raise ValueError("LLM timeout must be positive; retries >=0; stream attempts >=1")
        self.client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=self.request_timeout,
            max_retries=self.sdk_retries
        )

        logger.info(f"生成模块初始化完成，模型: {model_name}, API地址: {self.base_url}")

    def _build_prompt(self, question: str, context: str) -> str:
        """构建统一的提示词"""
        return f"""
        作为一位专业的烹饪助手，请基于以下信息回答用户的问题。

        检索到的相关信息：
        {context}

        用户问题：{question}

        请提供准确、实用的回答。根据问题的性质：
        - 如果是询问多个菜品，请提供清晰的列表
        - 如果是询问具体制作方法，请提供详细步骤
        - 如果是一般性咨询，请提供综合性回答

        重要提醒：历史对话仅用于理解指代，不是事实依据。追问也必须以本轮目标菜谱资料为准。
        每段资料都有来源标签，食材节点的名称不等于菜谱名称。明确单道目标时，
        只使用该目标的步骤，不得把其他来源的做法改名套用。
        缺少某项资料就说明该项未注明，不生成所谓简化版填补缺失步骤。

        如果资料只属于一道菜，只回答这道菜。不要把其他菜的步骤改名后当作目标菜，
        可以用自然语言整理，不要求逐字复述，但不得改变食材、用量范围、份数公式、
        顺序依赖、否定和可选条件。不要编造资料中没有的用量、时间或做法。
        如原文声明“参考创作/改编”，必须保留这个限定，不称为标准或权威做法。
        来源核对不代表专业审核，个人经验不能写成普遍结论。资料不足时明确说明不足。

        回答：
        """

    @timed("generation")
    def generate_adaptive_answer(self, question: str, documents: List[Document]) -> str:
        """
        智能统一答案生成
        自动适应不同类型的查询，无需预先分类
        """
        context = evidence_context(documents)

        # 使用统一的提示词构建方法
        prompt = self._build_prompt(question, context)
        
        try:
            response = llm_create(self.client.chat.completions.create,
                model=self.model_name,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )
            
            answer = response.choices[0].message.content
            if not usable_answer(answer):
                raise GenerationError("Empty or invalid generation")
            return answer.strip()
            
        except Exception as e:
            logger.error(f"LightRAG答案生成失败: {e}")
            raise GenerationError("生成服务暂时不可用，请稍后重试") from e
    
    @timed("generation.stream")
    def generate_adaptive_answer_stream(self, question: str, documents: List[Document], max_retries: int = None):
        """
        LightRAG风格的流式答案生成（带重试机制）
        """
        context = evidence_context(documents)

        # 使用统一的提示词构建方法
        prompt = self._build_prompt(question, context)
        
        if max_retries is None:
            max_retries = self.stream_attempts
        if max_retries < 1:
            raise ValueError("stream attempts must be >= 1")
        for attempt in range(max_retries):
            full_response = ""
            try:
                response = llm_create(self.client.chat.completions.create,
                    model=self.model_name,
                    messages=[
                        {"role": "user", "content": prompt}
                    ],
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    stream=True,
                    timeout=self.request_timeout
                )
                
                if attempt == 0:
                    print("开始流式生成回答...\n")
                else:
                    print(f"第{attempt + 1}次尝试流式生成...\n")
                
                full_response = ""
                for chunk in response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        full_response += content
                        yield content  # 使用yield返回流式内容
                
                if not usable_answer(full_response):
                    raise GenerationError("Empty or invalid stream")
                # Only a fully consumed, non-empty stream is successful.
                return
                
            except Exception as e:
                logger.warning(f"流式生成第{attempt + 1}次尝试失败: {e}")
                
                # Once text was emitted, retry/fallback would duplicate or mix answers.
                if full_response:
                    raise
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # 递增等待时间
                    print(f"⚠️ 连接中断，{wait_time}秒后重试...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise GenerationError("生成服务暂时不可用，请稍后重试") from e
