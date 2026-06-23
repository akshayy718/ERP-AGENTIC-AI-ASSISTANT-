"""
agent_app.py  (Tier 1+3: CODE-ENFORCED confirmation gate + error handling)
------------
A LangChain agent powered by Groq, with a simple Gradio chat window.

THE KEY IDEA (what makes it "agentic"):
We give the LLM a goal and a set of tools (from erp_tools.py). The LLM runs in a
LOOP - it thinks, picks a tool, sees the result, and decides what to do next,
repeating until done. We do NOT hard-code the steps; the model plans them itself.

HOW THE CONFIRMATION GUARDRAIL ACTUALLY WORKS (read this):
Earlier versions just told the model in the system prompt "ask before you write
data" - but LLMs don't follow instructions 100% of the time, and for actions
that change real data, that isn't good enough.

So now the SAFETY ITSELF lives in this file's code, not in the prompt:
  - The write tools (approve/reject/update) in erp_tools.py no longer touch the
    ERP at all. They just record a "proposal" in erp_tools.PENDING_WRITES.
  - chat_fn() below checks, BEFORE calling the agent: is there a pending
    proposal from last turn, AND did the user just say something affirmative
    like "yes"? If both are true, our own Python code executes the real change
    directly (via erp_tools.EXECUTORS) - the LLM is not involved in that
    decision at all.
  - If the user says anything else, the pending proposal is discarded (treated
    as "no") and the message goes to the agent normally.
This means the data is safe even if the model ever messes up its wording.

ERROR HANDLING: if a tool returns a result starting with "ERROR", the agent
must stop, explain the problem in plain language, and NOT pretend it worked.

Run it with (after the ERP API is running on port 8000):
    py -3.12 agent_app.py
"""

import os
import time
import json
from dotenv import load_dotenv

from langchain_groq import ChatGroq
from langchain.agents import create_tool_calling_agent, AgentExecutor
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage, AIMessage

import gradio as gr

import erp_tools
from erp_tools import ALL_TOOLS

load_dotenv()


# 1. The "brain": the LLM.
llm = ChatGroq(
    model="llama-3.3-70b-versatile",
    temperature=0,
)


# 2. The system prompt: standing instructions that shape how the agent behaves.
SYSTEM_PROMPT = """You are an ERP assistant for a company that uses a mock ERP system.
You help staff manage vendors and purchase orders using natural language.

GENERAL RULES
- You can ONLY act through the tools provided. Never invent vendor ids, PO ids, or data.
- To create a purchase order you need the vendor's id. If the user gives only a name,
  call list_vendors first to find the id, then create the purchase order.
- For tasks that need several steps, do them one tool at a time, using the result of
  each step to decide the next.

CONFIRMATION RULE
- approve_purchase_order, reject_purchase_order and update_purchase_order_amount
  PROPOSE a write action - they do not perform it. Calling one of them stages
  the change and the real system will separately ask the user to confirm.
- After calling one of these tools, tell the user clearly what you proposed
  (the PO id and what will change) and ask them to reply 'yes' to proceed.
  This applies even when the user's request sounded direct, e.g.
  "change PO1002 to 9000" - still propose it and ask, don't assume it's done.
- Reading data (list_vendors, list_purchase_orders, search_vendors, get_spend_summary)
  and create_vendor / create_purchase_order are immediate - no proposal needed.

Example 1 - approving:
  User: approve all pending purchase orders under 5000
  You: call list_purchase_orders, then call approve_purchase_order for each
    matching PO (this only stages them), then reply:
    "I've proposed approving PO1001 ($4500) and PO1004 ($800).
     PO1002 ($12000) is over 5000 so I'll leave it. Confirm with yes?"

Example 2 - editing an amount:
  User: change purchase order PO1002 to 9000
  You: call update_purchase_order_amount (this only stages it), then reply:
    "I've proposed changing PO1002 to $9000. Confirm with yes?"

ERROR RULE
- If any tool returns a result that starts with "ERROR", the action did NOT succeed.
- Stop, do not retry with guessed data, and explain the problem to the user in plain,
  friendly language. Never pretend an action worked when it returned an ERROR.

FINISHING
- After finishing, summarise what you did in plain language. Show ids and amounts clearly.
  Do not show raw JSON to the user.
"""

prompt = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT),
    ("placeholder", "{chat_history}"),
    ("human", "{input}"),
    ("placeholder", "{agent_scratchpad}"),
])


# 3. Build the agent and the executor that runs the loop.
agent = create_tool_calling_agent(llm, ALL_TOOLS, prompt)
agent_executor = AgentExecutor(
    agent=agent,
    tools=ALL_TOOLS,
    verbose=True,
    max_iterations=8,
    return_intermediate_steps=True,  # lets us show the agent's tool-call trace
)


