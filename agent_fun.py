# agent_fun.py — Weekend Wizard Agent (fully local via Ollama)
#
# Requires Ollama running locally: https://ollama.com
# Pull the model first:  ollama pull mistral
# Then run:              python agent_fun.py

import asyncio, json, sys, os, re
from typing import Dict, Any, List
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import ollama

# ─── Configuration ─────────────────────────────────────────────────────────────

MODEL       = os.environ.get("OLLAMA_MODEL", "mistral")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MAX_STEPS   = 8  # safety cap for the agentic loop

# Verify Ollama is reachable before starting
_ollama_client = ollama.Client(host=OLLAMA_HOST)
try:
    _ollama_client.list()
except Exception:
    print(f"\n[ERROR] Cannot connect to Ollama at {OLLAMA_HOST}")
    print("  1. Install Ollama from: https://ollama.com")
    print("  2. Start it:  ollama serve")
    print(f"  3. Pull the model: ollama pull {MODEL}")
    print("  Then re-run this script.\n")
    sys.exit(1)

# ─── System Prompt ─────────────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = """\
You are Weekend Wizard, a cheerful AI that helps people plan amazing weekends.

You have access to these MCP tools:
{tools_description}

## ReAct Protocol (follow this exactly)
Think step-by-step. For EVERY response output ONLY a single valid JSON object — no prose, no markdown fences.

To call a tool:
{{"action": "<tool_name>", "args": {{"<param>": <value>}}}}

To give your final answer:
{{"action": "final", "answer": "<your friendly, well-formatted answer>"}}

## Rules
- If the user mentions a city but no coordinates, call city_to_coords FIRST.
- For a full weekend plan: fetch weather, book_recs, random_joke, AND random_dog.
- Your final answer must be warm, structured, and directly reference the fetched data
  (actual temperature, real book titles, the exact joke text, the real dog URL).
