"""OpenAI-compatible and Google-native endpoints for the platform gateway.

Supported routes:
  GET  /v1/models
  GET  /v1/models/{model}
  POST /v1/chat/completions
  POST /v1/responses
  GET  /v1beta/models
  POST /v1beta/models/{model}:generateContent
  POST /v1beta/models/{model}:streamGenerateContent

All routes require a platform API key with a positive token balance.
"""
import time
import json
import uuid
import re
from typing import Any, Dict

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from gemini_web2api.config import CONFIG
from gemini_web2api.models import MODELS, MODEL_ALIASES, list_model_ids, resolve_model
from gemini_web2api.generation import generate, generate_stream
from gemini_web2api.astream import astream
from gemini_web2api.gemini import log
from gemini_web2api.tools import (
    messages_to_prompt,
    parse_tool_calls,
    strip_tool_blocks,
    google_contents_to_prompt,
    parse_google_function_calls,
)
from gemini_web2api.multimodal import fetch_image_bytes, detect_image_mime, upload_image
from gemini_web2api.options import (
    augment_prompt,
    clip_text,
    build_usage,
    stream_exceeds_limit,
)

from ..proxy_service import (
    authenticate_api_key,
    record_api_usage,
    _upload_images_safe,
    sync_gemini_config,
)

router = APIRouter(tags=["openai"])


_TOOL_MARKERS = (
    "```tool_call", "```function_call",
    "<tool_call>", "<function_call>",
    "\ntool_call:", "\nfunction_call:",
)


def _stream_visible_prefix(raw: str) -> str:
    """Text safe to emit while a tool call may still be streaming in."""
    # Keep a tail back so partial markers are never shown as regular text.
    prefix = raw[:max(0, len(raw) - 64)]
    positions = [prefix.find(m) for m in _TOOL_MARKERS if prefix.find(m) >= 0]
    if positions:
        prefix = prefix[:min(positions)]
    return prefix


def _model_object(name: str) -> Dict[str, Any]:
    if name in MODELS:
        cfg = MODELS[name]
    elif name in MODEL_ALIASES:
        # Aliases route through a real model but keep their requested id.
        canonical = MODEL_ALIASES[name]
        cfg = MODELS.get(canonical, {})
    else:
        raise HTTPException(status_code=404, detail={"error": {"message": f"Unknown model '{name}'"}})
    return {
        "id": name,
        "object": "model",
        "created": 1700000000,
        "owned_by": "google",
        "description": cfg.get("desc", ""),
        "permission": [],
        "root": name,
        "parent": None,
    }


@router.get("/v1/models")
def list_models(request: Request):
    authenticate_api_key(request)
    ids = list_model_ids()
    return {"object": "list", "data": [_model_object(name) for name in ids]}