# Words that count as the user confirming a pending write action.
_AFFIRMATIVE = {"yes", "y", "yes please", "confirm", "confirmed", "go ahead",
                "do it", "proceed", "sure", "ok", "okay"}

# Words that count as the user explicitly declining a pending write action.
# IMPORTANT: these must be handled deterministically too, the same as "yes" -
# if we let a bare "no" fall through to the LLM, it can get confused by the
# prior "I've proposed rejecting PO1003..." context and re-propose the same
# action instead of understanding "no" as a decline. So decline gets its own
# fixed, LLM-free response, exactly like confirm does.
_NEGATIVE = {"no", "n", "nope", "cancel", "decline", "stop", "don't", "do not"}


def _describe_pending(pending: list[dict]) -> str:
    """Turn a list of staged writes into a short human-readable description,
    used only for the technical detail / fallback messages below."""
    parts = []
    for w in pending:
        if w["tool"] == "update_purchase_order_amount":
            a = w["args"]
            parts.append(f"set {a['po_id']} to ${a['new_amount']}")
        else:
            parts.append(f"{w['tool']} on {w['args'].get('po_id')}")
    return "; ".join(parts)


def _summarize_step(tool: str, tool_input: dict, result: str) -> str:
    """Turn one tool call the AGENT made into a short, plain-English sentence
    for the visible 'agent steps' trace - e.g. 'Searched vendors for IT'
    instead of a raw JSON blob. The full raw result is still kept separately
    (see 'result' in the step dict) for anyone who wants to check the detail."""
    if isinstance(result, str) and result.startswith("ERROR"):
        return f"{tool} failed - {result}"

    try:
        parsed = json.loads(result) if isinstance(result, str) else None
    except (json.JSONDecodeError, TypeError):
        parsed = None
    count = len(parsed) if isinstance(parsed, list) else None

    if tool == "list_vendors":
        return "Looked up vendors" + (f" ({count} found)" if count is not None else "")
    if tool == "search_vendors":
        q = tool_input.get("query", "")
        return f"Searched vendors for '{q}'" + (f" ({count} found)" if count is not None else "")
    if tool == "create_vendor":
        vid = parsed.get("id") if isinstance(parsed, dict) else None
        name = tool_input.get("name", "")
        return f"Created vendor {name}" + (f" ({vid})" if vid else "")
    if tool == "list_purchase_orders":
        status = tool_input.get("status", "")
        label = f"Checked {status} purchase orders" if status else "Checked purchase orders"
        return label + (f" ({count} found)" if count is not None else "")
    if tool == "create_purchase_order":
        pid = parsed.get("id") if isinstance(parsed, dict) else None
        amount = tool_input.get("amount", "")
        return f"Created purchase order {pid}" if pid else f"Created a ${amount} purchase order"
    if tool == "approve_purchase_order":
        return f"Proposed approving {tool_input.get('po_id', '')}"
    if tool == "reject_purchase_order":
        return f"Proposed rejecting {tool_input.get('po_id', '')}"
    if tool == "update_purchase_order_amount":
        po = tool_input.get("po_id", "")
        amt = tool_input.get("new_amount", "")
        return f"Proposed changing {po} to ${amt}"
    if tool == "get_spend_summary":
        return "Checked the spend summary"

    return f"Ran {tool}"


def _summarize_executed(tool: str, args: dict, outcome: str) -> str:
    """Same idea as _summarize_step, but for a write action that was just
    REALLY executed (after the user confirmed) - so the verb is past tense
    and definite, e.g. 'Approved PO1001' instead of 'Proposed approving'."""
    if isinstance(outcome, str) and outcome.startswith("ERROR"):
        return f"{tool} failed - {outcome}"
    if tool == "approve_purchase_order":
        return f"Approved {args.get('po_id', '')}"
    if tool == "reject_purchase_order":
        return f"Rejected {args.get('po_id', '')}"
    if tool == "update_purchase_order_amount":
        return f"Set {args.get('po_id', '')} to ${args.get('new_amount', '')}"
    return f"Executed {tool}"