- Keep the final answer between 6 and 12 lines — concise but complete.
- Do NOT call a tool more than once per session unless results differ.
"""

# ─── Tool Description Builder ──────────────────────────────────────────────────

def _build_tools_description(tools) -> str:
    lines = []
    for t in tools:
        schema   = getattr(t, "inputSchema", {}) or {}
        props    = schema.get("properties", {})
        required = schema.get("required", [])
        params   = []
        for k, v in props.items():
            ptype  = v.get("type", "any")
            marker = "" if k in required else "?"
            params.append(f"{k}{marker}: {ptype}")
        param_str = ", ".join(params) if params else ""
        desc      = (getattr(t, "description", "") or "").strip()
        lines.append(f"  - {t.name}({param_str}) : {desc}")
    return "\n".join(lines)

# ─── JSON Extraction ───────────────────────────────────────────────────────────

def _extract_json(text: str) -> Dict[str, Any]:
    """Robustly extract a JSON object from LLM output."""
    text = text.strip()

    # 1. Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Strip markdown code fences
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 3. Find the largest well-formed JSON object in the text
    for start in [i for i, ch in enumerate(text) if ch == "{"]:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    return {}  # failed


# ─── LLM Wrappers ──────────────────────────────────────────────────────────────

def _llm_raw(messages: List[Dict[str, str]], temperature: float = 0.2, max_tokens: int = 1024) -> str:
    resp = _ollama_client.chat(
        model=MODEL,
        messages=messages,
        options={"temperature": temperature, "num_predict": max_tokens},
    )
    return resp["message"]["content"].strip()


def llm_json(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """Ask the LLM for a JSON action decision; repair if needed."""
    txt    = _llm_raw(messages, temperature=0.2)
    result = _extract_json(txt)

    if "action" not in result:
        # One-shot JSON repair
        repair = _llm_raw(
            messages=[
                {
                    "role": "system",
                    "content": (
                        'Convert the text below into a single valid JSON with an "action" key. '
                        'Tool call format: {"action":"tool_name","args":{...}} '
                        'Final answer format: {"action":"final","answer":"..."} '
                        "Output ONLY the JSON."
                    ),
                },
                {"role": "user", "content": txt},
            ],
            temperature=0,
        )
        result = _extract_json(repair)

    if "action" not in result:
        # Last resort: treat raw text as the final answer
        result = {"action": "final", "answer": txt}

    return result


def llm_reflect(answer: str) -> str:
    """One-shot reflection pass — returns improved answer or 'looks good'."""
    resp = _llm_raw(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a quality reviewer for weekend plans. "
                    "If the plan is complete, friendly, and references real data "
                    "(temperature, book titles, joke text, dog URL), reply with EXACTLY: looks good\n"
                    "Otherwise provide an improved version of the plan."
                ),
            },
            {"role": "user", "content": answer},
        ],
        temperature=0,
        max_tokens=1024,
    )
    return resp


# ─── Main Agent Loop ───────────────────────────────────────────────────────────

async def main():
    server_path = sys.argv[1] if len(sys.argv) > 1 else "server_fun.py"

    print("=" * 50)
    print("       Weekend Wizard  - Powered by Ollama")
    print("=" * 50)
    print(f"  Model : {MODEL}  (via {OLLAMA_HOST})")

    exit_stack = AsyncExitStack()
    try:
        # Spawn the MCP tools server as a subprocess (stdio transport)
        stdio = await exit_stack.enter_async_context(
            stdio_client(
                StdioServerParameters(command=sys.executable, args=[server_path])
            )
        )
        r_in, w_out = stdio
        session = await exit_stack.enter_async_context(ClientSession(r_in, w_out))
        await session.initialize()

        tools      = (await session.list_tools()).tools
        tool_index = {t.name: t for t in tools}
        tools_desc = _build_tools_description(tools)

        print(f"  Tools : {', '.join(tool_index.keys())}")
        print("=" * 50)
        print()
        print("Example prompts:")
        print('  Plan a cozy Saturday in NYC at (40.7128, -74.0060) with mystery books')
        print('  What is the weather at (37.7749, -122.4194)?')
        print('  Give me a trivia question')
        print('  Plan a fun Sunday in London (use the city name)')
        print()
        print("Type 'exit' to quit.")
        print()

        SYSTEM = _SYSTEM_TEMPLATE.format(tools_description=tools_desc)

        while True:
            try:
                user = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user or user.lower() in {"exit", "quit", "bye"}:
                break

            # Fresh conversation history per user query
            history: List[Dict[str, str]] = [
                {"role": "system", "content": SYSTEM},
                {"role": "user",   "content": user},
            ]

            print("\n[Wizard is thinking", end="", flush=True)
            final_answer: str | None = None

            # ── Agentic ReAct loop ────────────────────────────────────────────
            for _step in range(MAX_STEPS):
                decision = llm_json(history)

                # ── Final answer? ─────────────────────────────────────────────
                if decision.get("action") == "final":
                    final_answer = decision.get("answer", "")
                    break

                # ── Tool call ─────────────────────────────────────────────────
                tname = decision.get("action", "")
                args  = decision.get("args", {})

                if tname not in tool_index:
                    history.append({"role": "assistant", "content": json.dumps(decision)})
                    history.append({
                        "role": "user",
                        "content": (
                            f"Tool '{tname}' does not exist. "
                            f"Available tools: {list(tool_index.keys())}. "
                            "Please use a valid tool or produce a final answer."
                        ),
                    })
                    print("?", end="", flush=True)
                    continue

                print(".", end="", flush=True)

                try:
                    result  = await session.call_tool(tname, args)
                    payload = result.content[0].text if result.content else result.model_dump_json()
                except Exception as exc:
                    payload = json.dumps({"error": str(exc)})

                history.append({"role": "assistant", "content": json.dumps(decision)})
                history.append({"role": "user",      "content": f"Result of {tname}: {payload}"})

            if final_answer is None:
                final_answer = "I reached the step limit. Please try a simpler or more specific prompt."

            # ── One-shot reflection ───────────────────────────────────────────
            reflected = llm_reflect(final_answer)
            if reflected.lower().strip() != "looks good":
                final_answer = reflected

            print("]\n")
            print(f"Wizard:\n{final_answer}")
            print()
            print("-" * 50)
            print()

    finally:
        await exit_stack.aclose()
        print("\nHave a great weekend! See you next time!")


if __name__ == "__main__":
    asyncio.run(main())
