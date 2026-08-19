"""Multi-turn chat UI for Open Deep Research Agent."""

import asyncio
import traceback

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from open_deep_research.deep_researcher import deep_researcher

st.set_page_config(page_title="Deep Research", page_icon="🔬")
st.title("🔬 Deep Research")

# --- 持久化状态 ---
if "thread_id" not in st.session_state:
    st.session_state.thread_id = "chat-" + str(hash(str(st.session_state)))
if "waiting_for_clarification" not in st.session_state:
    st.session_state.waiting_for_clarification = False

# --- 显示历史消息 ---
for msg in st.session_state.get("chat_history", []):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# --- 输入框 ---
question = st.chat_input("输入你的研究问题...")

if question:
    # 用户消息
    st.chat_message("user").markdown(question)
    st.session_state.setdefault("chat_history", []).append(
        {"role": "user", "content": question}
    )

    result = {"report": None, "error": None, "clarification": None}
    progress_placeholder = st.status("🧠 分析中...", expanded=True)
    progress_placeholder.write("🧠 正在分析你的问题...")

    async def run():
        config = {"configurable": {"thread_id": st.session_state.thread_id}}

        try:
            async for event in deep_researcher.astream(
                {"messages": [{"role": "user", "content": question}]},
                config,
                stream_mode="updates",
            ):
                for node_name, node_output in event.items():
                    if node_name == "final_report_generation":
                        result["report"] = node_output.get("final_report", "")
                        progress_placeholder.write("✅ 报告已生成")
                    elif node_name == "clarify_with_user":
                        msgs = node_output.get("messages", [])
                        if msgs and getattr(msgs[-1], "additional_kwargs", {}).get("is_clarification_question") is True:
                            result["clarification"] = str(msgs[-1].content)
                            progress_placeholder.write(
                                f"🤔 Agent: {result['clarification'][:200]}"
                            )
                        else:
                            progress_placeholder.write("➡️ 澄清完成，直接开始研究")
                    elif node_name == "write_research_brief":
                        brief = node_output.get("research_brief", "")
                        progress_placeholder.write(f"📋 研究简报: {brief[:150]}")
                    elif node_name == "research_supervisor":
                        progress_placeholder.write("🔍 研究中...")
                    else:
                        progress_placeholder.write(f"⚙️ {node_name}")

        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"

    asyncio.run(run())

    # --- 处理 Agent 的回应 ---
    if result["error"]:
        progress_placeholder.update(label="❌ 错误", state="error", expanded=False)
        st.error("执行失败:")
        st.code(result["error"])
        st.session_state.waiting_for_clarification = False

    elif result["report"]:
        progress_placeholder.update(label="✅ 研究完成", state="complete", expanded=False)
        st.session_state.waiting_for_clarification = False
        with st.chat_message("assistant"):
            st.markdown(result["report"])
        st.session_state["chat_history"].append(
            {"role": "assistant", "content": result["report"]}
        )

    elif result["clarification"]:
        progress_placeholder.update(label="🤔 Agent 想确认一个问题", state="complete", expanded=False)
        st.session_state.waiting_for_clarification = True
        with st.chat_message("assistant"):
            st.markdown(result["clarification"])
        st.session_state["chat_history"].append(
            {"role": "assistant", "content": result["clarification"]}
        )
        st.info("👆 Agent 需要你先回答上面的问题，然后在输入框回复即可继续研究。")

    else:
        progress_placeholder.update(label="⚠️ 未生成报告", state="error", expanded=False)
        st.warning("未生成报告，请重试。")
        st.session_state.waiting_for_clarification = False