@router.get("/v1/models/{model_id}")
def retrieve_model(model_id: str, request: Request):
    authenticate_api_key(request)
    return _model_object(model_id)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    user, key_info = authenticate_api_key(request)
    user_cookie = user.get("gemini_cookie_override", "")
    client_ip = request.client.host if request.client else "unknown"
    start_time = time.time()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}})
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"error": {"message": "Request body must be an object", "type": "invalid_request_error"}})

    default_model = CONFIG.get("default_model", "gemini-3.7-flash")
    model_name, model_id, think_mode, err, extra_fields = resolve_model(
        body.get("model", default_model), default=default_model
    )
    if err:
        raise HTTPException(status_code=400, detail={"error": {"message": err, "type": "invalid_request_error"}})

    tools = body.get("tools")
    tool_choice = body.get("tool_choice", "auto")
    prompt, images = messages_to_prompt(body.get("messages", []), tools, tool_choice)
    prompt = augment_prompt(prompt, body)
    if not prompt.strip():
        raise HTTPException(status_code=400, detail={"error": {"message": "Prompt is empty", "type": "invalid_request_error"}})

    stream = bool(body.get("stream", False))
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    try:
        file_refs = _upload_images_safe(images)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail={"error": {"message": f"Upstream error: {e}", "type": "upstream_error"}})

    def usage_for(text: str) -> Dict[str, int]:
        return build_usage(prompt, text)

    auth_user = user.get("gemini_auth_user", "")
    xsrf_token = user.get("gemini_xsrf_token", "")

    def call_generate():
        """Return (text, tool_calls) from the appropriate backend."""
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields, cookie=user_cookie, auth_user=auth_user, xsrf_token=xsrf_token)
        if tools and text and tool_choice != "none":
            text, parsed = parse_tool_calls(text)
            return text, parsed or None
        return text, None

    def call_stream():
        return generate_stream(prompt, model_id, think_mode, file_refs, extra_fields, cookie=user_cookie, auth_user=auth_user, xsrf_token=xsrf_token)

    async def call_astream():
        """Async streaming path: one connection per request, no thread pool."""
        async for chunk in astream(prompt, model_id, think_mode, file_refs,
                                   extra_fields, user_cookie, auth_user, xsrf_token):
            yield chunk

    # Real incremental streaming, including tool calls.
    if stream:
        use_tools = bool(tools) and tool_choice != "none"

        async def event_stream():
            raw_text = ""
            stable_visible = ""
            final_visible = ""
            try:
                first_chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(first_chunk)}\n\n".encode("utf-8")

                # Thinking models: tell the client we're reasoning before the
                # first token arrives so the UI can show live status.
                if think_mode == 0:
                    thinking_chunk = {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [{"index": 0, "delta": {"thinking": True}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(thinking_chunk)}\n\n".encode("utf-8")

                deltas = call_astream()

                async for delta in deltas:
                    if stream_exceeds_limit(raw_text + delta, body):
                        break
                    raw_text += delta

                    if use_tools:
                        # Never emit a partial tool marker as assistant text.
                        stable = _stream_visible_prefix(raw_text)
                        new_text = stable[len(stable_visible):]
                        stable_visible = stable
                    else:
                        new_text = delta

                    if new_text:
                        chunk = {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {"content": new_text}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

                finish = "stop"
                if use_tools:
                    final_visible = strip_tool_blocks(raw_text)
                    trailing = final_visible[len(stable_visible):]
                    if trailing:
                        chunk = {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_name,
                            "choices": [{"index": 0, "delta": {"content": trailing}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

                    _, tool_calls = parse_tool_calls(raw_text)
                    for index, tc in enumerate(tool_calls):
                        start_chunk = {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_name,
                            "choices": [{
                                "index": 0,
                                "delta": {"tool_calls": [{
                                    "index": index,
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {"name": tc["function"]["name"], "arguments": ""},
                                }]},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(start_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                        args_chunk = {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model_name,
                            "choices": [{
                                "index": 0,
                                "delta": {"tool_calls": [{
                                    "index": index,
                                    "function": {"arguments": tc["function"]["arguments"]},
                                }]},
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(args_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                    if tool_calls:
                        finish = "tool_calls"
                else:
                    final_visible = raw_text

                if (body.get("stream_options") or {}).get("include_usage"):
                    usage_chunk = {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model_name,
                        "choices": [],
                        "usage": usage_for(final_visible),
                    }
                    yield f"data: {json.dumps(usage_chunk)}\n\n".encode("utf-8")

                end_chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                }
                yield f"data: {json.dumps(end_chunk)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

                duration_ms = (time.time() - start_time) * 1000
                record_api_usage(user["id"], key_info["key_id"], key_info["key_prefix"],
                                 "/v1/chat/completions", model_name, 200, duration_ms,
                                 build_usage(prompt, final_visible)["total_tokens"], client_ip)
            except Exception as e:
                log(f"Stream error: {e}")
                err_chunk = {"error": {"message": str(e), "type": "stream_error"}}
                yield f"data: {json.dumps(err_chunk)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Non-streaming: generate once, then format.
    try:
        text, tool_calls = call_generate()
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        record_api_usage(user["id"], key_info["key_id"], key_info["key_prefix"],
                         "/v1/chat/completions", model_name, 502, duration_ms, 0, client_ip)
        raise HTTPException(status_code=502, detail={"error": {"message": f"Upstream error: {e}", "type": "upstream_error"}})

    text = clip_text(text, body)
    if tools and text and tool_choice != "none":
        text, tool_calls = parse_tool_calls(text)

    msg: Dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"
    usage = usage_for(text or "")
    duration_ms = (time.time() - start_time) * 1000
    record_api_usage(user["id"], key_info["key_id"], key_info["key_prefix"],
                     "/v1/chat/completions", model_name, 200, duration_ms, usage["total_tokens"], client_ip)

    if stream:
        def event_stream_tools():
            chunk = {
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model_name,
                "choices": [{"index": 0, "delta": msg, "finish_reason": finish}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
            if (body.get("stream_options") or {}).get("include_usage"):
                usage_chunk = {"id": cid, "object": "chat.completion.chunk",
                               "created": int(time.time()), "model": model_name,
                               "choices": [], "usage": usage}
                yield f"data: {json.dumps(usage_chunk)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(event_stream_tools(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": usage,
    }


@router.post("/v1/responses")
async def responses(request: Request):
    """Codex CLI / Assistants responses compatibility, with real streaming."""
    user, key_info = authenticate_api_key(request)
    user_cookie = user.get("gemini_cookie_override", "")
    client_ip = request.client.host if request.client else "unknown"
    start_time = time.time()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}})
    if len(str(body)) > 1_000_000:
        raise HTTPException(status_code=413, detail={"error": {"message": "Request body too large (max 1MB)", "type": "invalid_request_error"}})

    default_model = CONFIG.get("default_model", "gemini-3.7-flash")
    model_name, model_id, think_mode, err, extra_fields = resolve_model(
        body.get("model", default_model), default=default_model
    )
    if err:
        raise HTTPException(status_code=400, detail={"error": {"message": err, "type": "invalid_request_error"}})

    input_items = body.get("input", [])
    tools = body.get("tools")
    messages = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": str(body["instructions"])[:8000]})
    if isinstance(input_items, str):
        messages.append({"role": "user", "content": input_items})
    elif isinstance(input_items, list):
        for item in input_items[:50]:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                role = item.get("role", "user")
                if role not in ("user", "assistant", "system", "tool"):
                    role = "user"
                messages.append({"role": role, "content": item.get("content", "")})

    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "No input messages provided", "type": "invalid_request_error"}})

    tool_choice = body.get("tool_choice", "auto")
    prompt, images = messages_to_prompt(messages, tools, tool_choice)
    prompt = augment_prompt(prompt, body)
    if not prompt.strip():
        raise HTTPException(status_code=400, detail={"error": {"message": "Prompt is empty", "type": "invalid_request_error"}})

    try:
        file_refs = _upload_images_safe(images)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail={"error": {"message": f"Image upload failed: {e}", "type": "upstream_error"}})

    rid = f"resp_{uuid.uuid4().hex[:16]}"
    created = int(time.time())

    auth_user = user.get("gemini_auth_user", "")
    xsrf_token = user.get("gemini_xsrf_token", "")

    def gen_text():
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields,
                        cookie=user_cookie, auth_user=auth_user, xsrf_token=xsrf_token)
        return text, None

    async def stream_text():
        async for chunk in astream(prompt, model_id, think_mode, file_refs,
                                   extra_fields, user_cookie, auth_user, xsrf_token):
            yield chunk

    if body.get("stream"):
        async def event_stream():
            sequence = 0
            full_text = ""
            tool_calls = None

            def emit(event_type: str, **fields):
                nonlocal sequence
                sequence += 1
                event = {"type": event_type, "sequence_number": sequence, **fields}
                return f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")

            base = {"id": rid, "object": "response", "created_at": created, "model": model_name}
            yield emit("response.created", response={**base, "status": "in_progress", "output": [], "usage": None})
            yield emit("response.in_progress", response={**base, "status": "in_progress", "output": [], "usage": None})

            if not tools or tool_choice == "none":
                mid = f"msg_{uuid.uuid4().hex[:12]}"
                item = {"type": "message", "id": mid, "role": "assistant", "status": "in_progress", "content": []}
                yield emit("response.output_item.added", output_index=0, item=item)
                part = {"type": "output_text", "text": "", "annotations": []}
                yield emit("response.content_part.added", item_id=mid, output_index=0,
                                content_index=0, part=part)
                try:
                    async for delta in stream_text():
                        if stream_exceeds_limit(full_text + delta, body):
                            break
                        full_text += delta
                        yield emit("response.output_text.delta", item_id=mid, output_index=0,
                                        content_index=0, delta=delta)
                except Exception as e:
                    log(f"Responses stream error: {e}")
                part["text"] = full_text
                yield emit("response.output_text.done", item_id=mid, output_index=0,
                                content_index=0, text=full_text)
                yield emit("response.content_part.done", item_id=mid, output_index=0,
                                content_index=0, part=part)
                final_item = {"type": "message", "id": mid, "role": "assistant", "status": "completed",
                              "content": [part]}
                yield emit("response.output_item.done", output_index=0, item=final_item)
                output = [final_item]
            else:
                tool_fcs = None
                try:
                    text, tool_fcs = gen_text()
                except Exception as e:
                    text = ""
                    log(f"Responses upstream error: {e}")
                text = clip_text(text, body)
                full_text, tool_calls = parse_tool_calls(text) if text else ("", None)
                output = []
                index = 0
                if tool_calls:
                    for tc in tool_calls:
                        call = {"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                                "name": tc["function"]["name"], "arguments": tc["function"]["arguments"],
                                "status": "completed"}
                        yield emit("response.output_item.added", output_index=index, item={**call, "arguments": "", "status": "in_progress"})
                        yield emit("response.function_call_arguments.delta", item_id=call["id"],
                                        output_index=index, delta=call["arguments"])
                        yield emit("response.function_call_arguments.done", item_id=call["id"],
                                        output_index=index, arguments=call["arguments"])
                        yield emit("response.output_item.done", output_index=index, item=call)
                        output.append(call)
                        index += 1
                if full_text or not output:
                    mid = f"msg_{uuid.uuid4().hex[:12]}"
                    part = {"type": "output_text", "text": full_text, "annotations": []}
                    final_item = {"type": "message", "id": mid, "role": "assistant", "status": "completed", "content": [part]}
                    yield emit("response.output_item.added", output_index=index, item={**final_item, "content": []})
                    yield emit("response.content_part.added", item_id=mid, output_index=index, content_index=0, part={"type": "output_text", "text": "", "annotations": []})
                    yield emit("response.output_text.delta", item_id=mid, output_index=index, content_index=0, delta=full_text)
                    yield emit("response.output_text.done", item_id=mid, output_index=index, content_index=0, text=full_text)
                    yield emit("response.content_part.done", item_id=mid, output_index=index, content_index=0, part=part)
                    yield emit("response.output_item.done", output_index=index, item=final_item)
                    output.append(final_item)

            usage = build_usage(prompt, full_text)
            yield emit("response.completed", response={**base, "status": "completed", "output": output, "usage": usage})

            duration_ms = (time.time() - start_time) * 1000
            record_api_usage(user["id"], key_info["key_id"], key_info["key_prefix"],
                             "/v1/responses", model_name, 200, duration_ms,
                             usage["total_tokens"], client_ip)

        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Non-streaming responses
    try:
        text, tool_fcs = gen_text()
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        record_api_usage(user["id"], key_info["key_id"], key_info["key_prefix"],
                         "/v1/responses", model_name, 502, duration_ms, 0, client_ip)
        raise HTTPException(status_code=502, detail={"error": {"message": f"Upstream AI error: {e}", "type": "upstream_error"}})

    text = clip_text(text, body)
    tool_calls = None
    if tools and text and tool_choice != "none":
        text, tool_calls = parse_tool_calls(text)

    output = []
    if tool_calls:
        for tc in tool_calls:
            output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                           "name": tc["function"]["name"], "arguments": tc["function"]["arguments"],
                           "status": "completed"})
    if text or not output:
        output.append({
            "type": "message", "id": f"msg_{uuid.uuid4().hex[:12]}", "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text or "", "annotations": []}],
        })

    usage = build_usage(prompt, text or "")
    duration_ms = (time.time() - start_time) * 1000
    record_api_usage(user["id"], key_info["key_id"], key_info["key_prefix"],
                     "/v1/responses", model_name, 200, duration_ms, usage["total_tokens"], client_ip)

    return {
        "id": rid, "object": "response", "created_at": created, "status": "completed",
        "model": model_name, "output": output, "usage": usage,
    }


# ─── Google-native Gemini CLI endpoints ──────────────────────────────────────

@router.get("/v1beta/models")
def google_models(request: Request):
    authenticate_api_key(request)
    return {"models": [
        {
            "name": f"models/{name}",
            "displayName": name,
            "description": cfg.get("desc", ""),
            "supportedGenerationMethods": ["generateContent", "streamGenerateContent"],
        }
        for name, cfg in MODELS.items()
    ]}


@router.post("/v1beta/models/{model_path:path}")
async def google_generate(model_path: str, request: Request):
    user, key_info = authenticate_api_key(request)
    user_cookie = user.get("gemini_cookie_override", "")
    client_ip = request.client.host if request.client else "unknown"
    start_time = time.time()

    if ":" in model_path:
        model_name, action = model_path.rsplit(":", 1)
    else:
        model_name, action = model_path, "generateContent"

    default_model = CONFIG.get("default_model", "gemini-3.6-flash")
    model_name, model_id, think_mode, err, extra_fields = resolve_model(model_name, default=default_model)
    if err:
        raise HTTPException(status_code=400, detail={"error": {"message": err}})

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "Invalid JSON in request body"}})

    tool_config = body.get("toolConfig", {}) or {}
    fc_mode = (tool_config.get("functionCallingConfig") or {}).get("mode", "AUTO")
    has_tools = bool(body.get("tools")) and fc_mode != "NONE"

    prompt, images = google_contents_to_prompt(body)
    prompt = augment_prompt(prompt, body)
    if not prompt.strip():
        raise HTTPException(status_code=400, detail={"error": {"message": "empty content"}})

    auth_user = user.get("gemini_auth_user", "")
    xsrf_token = user.get("gemini_xsrf_token", "")

    try:
        file_refs = _upload_images_safe(images)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}"}})

    def gen_call():
        return generate(prompt, model_id, think_mode, file_refs, extra_fields, cookie=user_cookie, auth_user=auth_user, xsrf_token=xsrf_token), None

    def stream_call():
        return generate_stream(prompt, model_id, think_mode, file_refs, extra_fields, cookie=user_cookie, auth_user=auth_user, xsrf_token=xsrf_token)

    async def call_astream():
        """Async streaming path shared by all streaming endpoints."""
        async for chunk in astream(prompt, model_id, think_mode, file_refs,
                                   extra_fields, user_cookie, auth_user, xsrf_token):
            yield chunk

    def usage_metadata(text: str) -> Dict[str, int]:
        usage = build_usage(prompt, text)
        return {
            "promptTokenCount": usage["prompt_tokens"],
            "candidatesTokenCount": usage["completion_tokens"],
            "totalTokenCount": usage["total_tokens"],
        }

    stream = "streamGenerate" in action or request.query_params.get("alt") == "sse"

    if stream and not has_tools:
        async def event_stream():
            full_text = ""
            try:
                async for delta in call_astream():
                    if stream_exceeds_limit(full_text + delta, body):
                        break
                    full_text += delta
                    chunk = {
                        "candidates": [{"content": {"parts": [{"text": delta}], "role": "model"}, "index": 0}],
                        "modelVersion": model_name,
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                final_chunk = {
                    "candidates": [{"finishReason": "STOP", "index": 0}],
                    "usageMetadata": usage_metadata(full_text),
                    "modelVersion": model_name,
                }
                yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                duration_ms = (time.time() - start_time) * 1000
                record_api_usage(user["id"], key_info["key_id"], key_info["key_prefix"],
                                 "/v1beta/models", model_name, 200, duration_ms,
                                 build_usage(prompt, full_text)["total_tokens"], client_ip)
            except Exception as e:
                log(f"Google stream error: {e}")
                yield f"data: {json.dumps({'error': {'message': str(e)}})}\n\n".encode("utf-8")

        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    try:
        text, tool_fcs = gen_call()
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        record_api_usage(user["id"], key_info["key_id"], key_info["key_prefix"],
                         "/v1beta/models", model_name, 502, duration_ms, 0, client_ip)
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}"}})

    text = clip_text(text, body)
    parts = []
    if tool_fcs:
        for fc in tool_fcs:
            parts.append({"functionCall": {
                "name": fc["name"],
                "args": json.loads(fc["arguments"]) if fc.get("arguments") else {},
            }})
        if text:
            parts.insert(0, {"text": text})
    elif has_tools and text:
        clean, function_calls = parse_google_function_calls(text)
        if function_calls:
            if clean:
                parts.append({"text": clean})
            for fc in function_calls:
                parts.append({"functionCall": {"name": fc["name"], "args": fc["args"]}})
        else:
            parts.append({"text": text})
    else:
        parts.append({"text": text or ""})

    response_obj = {
        "candidates": [{"content": {"parts": parts, "role": "model"}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": usage_metadata(text),
        "modelVersion": model_name,
    }
    duration_ms = (time.time() - start_time) * 1000
    record_api_usage(user["id"], key_info["key_id"], key_info["key_prefix"],
                     "/v1beta/models", model_name, 200, duration_ms,
                     build_usage(prompt, text)["total_tokens"], client_ip)

    if stream:
        return StreamingResponse(
            iter([f"data: {json.dumps(response_obj, ensure_ascii=False)}\n\n".encode("utf-8")]),
            media_type="text/event-stream",
        )
    return response_obj
