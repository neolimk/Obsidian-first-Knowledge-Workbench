def build_chat_title(user_msg):
    return user_msg.split("\n")[0][:40]



def prepare_messages_for_session(sess, user_msg):
    msgs = [{"role": m["role"], "content": m["content"]} for m in sess.get("messages", [])]
    msgs.append({"role": "user", "content": user_msg})
    return msgs



def prepare_chat_request(
    sess,
    user_msg,
    route,
    rag_enabled,
    rag_search,
    rag_context_builder,
    rag_refs_builder,
):
    msgs = prepare_messages_for_session(sess, user_msg)
    title = build_chat_title(user_msg)
    rag_hits = []
    refs = None
    full_system = sess.get("system") or ""
    if rag_enabled:
        rag_hits = rag_search(user_msg)
    if rag_hits:
        rag_ctx = rag_context_builder(user_msg, rag_hits)
        refs = rag_refs_builder(rag_hits)
        rag_system = (
            "你是一个基于用户本地 Obsidian 知识库的助手。请优先基于以下参考资料回答用户问题。\n"
            "## 用户的 Obsidian 知识库参考资料:\n" + rag_ctx
        )
        full_system = rag_system + ("\n\n## 用户额外设置:\n" + full_system if full_system else "")
        msgs = [{"role": "system", "content": full_system}] + msgs
    return {
        "title": title,
        "messages": msgs,
        "refs": refs,
        "rag_hits": rag_hits,
        "system": full_system,
        "route": route or {},
    }



def persist_assistant_message(add_message_fn, sid, text, meta):
    if text and text.strip():
        add_message_fn(sid, "assistant", text, meta=meta)
