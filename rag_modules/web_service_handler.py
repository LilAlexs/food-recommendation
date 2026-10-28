from rag_modules.quantity_followup import source_quantity_target
from rag_modules.session_constraints import parse_updates, state_for, constraint_text
from rag_modules.query_understanding import semantic_resolution
from rag_modules.identity_bound_retrieval import IdentityBoundRetriever
from rag_modules.recipe_catalog import resolve_request, get_catalog, public_result, CatalogUnavailable
import os
import re
from rag_modules.generation_errors import GenerationError, usable_answer
from rag_modules.performance_monitor import event
from rag_modules.performance_monitor import traced, submit_context, mark_failure
"""
Web服务处理模块
负责处理Web API和静态文件服务
"""

import logging
import json
import time
import concurrent.futures
from datetime import datetime
from typing import Dict, Any, Optional
from rag_modules.rule_coreference import rewrite_query_with_history

logger = logging.getLogger(__name__)

class WebServiceHandler:
    """
    Web服务处理器
    
    功能：
    1. API路由处理
    2. 静态文件服务
    3. 错误处理
    4. 响应格式化
    """
    
    def __init__(self, rag_system):
        """初始化Web服务处理器"""
        self.rag_system = rag_system
        self.app = None
    
    def setup_flask_app(self):
        """设置Flask应用和路由"""
        try:
            from flask import Flask, request, jsonify, Response
            from flask_cors import CORS
            
            self.app = Flask(__name__)
            CORS(self.app)
            
            # 设置路由
            self._setup_routes()
            
            return self.app
            
        except ImportError as e:
            logger.error(f"Flask导入失败: {e}")
            return None
    
    def _setup_routes(self):
        """设置所有API路由"""
        from flask import request, jsonify, Response, send_from_directory
        
        @self.app.route('/')
        def serve_index():
            """提供主页"""
            return self._serve_static_file('index.html')
        
        @self.app.route('/<path:filename>')
        def serve_static(filename):
            """提供静态文件服务"""
            return self._serve_static_file(filename)
        
        @self.app.route('/health', methods=['GET'])
        def health_check():
            """健康检查端点"""
            return jsonify({
                "status": "healthy",
                "timestamp": str(datetime.now()),
                "service": "RAG System"
            })
        
        @self.app.route('/api/chat', methods=['POST'])
        def chat():
            """聊天API - 普通响应"""
            return self._handle_chat_request()
        
        @self.app.route('/api/chat/stream', methods=['POST'])
        def chat_stream():
            """聊天API - 流式响应"""
            return self._handle_stream_request()
        
        @self.app.route('/api/recipes/recommendations', methods=['POST'])
        def get_recommendations():
            """获取菜谱推荐"""
            return self._handle_recommendations_request()
        
        @self.app.route('/api/recipes/<recipe_id>', methods=['GET'])
        def get_recipe_detail(recipe_id):
            """获取菜谱详情"""
            return self._handle_recipe_detail_request(recipe_id)
        
        @self.app.route('/api/stats', methods=['GET'])
        def get_stats():
            """获取系统统计信息"""
            return self._handle_stats_request()
    
    def _serve_static_file(self, filename):
        """提供静态文件服务"""
        import os
        from flask import send_from_directory
        
        # 安全检查，防止路径遍历攻击
        if '..' in filename or filename.startswith('/'):
            return "Forbidden", 403
        
        # 前端文件路径
        frontend_path = os.path.join(os.getcwd(), 'frontend', 'dist')
        
        try:
            if filename == 'index.html' or filename == '':
                return send_from_directory(frontend_path, 'index.html')
            else:
                return send_from_directory(frontend_path, filename)
        except FileNotFoundError:
            # 如果文件不存在，返回index.html（用于SPA路由）
            return send_from_directory(frontend_path, 'index.html')
    
    def _prepare_query_and_cache(self, query, session_id):
        # Future.result propagates task failures; wait(timeout) did not cancel work.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            cache = submit_context(executor, self.rag_system.cache_manager.check_semantic_cache,
                                   query, session_id)
            preparation = submit_context(executor, self.rag_system.cache_manager.get_context_for_query,
                                         session_id, query)
            return preparation.result(), cache.result()

    def _recipe_input(self, query, session_id):
        enhanced = self.rag_system.cache_manager.get_context_for_query(session_id, query)
        result = None
        histories = getattr(self.rag_system.cache_manager, "session_contexts", {})
        history = histories.get(session_id, []) if isinstance(histories, dict) else []
        scoped = state_for(self.rag_system.cache_manager, session_id)
        if not scoped.hydrated:
            # Upgrade existing in-memory history once, only explicitly session-scoped updates.
            for item in history:
                old = parse_updates(item["query"])
                scoped.apply([c for c in old.changes if c.scope=="session"])
            scoped.hydrated = True
        changes = None
        if os.getenv("RAG_RECIPE_GUARD", "1").lower() not in {"0", "false", "no"}:
            states = getattr(self.rag_system.cache_manager, "confirmed_recipes", {})
            states = states if isinstance(states, dict) else {}
            current = states.get(session_id) or {}
            changes = parse_updates(query, current.get("name"))
            if changes.only_update:
                if not session_id:
                    notice = "请先新建会话，再设置需要继承的要求。"
                elif changes.ambiguous or any(c.scope=="dish" for c in changes.changes) and not current.get("recipe_id"):
                    notice = "请确认这个要求用于当前这道菜，还是本次会话的所有菜；如指当前菜，请先明确菜名。"
                else:
                    scoped.apply(changes.changes, current.get("recipe_id"))
                    self.rag_system.cache_manager.session_caches.pop(session_id, None)
                    self.rag_system.cache_manager.session_embeddings.pop(session_id, None)
                    event(kind="constraint_update", scopes=sorted({c.scope for c in changes.changes}),
                          actions=[c.action for c in changes.changes], active_count=len(scoped.active(current.get("recipe_id"))))
                    notice = "已更新本次会话的要求。" + constraint_text(scoped.active(current.get("recipe_id")))
                    if not scoped.active(current.get("recipe_id")):
                        notice += "当前没有生效的限制。"
                info = dict(answer_source="constraint_notice", recipe_status="constraint_update", response=notice, options=[])
                return query, None, None, info, notice
            result = resolve_request(query, enhanced, confirmed_recipe=states.get(session_id),
                                     has_identity_state=session_id in states)
            if result is None or not result.get("recipe"):
                local_quantity = source_quantity_target(query, states.get(session_id),
                    get_catalog(os.getenv("RAG_RECIPE_DIR") or None))
                if local_quantity is not None:
                    result = local_quantity
            # Rule misses go to a semantic interpreter, not directly to "not found".
            # Existing catalog choices remain explicit confirmations without an LLM call.
            direct_unknown = bool(result and result.get("status")=="not_found" and
                query.rstrip("？?。！!") in {result["requested"]+"怎么做", result["requested"]+"的做法"} and
                re.fullmatch(r"[\u4e00-\u9fff]{2,24}",result["requested"]) and
                not re.search(r"换|之前|刚才|我想|请|它|这道|那道",result["requested"]))
            needs_analysis = not direct_unknown and (result is None or not result.get("recipe") and
                (not result.get("suggestions") or result.get("status")=="not_found"))
            if needs_analysis and os.getenv("RAG_SEMANTIC_QUERY", "1").lower() not in {"0","false","no"}:
                generation = self.rag_system.generation_module
                result = semantic_resolution(query, states.get(session_id), history,
                    get_catalog(os.getenv("RAG_RECIPE_DIR") or None), generation.client, generation.model_name)
            # Never embed generated assistant history into a new retrieval query.
            enhanced = query
            if result is None:
                if changes.ambiguous:
                    notice = "请明确限制用于这道菜还是本次会话的所有菜，避免错误继承。"
                    return query, None, None, dict(answer_source="constraint_notice", response=notice), notice
                # General recommendation has no bound dish. Persist only explicitly global updates.
                globals_only = [c for c in changes.changes if c.scope=="session"]
                if globals_only:
                    scoped.apply(globals_only)
                    self.rag_system.cache_manager.session_caches.pop(session_id, None)
                    self.rag_system.cache_manager.session_embeddings.pop(session_id, None)
                scoped.bind(None)
                enhanced += constraint_text(scoped.active())
        if result is None:
            # A short dependent phrase must not reuse a response from a different
            # conversation state. Confirmed recipes already bypass answer cache.
            cached = None if history else self.rag_system.cache_manager.check_semantic_cache(query, session_id)
            return enhanced, cached, None, {}, None
        info = public_result(result)
        if result.get("status")=="analysis_unavailable":
            mark_failure("query_analysis_unavailable")
            info.update(answer_source="analysis_error", response=result["message"],
                        retryable=result.get("retryable",False), success=False, options=[])
        event(kind="recipe_resolution", status=result["status"],
              recipe_id=info.get("recipe_id"), source=info.get("source"))
        if not result.get("recipe"):
            setter = getattr(self.rag_system.cache_manager, "set_confirmed_recipe", None)
            if callable(setter) and not result.get("preserve_current", False):
                setter(session_id, None)
                scoped.bind(None)
            event(kind="recipe_state", action="preserved" if result.get("preserve_current",False) else "cleared", reason="catalog_notice")
            # Notices do not contaminate answer cache or conversation recipe state.
            return enhanced, None, None, info, info["response"]
        catalog = get_catalog(os.getenv("RAG_RECIPE_DIR") or None)
        setter = getattr(self.rag_system.cache_manager, "set_confirmed_recipe", None)
        if callable(setter): setter(session_id, None)
        document = catalog.document(result["recipe"])
        event(kind="retrieval", strategy="confirmed_local_recipe",
              recipe_ids=[info["recipe_id"]], document_count=1,
              candidates=[dict(document.metadata)])
        # Never reuse pre-v4 mixed-recipe answers, even if the query text matches.
        event(kind="cache_policy", reason="confirmed_recipe_bypass")
        # A confirmed recipe must not inherit unrelated assistant recipes as instructions.
        question = "已确认目标菜谱：" + info["recipe_name"] + "\n当前问题：" + query
        scoped.bind(info["recipe_id"])
        # A directly confirmed target supplies the scope for combined restrictions.
        combined = parse_updates(query, info["recipe_name"])
        if not combined.ambiguous:
            scoped.apply(combined.changes, info["recipe_id"])
            if combined.changes:
                self.rag_system.cache_manager.session_caches.pop(session_id, None)
                self.rag_system.cache_manager.session_embeddings.pop(session_id, None)
        active = scoped.active(info["recipe_id"])
        question += constraint_text(active)
        event(kind="constraint_context", session_count=sum(x["scope"]=="session" for x in active),
              dish_count=sum(x["scope"]=="dish" for x in active))
        documents = [document]
        info["retrieval_backend"] = "local_recipe"
        if os.getenv("RAG_BOUND_RETRIEVAL", "1").lower() not in {"0", "false", "no"}:
            data_module = getattr(self.rag_system, "data_module", None)
            driver = getattr(data_module, "driver", None) if data_module is not None else None
            vector_module = getattr(self.rag_system, "index_module", None)
            database = getattr(self.rag_system.config, "neo4j_database", "neo4j")
            result = IdentityBoundRetriever(driver, vector_module, database).retrieve(document)
            documents = result.documents
            info["retrieval_backend"] = result.status
            info["graph_recipe_id"] = result.graph_id
            info["vector_status"] = result.vector_status
            info["evidence_policy"] = "source_projection_v7"
            info["unverified_summary_steps"] = result.unverified_steps
            info["withheld_chunks"] = result.withheld_chunks
        return question, None, documents, info, None

    def _remember_success(self, session_id, query, answer, recipe_info=None):
        if not usable_answer(answer):
            raise GenerationError("Empty or invalid answer")
        self.rag_system.cache_manager.add_to_semantic_cache(query, answer, session_id)
        self.rag_system.cache_manager.add_to_context(session_id, query, answer)
        setter = getattr(self.rag_system.cache_manager, "set_confirmed_recipe", None)
        if callable(setter):
            identity = ({"name": recipe_info["recipe_name"], "recipe_id": recipe_info["recipe_id"]}
                        if recipe_info and recipe_info.get("recipe_id") else None)
            setter(session_id, identity)

    @traced("chat")
    def _handle_chat_request(self):
        from flask import request, jsonify
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("message"), str) or not data["message"].strip():
            return jsonify({"error": "消息不能为空", "success": False}), 400
        query = data["message"].strip()
        session_id = data.get("session_id", "")
        if not isinstance(session_id, str):
            return jsonify({"error": "session_id必须是字符串", "success": False}), 400
        try:
            enhanced_query, cached, documents, info, notice = self._recipe_input(query, session_id)
            if notice:
                return jsonify({**info, "query": query, "session_id": session_id,
                                "success": info.get("success",True), "from_cache": False})
            if usable_answer(cached):
                self.rag_system.cache_manager.add_to_context(session_id, query, cached)
                setter = getattr(self.rag_system.cache_manager, "set_confirmed_recipe", None)
                if callable(setter): setter(session_id, None)
                return jsonify({"response": cached, "query": query, "session_id": session_id,
                                "from_cache": True, "success": True})
            if documents is None:
                documents, analysis = self.rag_system.query_router.route_query(
                    query=enhanced_query, top_k=self.rag_system.config.top_k)
            answer = self.rag_system.generation_module.generate_adaptive_answer(enhanced_query, documents)
            self._remember_success(session_id, query, answer, info)
            return jsonify({**info, "response": answer, "query": query, "session_id": session_id,
                            "timestamp": str(datetime.now()), "success": True, "from_cache": False})
        except CatalogUnavailable:
            mark_failure("catalog_unavailable")
            return jsonify({"error": "菜谱资料暂时不可用，请稍后重试", "success": False,
                            "error_code": "catalog_unavailable"}), 503
        except GenerationError:
            mark_failure("generation_failed")
            return jsonify({"error": "生成服务暂时不可用，请稍后重试", "success": False,
                            "error_code": "generation_failed"}), 502
        except Exception as exc:
            mark_failure(type(exc).__name__)
            logger.exception("Chat request failed")
            return jsonify({"error": "请求处理失败，请稍后重试", "success": False,
                            "error_code": "request_failed"}), 500

    def _handle_stream_request(self):
        from flask import request, Response, jsonify
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("message"), str) or not data["message"].strip():
            return jsonify({"error": "消息不能为空", "success": False}), 400
        query = data["message"].strip()
        session_id = data.get("session_id", "")
        if not isinstance(session_id, str):
            return jsonify({"error": "session_id必须是字符串", "success": False}), 400

        @traced("chat.stream")
        def generate():
            started = time.perf_counter()
            first = True
            try:
                enhanced_query, cached, documents, info, notice = self._recipe_input(query, session_id)
                if notice:
                    event(kind="first_content", elapsed_ms=(time.perf_counter()-started)*1000,
                          from_cache=False)
                    yield "data: " + json.dumps({**info, "chunk": notice}) + "\n\n"
                    yield "data: [DONE]\n\n"
                    return
                if info:
                    yield "data: " + json.dumps(info) + "\n\n"
                if usable_answer(cached):
                    event(kind="first_content", elapsed_ms=(time.perf_counter()-started)*1000,
                          from_cache=True)
                    yield "data: " + json.dumps({"chunk": cached, "from_cache": True}) + "\n\n"
                    self.rag_system.cache_manager.add_to_context(session_id, query, cached)
                    setter = getattr(self.rag_system.cache_manager, "set_confirmed_recipe", None)
                    if callable(setter): setter(session_id, None)
                    yield "data: [DONE]\n\n"
                    return
                if documents is None:
                    documents, analysis = self.rag_system.query_router.route_query(
                        query=enhanced_query, top_k=self.rag_system.config.top_k)
                full = ""
                for chunk in self.rag_system.generation_module.generate_adaptive_answer_stream(enhanced_query, documents):
                    if not chunk:
                        continue
                    if first:
                        event(kind="first_content", elapsed_ms=(time.perf_counter()-started)*1000,
                              from_cache=False)
                        first = False
                    full += chunk
                    yield "data: " + json.dumps({"chunk": chunk}) + "\n\n"
                self._remember_success(session_id, query, full, info)
                yield "data: [DONE]\n\n"
            except Exception as exc:
                code = "catalog_unavailable" if isinstance(exc, CatalogUnavailable) else "generation_failed"
                mark_failure(code)
                # Headers may already be sent: SSE errors must be signalled in the body.
                yield "data: " + json.dumps({"chunk": "抱歉，回答未能完成，请稍后重试。",
                    "error": code, "success": False}) + "\n\n"
                yield "data: [DONE]\n\n"

        response = Response(generate(), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    def _handle_recommendations_request(self):
        """处理菜谱推荐请求"""
        from flask import request, jsonify
        
        try:
            data = request.get_json() or {}
            preferences = data.get('preferences', {})
            
            # 获取推荐菜谱
            recipes = self.rag_system.recipe_manager.get_random_recipes_with_images(limit=3)
            
            return jsonify({
                "success": True,
                "data": recipes,
                "message": "推荐获取成功"
            })
            
        except Exception as e:
            logger.error(f"推荐API错误: {e}")
            return jsonify({"error": str(e)}), 500
    
    def _handle_recipe_detail_request(self, recipe_id):
        """处理菜谱详情请求"""
        from flask import jsonify
        
        try:
            recipe = self.rag_system.recipe_manager.get_recipe_by_id(recipe_id)
            if recipe:
                return jsonify({
                    "success": True,
                    "data": recipe
                })
            else:
                return jsonify({"error": "菜谱不存在"}), 404
                
        except Exception as e:
            logger.error(f"菜谱详情API错误: {e}")
            return jsonify({"error": str(e)}), 500
    
    def _handle_stats_request(self):
        """处理统计信息请求"""
        from flask import jsonify
        
        try:
            # 获取系统统计信息
            stats = {
                "cache_stats": self.rag_system.cache_manager.get_session_stats(),
                "route_stats": self.rag_system.query_router.get_route_statistics(),
                "system_info": {
                    "timestamp": str(datetime.now()),
                    "status": "running"
                }
            }
            return jsonify(stats)
            
        except Exception as e:
            logger.error(f"统计API错误: {e}")
            return jsonify({"error": str(e)}), 500
