from app.services.chat_flow import build_chat_title, prepare_messages_for_session


def test_build_chat_title_uses_first_line_and_truncates():
    title = build_chat_title("第一行标题" + "x" * 80 + "\n第二行说明")
    assert title.startswith("第一行标题")
    assert len(title) == 40


def test_prepare_messages_for_session_appends_user_message():
    sess = {"messages": [{"role": "assistant", "content": "旧回复"}]}
    msgs = prepare_messages_for_session(sess, "新问题")
    assert msgs == [
        {"role": "assistant", "content": "旧回复"},
        {"role": "user", "content": "新问题"},
    ]