# 4. The core turn logic - returns rich detail (reply + step trace + pending
#    state), so both the Gradio app AND the new web dashboard can use it.
def run_agent_turn(message: str, history: list) -> dict:
    """
    Process one user message and return:
      {
        "reply": str,           the text to show the user
        "steps": list[dict],    what the agent did this turn (tool + summary)
        "pending": list[dict],  any write action awaiting a yes/no right now
      }

    CODE-LEVEL CONFIRMATION GATE (see module docstring above for the why):
    Before even calling the agent, we check erp_tools.PENDING_WRITES - any
    write action staged on the PREVIOUS turn but not yet executed.
      - If something is pending AND this message is a plain "yes" -> we
        execute the real write(s) ourselves, right here in Python, and never
        even call the LLM for this turn. Fast, and 100% deterministic.
      - If something is pending but the message is NOT an affirmative -> we
        drop the old proposal (treat it as declined / superseded) and let
        this new message go to the agent normally.
      - If nothing is pending -> normal agent turn, same as always.

    RETRY LOGIC: occasionally the model fumbles a tool call - either Groq
    rejects it outright ('failed_generation'), or a tool with no real
    arguments gets called with None instead of an empty set of arguments
    ('NoneType' is not iterable). Both are transient hiccups, not real bugs,
    so we quietly retry a couple of times before showing the user an error.
    """
    # --- Step 1: the code-level confirmation gate ---
    if erp_tools.PENDING_WRITES:
        if message.strip().lower() in _AFFIRMATIVE:
            pending = erp_tools.PENDING_WRITES.copy()
            erp_tools.PENDING_WRITES.clear()
            steps = []
            summary_lines = []
            for w in pending:
                executor_fn = erp_tools.EXECUTORS.get(w["tool"])
                if executor_fn is None:
                    outcome = f"Could not execute unknown action: {w['tool']}"
                else:
                    outcome = executor_fn(**w["args"])
                steps.append({
                    "tool": w["tool"],
                    "input": w["args"],
                    "result": outcome,
                    "summary": _summarize_executed(w["tool"], w["args"], outcome),
                })
                if outcome.startswith("ERROR"):
                    summary_lines.append(f"Failed: {_describe_pending([w])} -> {outcome}")
                else:
                    summary_lines.append(f"Done: {_describe_pending([w])}")
            return {
                "reply": "Confirmed. " + " | ".join(summary_lines),
                "steps": steps,
                "pending": [],
            }
        elif message.strip().lower() in _NEGATIVE:
            # Explicit decline - handle it deterministically, just like a
            # confirm. Do NOT send "no" into the LLM; it has no way to know
            # that bare word means "cancel the thing you just proposed" and
            # may re-propose the same action instead.
            pending = erp_tools.PENDING_WRITES.copy()
            erp_tools.PENDING_WRITES.clear()
            return {
                "reply": f"Okay, cancelled. ({_describe_pending(pending)} was NOT done.)",
                "steps": [],
                "pending": [],
            }
        else:
            # Some other, unrelated message - drop the stale proposal and
            # treat this as a genuinely new request for the agent to handle.
            erp_tools.PENDING_WRITES.clear()
            # ...and fall through to handle this message normally below.

    # --- Step 2: normal agent turn (with retry on transient hiccups) ---
    chat_history = []
    for turn in history:
        if turn["role"] == "user":
            chat_history.append(HumanMessage(content=turn["content"]))
        elif turn["role"] == "assistant":
            chat_history.append(AIMessage(content=turn["content"]))

    max_attempts = 3
    last_error = None
    transient_markers = (
        "failed_generation",
        "Failed to call a function",
        "NoneType",
    )

    for attempt in range(1, max_attempts + 1):
        try:
            result = agent_executor.invoke({
                "input": message,
                "chat_history": chat_history,
            })
            steps = []
            for action, observation in result.get("intermediate_steps", []):
                obs_text = str(observation)
                preview = obs_text if len(obs_text) <= 80 else obs_text[:77] + "..."
                steps.append({
                    "tool": action.tool,
                    "input": action.tool_input,
                    "result": preview,
                    "summary": _summarize_step(action.tool, action.tool_input, obs_text),
                })
            return {
                "reply": result["output"],
                "steps": steps,
                "pending": erp_tools.PENDING_WRITES.copy(),
            }
        except Exception as e:
            last_error = e
            if any(marker in str(e) for marker in transient_markers):
                erp_tools.PENDING_WRITES.clear()  # don't leave a half-formed proposal
                time.sleep(1)  # brief pause before trying again
                continue
            break  # a different kind of error - retrying won't help

    return {
        "reply": (f"The assistant had trouble formatting its response after "
                  f"{max_attempts} attempts. Please try rephrasing your request, "
                  f"or try again in a moment.\n\n(Technical detail: {last_error})"),
        "steps": [],
        "pending": [],
    }


# Thin wrapper so the Gradio app keeps working exactly as before -
# Gradio's ChatInterface expects a plain string back, nothing more.
def chat_fn(message, history):
    return run_agent_turn(message, history)["reply"]


# 5. Launch the chat UI.
demo = gr.ChatInterface(
    fn=chat_fn,
    type="messages",
    title="ERP AI Assistant",
    description="Ask in plain English. Write actions (approve/reject) will ask you to "
                "confirm before changing anything.",
    examples=[
        "Show me all pending purchase orders",
        "Create a vendor called Falcon IT in the IT category",
        "Create a purchase order of 2500 for ACME Trading for printer ink",
        "Approve all pending purchase orders under 5000",
    ],
)

if __name__ == "__main__":
    demo.launch()
