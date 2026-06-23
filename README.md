# ERP AI Assistant 🤖

A natural-language assistant that sits on top of an ERP system. You type plain
English ("show me all pending approvals", "create a vendor called Falcon IT",
"approve all pending purchase orders under 5000") and an **LLM agent** figures
out which ERP API calls to make — in the right order — and executes them.

> Built with an LLM **agent** (LangChain + Groq) over a **mock ERP REST API**
> (FastAPI). The ERP data is simulated, so this demonstrates the agent and
> integration pattern, not a live SAP/Sage connection.

---

## What's in this project

| File | What it is |
|------|------------|
| `erp_api.py`    | A fake ERP system (FastAPI). Stores vendors + purchase orders. This is the "backend". |
| `erp_tools.py`  | The tools the AI is allowed to use. Each one calls the ERP API over HTTP. |
| `agent_app.py`  | The AI agent (LangChain + Groq) + a Gradio chat window. |
| `requirements.txt` | The exact library versions to install. |
| `.env.example`  | Template for your secret Groq API key. |

### How the pieces talk to each other

```
You (browser chat)
      │
      ▼
agent_app.py  ── the LLM agent decides WHAT to do, in a loop
      │
      ▼ (calls a tool)
erp_tools.py  ── each tool makes an HTTP request
      │
      ▼
erp_api.py    ── the mock ERP answers (vendors, POs, etc.)
```

The agent can ONLY act through the tools. It cannot invent data. That's what
makes it safe and realistic.

---

## Setup (do this once)

**1. Install the libraries**

```bash
pip install -r requirements.txt
```

**2. Get a free Groq API key**

Go to https://console.groq.com/keys, create a key, then:

```bash
cp .env.example .env
```

Open the new `.env` file and paste your key after `GROQ_API_KEY=`.

---

## Running it (you need TWO terminals)

This is on purpose — the ERP and the agent are two separate services, exactly
like in the real world.

**Terminal 1 — start the mock ERP:**

```bash
uvicorn erp_api:app --reload --port 8000
```

Leave it running. (You can open http://127.0.0.1:8000/docs to see the API.)

**Terminal 2 — start the AI assistant:**

```bash
python agent_app.py
```

It will print a local URL (like http://127.0.0.1:7860). Open it and start chatting.

---

## Things to try (and what they prove)

| You type | What the agent does | Why it matters |
|----------|--------------------|----------------|
| "Show me all pending purchase orders" | One read call | Basic tool use |
| "Create a purchase order of 2500 for ACME Trading" | Looks up ACME's id, *then* creates the PO | **Multi-step**: step 2 uses step 1's result |
| "Approve all pending purchase orders under 5000" | Lists pending → filters → approves each | **A real agent loop** — it plans the steps itself |

That middle and last row are the important ones. A request that needs several
chained calls — where later steps depend on earlier results — is what makes
this *agentic* and not just a translator.

---

## How to talk about this in an interview

- **"What makes it an agent?"** The LLM is given a goal and a set of tools, then
  runs in a loop: think → pick a tool → read the result → decide the next step,
  until done. I don't hard-code the sequence; the model plans it at runtime.
- **"How is this different from your n8n GL workflow?"** n8n is *deterministic* —
  I drew the boxes and the path is fixed. This agent decides the path itself
  based on the request. One is workflow automation, one is LLM planning.
- **"What are its weaknesses?"** It's a mock ERP, so no real auth or transactions.
  LLM agents can also pick the wrong tool or loop, which is why I added a
  `max_iterations` cap and write-action guardrails in the system prompt.
- **"What would you do next?"** Point the tools at a real OData service with
  XSUAA/JWT auth, and add human-in-the-loop confirmation before write actions.

---

## Tech stack

LangChain agent · Groq (`llama-3.3-70b-versatile`) · FastAPI · Gradio · Python
