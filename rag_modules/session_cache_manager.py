from rag_modules.session_constraints import state_for
from rag_modules.query_preparation import prepare, enabled
from rag_modules.generation_errors import usable_answer
from rag_modules.performance_monitor import event
from rag_modules.performance_monitor import timed, llm_create, submit_context
"""
会话缓存管理模块
负责管理会话级语义缓存和上下文
"""

import logging
import numpy as np
from typing import Dict, List, Optional, Any
from datetime import datetime

logger = logging.getLogger(__name__)

class SessionCacheManager:
    """
    会话级缓存管理器
    
    功能：
    1. 会话级语义缓存 - 每个聊天窗口独立缓存
    2. 上下文管理 - 维护对话历史
    3. 语义相似度匹配 - 智能缓存命中
    """
    
    def __init__(self, embedding_model=None):
        """初始化缓存管理器"""
        self.embedding_model = embedding_model
        
        # 🚀 会话级语义缓存系统 - 针对每个聊天窗口独立缓存
        self.session_caches = {}  # 按session_id分组的缓存：{session_id: {query: response}}
        self.session_embeddings = {}  # 按session_id分组的向量：{session_id: {query: embedding}}
        self.constraint_states = {}
        self.confirmed_recipes = {}  # session -> confirmed identity, or None after a topic barrier
        self.session_contexts = {}  # 按session_id分组的上下文：{session_id: [messages]}
        
        # 缓存配置
        self.cache_threshold = 0.75  # 语义相似度阈值
        self.max_session_cache_size = 50  # 每个会话最大缓存条目数
        self.max_context_length = 10  # 每个会话保留的最大上下文消息数

    
    def _calculate_similarity(self, embedding1: np.ndarray, embedding2: np.ndarray) -> float:
        """计算两个向量的余弦相似度"""
        try:
            dot_product = np.dot(embedding1, embedding2)
            norm1 = np.linalg.norm(embedding1)
            norm2 = np.linalg.norm(embedding2)
            return dot_product / (norm1 * norm2)
        except:
            return 0.0

    @timed("cache.lookup")
    def check_semantic_cache(self, query: str, session_id: str = None) -> Optional[str]:
        """检查会话级语义缓存中是否有相似查询"""
        if state_for(self, session_id).any():
            event(kind="cache_policy", reason="scoped_constraints_bypass")
            return None
        if not session_id or session_id not in self.session_caches:
            return None

        if enabled("RAG_ADAPTIVE_CONTEXT"):
            plan = prepare(query, self.session_contexts.get(session_id, []))
            if plan.dependent:
                event(kind="cache_policy", reason="dependent_bypass")
                return None
            # Exact matching avoids silently reusing a previous dish's answer.
            cached = self.session_caches[session_id].get(query)
            answer = cached.get("response") if cached else None
            return answer if usable_answer(answer) else None
        session_cache = self.session_caches[session_id]
        session_embeddings = self.session_embeddings[session_id]

        if not session_cache:
            return None

        try:
            # 计算查询向量
            query_embedding = self.embedding_model.embed_documents([query])[0]
        except Exception as e:
            logger.warning(f"查询向量计算失败: {e}")
            return None

        # 查找最相似的缓存查询
        best_similarity = 0
        best_response = None

        for cached_query, cached_data in session_cache.items():
            cached_embedding = session_embeddings.get(cached_query)
            if cached_embedding is not None:
                similarity = self._calculate_similarity(query_embedding, cached_embedding)
                if similarity > best_similarity and similarity >= self.cache_threshold:
                    best_similarity = similarity
                    best_response = cached_data['response']

        if usable_answer(best_response):
            logger.info(f"🎯 会话缓存命中! Session: {session_id}, 相似度: {best_similarity:.3f}")
            return best_response

        return None

    def add_to_semantic_cache(self, query: str, response: str, session_id: str = None):
        """将查询-答案对添加到会话级语义缓存"""
        try:
            if state_for(self, session_id).any():
                return
            if not session_id or not usable_answer(response):
                return

            if enabled("RAG_ADAPTIVE_CONTEXT") and prepare(
                    query, self.session_contexts.get(session_id, [])).dependent:
                return
            # 初始化会话缓存
            if session_id not in self.session_caches:
                self.session_caches[session_id] = {}
                self.session_embeddings[session_id] = {}

            session_cache = self.session_caches[session_id]
            session_embeddings = self.session_embeddings[session_id]

            # 限制会话缓存大小
            if len(session_cache) >= self.max_session_cache_size:
                # 删除最旧的缓存项
                oldest_key = next(iter(session_cache))
                del session_cache[oldest_key]
                del session_embeddings[oldest_key]

            # 计算查询向量
            query_embedding = self.embedding_model.embed_documents([query])[0]

            # 添加到缓存
            session_cache[query] = {
                'response': response,
                'timestamp': datetime.now().isoformat()
            }
            session_embeddings[query] = query_embedding

            logger.info(f"📝 已添加到会话缓存 {session_id}, 当前大小: {len(session_cache)}")

        except Exception as e:
            logger.warning(f"添加到语义缓存失败: {e}")

    def set_confirmed_recipe(self, session_id, recipe=None):
        if session_id:
            self.confirmed_recipes[session_id] = dict(recipe) if recipe else None

    def add_to_context(self, session_id: str, query: str, response: str):
        """添加对话到上下文历史"""
        try:
            if not session_id or not usable_answer(response):
                return

            # 初始化会话上下文
            if session_id not in self.session_contexts:
                self.session_contexts[session_id] = []

            context = self.session_contexts[session_id]

            # 添加新的对话
            context.append({
                'query': query,
                'response': response,
                'timestamp': datetime.now().isoformat()
            })

            # 限制上下文长度
            if len(context) > self.max_context_length:
                context.pop(0)  # 删除最旧的对话

            logger.info(f"📝 已添加上下文到会话 {session_id}, 当前长度: {len(context)}")

        except Exception as e:
            logger.warning(f"添加上下文失败: {e}")

    @timed("query.context")
    def get_context_for_query(self, session_id: str, current_query: str) -> str:
        """获取增强的查询上下文"""
        try:
            if enabled("RAG_ADAPTIVE_CONTEXT"):
                plan = prepare(current_query, self.session_contexts.get(session_id, []))
                event(kind="query_preparation", reason=plan.reason, dependent=plan.dependent,
                      original_chars=len(current_query), prepared_chars=len(plan.text))
                return plan.text
            if not session_id or session_id not in self.session_contexts:
                return current_query

            context = self.session_contexts[session_id]
            if not context:
                return current_query

            # 构建上下文增强的查询
            context_parts = []
            
            # 添加最近的对话历史（最多3轮）
            recent_context = context[-3:] if len(context) > 3 else context
            
            for item in recent_context:
                context_parts.append(f"用户问: {item['query']}")
                context_parts.append(f"AI答: {item['response'][:100]}...")  # 截取前100字符
            
            # 添加当前查询
            context_parts.append(f"当前问题: {current_query}")
            
            enhanced_query = "\n".join(context_parts)
            
            logger.info(f"🔗 已为会话 {session_id} 构建上下文增强查询")
            return enhanced_query

        except Exception as e:
            logger.warning(f"上下文获取失败: {e}")
            return current_query

    def get_session_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        return {
            'total_sessions': len(self.session_caches),
            'total_cached_queries': sum(len(cache) for cache in self.session_caches.values()),
            'total_contexts': sum(len(context) for context in self.session_contexts.values()),
            'cache_threshold': self.cache_threshold,
            'max_session_cache_size': self.max_session_cache_size,
            'max_context_length': self.max_context_length
        }

    def clear_session_cache(self, session_id: str):
        """清除指定会话的缓存"""
        if session_id in self.session_caches:
            del self.session_caches[session_id]
        if session_id in self.session_embeddings:
            del self.session_embeddings[session_id]
        self.confirmed_recipes.pop(session_id, None)
        self.constraint_states.pop(session_id, None)
        if session_id in self.session_contexts:
            del self.session_contexts[session_id]
        logger.info(f"🗑️ 已清除会话 {session_id} 的缓存")

    def clear_all_caches(self):
        """清除所有缓存"""
        self.session_caches.clear()
        self.session_embeddings.clear()
        self.session_contexts.clear()
        self.confirmed_recipes.clear()
        self.constraint_states.clear()
        logger.info("🗑️ 已清除所有会话缓存")
