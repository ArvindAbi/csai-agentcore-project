"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the project instructions and rubric for guidance.
Work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
    AfterToolCallEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")
logger.setLevel(logging.INFO)

# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
# Create a BedrockAgentCoreApp instance.
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.
#
# Hint: app = BedrockAgentCoreApp()

# TODO: Create the BedrockAgentCoreApp instance
app = BedrockAgentCoreApp()


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────
# Replace the placeholder strings with your actual AWS resource values.
# You collected these in the infrastructure setup section of the project instructions.
#
# GATEWAY_URL format: https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# This starter uses an unsigned MCP connection and therefore assumes the
# project Gateway is configured with the NONE authorizer.
# KB_ID       format: 10-character alphanumeric string from the KB console
# REGION:     your AWS region, e.g. "us-east-1"
# MEMORY_ID   format: shown in the AgentCore Memory console

GATEWAY_URL = "https://customersupportgateway-h48fjafkiq.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "VULIOY6YSE"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-wotGFWCS5k"


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
# Create:
#   1. A BedrockModel using model_id "global.amazon.nova-2-lite-v1:0"
#   2. A MemoryClient with region_name=REGION
#   3. A boto3 client for the "bedrock-agent-runtime" service in REGION
#
# Hint: model = BedrockModel(model_id=model_id)

model_id = "global.amazon.nova-2-lite-v1:0"

# TODO: Create the BedrockModel instance
model = BedrockModel(model_id=model_id, temperature=0)

# TODO: Create the MemoryClient instance
memory_client = MemoryClient(region_name=REGION)

# TODO: Create the boto3 bedrock-agent-runtime client
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.
#
# Steps:
#   1. Call mem_client.get_memory_strategies(memory_id) to get strategy list
#   2. Read the namespace from strategy["namespaceTemplates"][0].
#      For compatibility with older AgentCore responses, fall back to
#      strategy["namespaces"][0] when namespaceTemplates is absent.
#
# Example output:
#   { "SEMANTIC": "cs_agent/{actorId}/facts",
#     "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    # TODO: Implement this function
    namespaces = {}
    for strategy in mem_client.get_memory_strategies(memory_id):
        strategy_type = strategy.get("type") or strategy.get("memoryStrategyType")
        templates = strategy.get("namespaceTemplates") or strategy.get("namespaces") or []
        if strategy_type and templates:
            namespaces[strategy_type] = templates[0]
    return namespaces


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
# Implement MemoryHook, a HookProvider subclass that adds long-term memory.
#
# The class needs:
#   __init__(self, actor_id, session_id, memory_client, memory_id)
#     — store all four as instance attributes
#     — call get_namespaces() and store the result as self.namespaces
#
#   retrieve_customer_context(self, event: MessageAddedEvent)
#     — only runs for plain-text user messages (not tool results)
#     — for each strategy namespace, call memory_client.retrieve_memories(
#          memory_id, namespace (formatted with actorId), query, top_k=5)
#     — collect non-empty memory texts tagged with their strategy type
#     — if any memories found, prepend them to the user message as:
#          "Customer Context:\n<memories>\n\n<original_message>"
#
#   save_support_interaction(self, event: AfterInvocationEvent)
#     — walk the message list backwards to find the last plain-text user
#       query and the last assistant response
#     — call memory_client.create_event(memory_id, actor_id, session_id,
#          messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")])
#
#   register_hooks(self, registry: HookRegistry)
#     — register retrieve_customer_context on MessageAddedEvent
#     — register save_support_interaction on AfterInvocationEvent

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        # TODO: Store actor_id, session_id, memory_id, memory_client as attributes
        # TODO: Call get_namespaces() and store the result as self.namespaces
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_id = memory_id
        self.memory_client = memory_client
        self.namespaces = get_namespaces(memory_client, memory_id)
        # Kept so the saved query is the customer's own text, not the
        # memories we prepended to it.
        self.context_prefix = ""

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        # TODO: Implement memory retrieval
        # Steps:
        #   1. Get the last message from event.agent.messages
        #   2. Check it is a user message and not a tool result
        #   3. Extract the user query text
        #   4. For each namespace in self.namespaces, call retrieve_memories()
        #   5. Collect non-empty memory texts with strategy type tags
        #   6. If any found, prepend them to the user message
        message = event.agent.messages[-1]
        content = message.get("content") or []
        if message.get("role") != "user" or not content or "toolResult" in content[0]:
            return
        user_query = content[0].get("text", "")
        if not user_query:
            return

        try:
            memories = []
            for strategy_type, namespace in self.namespaces.items():
                records = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace.format(actorId=self.actor_id),
                    query=user_query,
                    top_k=5,
                )
                for record in records:
                    text = record.get("content", {}).get("text", "").strip()
                    if text:
                        memories.append(f"[{strategy_type}] {text}")

            if memories:
                self.context_prefix = "Customer Context:\n" + "\n".join(memories) + "\n\n"
                content[0]["text"] = self.context_prefix + user_query
        except Exception as e:
            logger.error("Could not retrieve customer context: %s", e)

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        # TODO: Implement memory saving
        # Steps:
        #   1. Get messages from event.agent.messages
        #   2. Walk backwards to find the last user query (plain text)
        #      and the last assistant response
        #   3. Call memory_client.create_event() with both messages
        try:
            customer_query = None
            agent_response = None
            for message in reversed(event.agent.messages):
                content = message.get("content") or []
                if not content:
                    continue
                if message["role"] == "assistant" and agent_response is None:
                    agent_response = next((c["text"] for c in content if "text" in c), None)
                elif message["role"] == "user" and "text" in content[0]:
                    customer_query = content[0]["text"].removeprefix(self.context_prefix)
                    break

            if customer_query and agent_response:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")],
                )
        except Exception as e:
            logger.error("Could not save interaction to memory: %s", e)

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        # TODO: Register retrieve_customer_context on MessageAddedEvent
        # TODO: Register save_support_interaction on AfterInvocationEvent
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
# Implement search_knowledge_base(query) using the @tool decorator.
#
# Steps:
#   1. Guard: if KB_ID is empty return "Knowledge base not configured."
#   2. Call _bedrock_runtime.retrieve(
#          knowledgeBaseId=KB_ID,
#          retrievalQuery={"text": query}
#      )
#   3. Extract resp["retrievalResults"]; return a message if empty
#   4. Join the text chunks with "\n---\n" and return the result
#
# The docstring is the tool description — the model uses it to decide when
# to call this tool, so keep it clear and accurate.

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    # TODO: Implement the Knowledge Base search
    if not KB_ID or KB_ID.startswith("<"):
        return "Knowledge base not configured. Set KB_ID in main.py to your Knowledge Base ID."

    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
    except Exception as e:
        logger.error("Knowledge base search failed: %s", e)
        return f"Knowledge base search failed: {e}"

    results = resp.get("retrievalResults", [])
    if not results:
        return "No matching information found in the knowledge base."
    return "\n---\n".join(r["content"]["text"] for r in results)


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
# Implement calculate_loyalty_discount() using the @tool decorator.
#
# The tool must:
#   1. Build a self-contained Python code string that:
#        • Defines earn_rates: {"standard": 1, "device": 2, "fresh": 5}
#        • Defines tier_rates: {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
#        • Calculates points_redeemed (floor to nearest 500, cap at 50% of order)
#        • Calculates tier_discount (applied to subtotal after points)
#        • Calculates final_total, total_savings, points_earned, remaining_points
#        • Prints a JSON result dict
#   2. Execute the code with code_session(REGION).invoke("executeCode", {...})
#      using language="python" and clearContext=True
#   3. Return the first result event as a JSON string
#   4. Include a fallback that computes only the tier discount if the
#      Code Interpreter is unavailable

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    # TODO: Build the code string (use an f-string to inject the arguments)
    tier = tier.strip().capitalize()
    product_category = product_category.strip().lower()

    code = f"""
import json, math

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

points = {int(loyalty_points)}
tier = {tier!r}
order_total = round({float(order_total)}, 2)
category = {product_category!r}

# 100 points = $1, redeemed in blocks of 500, never more than half the order
max_points = int(order_total * 0.5 * 100)
points_redeemed = (min(points, max_points) // 500) * 500
points_value = points_redeemed / 100

subtotal = order_total - points_value
rate = tier_rates.get(tier, 0.0)
tier_discount = round(subtotal * rate, 2)
final_total = round(subtotal - tier_discount, 2)

points_earned = math.floor(final_total * earn_rates.get(category, 1))
remaining_points = points - points_redeemed + points_earned

# Ready-made sentence so the agent quotes these figures instead of redoing them
summary = (
    f"Redeemed {{points_redeemed}} points (${{points_value:.2f}}). "
    f"{{tier}} discount {{round(rate * 100)}}% on ${{subtotal:.2f}} = ${{tier_discount:.2f}}. "
    f"Final total ${{final_total:.2f}}, total savings ${{order_total - final_total:.2f}}. "
    f"Points earned {{points_earned}}, remaining points {{remaining_points}}."
)

print(json.dumps({{
    "tier": tier,
    "order_total": order_total,
    "points_redeemed": points_redeemed,
    "points_value": round(points_value, 2),
    "tier_discount_pct": round(rate * 100),
    "tier_discount": tier_discount,
    "final_total": final_total,
    "total_savings": round(order_total - final_total, 2),
    "points_earned": points_earned,
    "remaining_points": remaining_points,
    "summary": summary,
}}))
"""

    try:
        # TODO: Execute the code using code_session and return the result
        with code_session(REGION) as client:
            response = client.invoke("executeCode", {
                "code": code,
                "language": "python",
                "clearContext": True,
            })
            for event in response["stream"]:
                result = event["result"]
                if result.get("isError"):
                    raise RuntimeError(result["content"][0]["text"])
                output = result.get("structuredContent", {}).get("stdout") or result["content"][0]["text"]
                return json.dumps(json.loads(output))
        raise RuntimeError("Code Interpreter returned no result")

    except Exception as e:
        # TODO: Implement fallback calculation using tier discount only
        logger.warning("Code Interpreter unavailable, using tier discount only: %s", e)
        rate = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}.get(tier, 0.0)
        tier_discount = round(order_total * rate, 2)
        return json.dumps({
            "tier": tier,
            "order_total": round(order_total, 2),
            "points_redeemed": 0,
            "tier_discount_pct": round(rate * 100),
            "tier_discount": tier_discount,
            "final_total": round(order_total - tier_discount, 2),
            "remaining_points": loyalty_points,
            "note": "Code Interpreter was unavailable, so only the tier discount was applied and no points were redeemed.",
        })


class ToolErrorLogger(HookProvider):
    """Logs tool calls that fail, so a Gateway or Lambda problem shows up in the runtime logs."""

    def log_failed_tool(self, event: AfterToolCallEvent):
        name = event.tool_use.get("name", "unknown")
        if event.exception is not None:
            logger.error("Tool %s raised %s: %s", name, type(event.exception).__name__, event.exception)
        elif event.result and event.result.get("status") == "error":
            detail = " ".join(c["text"] for c in event.result.get("content", []) if "text" in c)
            logger.warning("Tool %s returned an error: %s", name, detail[:300])

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        registry.add_callback(AfterToolCallEvent, self.log_failed_tool)


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────
# Implement the invoke() function decorated with @app.entrypoint.
#
# Steps:
#   1. Extract user_input, actor_id, and session_id from the payload
#      (generate a UUID if session_id is missing)
#   2. Instantiate MemoryHook for this actor/session
#   3. Instantiate AgentCoreBrowser(region=REGION)
#   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
#                              agent_core_browser.browser]
#   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
#   6. Create and invoke the Agent with all tools, hooks, and system_prompt
#   7. Return the text from the first content block of the response
#   8. Handle exceptions gracefully

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    # TODO: Implement the agent invocation
    user_input = payload.get("prompt", "")
    actor_id = payload.get("customer_id") or "guest"
    session_id = payload.get("session_id") or str(uuid.uuid4())

    system_prompt = f"""You are a customer support assistant for an online store.
The customer you are talking to has the ID {actor_id}.

How to help:
- Order status, order history and customer details: use the order tools.
- Refunds, refund status and return labels: use the refund tools. To start a refund, first get the order with the order tool, then call initiate_refund with amount set to the order total. Never call initiate_refund without the amount.
- Products, return policy, warranty, loyalty tiers and order status meanings: use search_knowledge_base and answer only from what it returns.
- Any loyalty discount or points calculation: use calculate_loyalty_discount. Do not do the maths yourself. Give the customer the figures from its summary exactly as written.
- Anything on a live web page: use the browser tool.

If the message starts with "Customer Context", that is what you remember about this customer from earlier sessions. Use it to personalise the reply, for example their name or how they like answers. It is not a source for order details, amounts or statuses; always get those from the tools.
Never make up order details, refund IDs or prices. If a tool fails, say so plainly.
Keep replies clear and to the point."""

    try:
        memory_hook = MemoryHook(actor_id, session_id, memory_client, MEMORY_ID)
        agent_core_browser = AgentCoreBrowser(region=REGION)
        tools = [search_knowledge_base, calculate_loyalty_discount, agent_core_browser.browser]

        # A Gateway problem should not take the whole agent down. The connection is
        # opened inside the try because an unreachable URL fails right there,
        # before any tool is listed.
        gateway_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
        gateway_tools = []
        gateway_started = False
        try:
            gateway_client.start()
            gateway_started = True
            page = gateway_client.list_tools_sync()
            gateway_tools = list(page)
            while page.pagination_token:
                page = gateway_client.list_tools_sync(pagination_token=page.pagination_token)
                gateway_tools.extend(page)
            tools.extend(gateway_tools)
            logger.info("Gateway connected successfully. Loaded %d tools.", len(gateway_tools))
        except TimeoutError:
            logger.exception("Gateway tool loading timed out")
        except ConnectionError:
            logger.exception("Gateway connection failed")
        except Exception as exc:
            # The MCP client wraps the real error (DNS, refused connection, 403 ...)
            # a few levels deep, so dig it out for a useful log line.
            cause = exc
            while True:
                if isinstance(cause, BaseExceptionGroup) and cause.exceptions:
                    cause = cause.exceptions[0]
                elif cause.__cause__ is not None:
                    cause = cause.__cause__
                else:
                    break
            logger.exception("Gateway tool loading failed: %s: %s", type(cause).__name__, cause)

        if not gateway_tools:
            system_prompt += (
                "\n\nThe order and refund systems cannot be reached right now. If the customer asks "
                "about orders, refunds or returns, tell them this and ask them to try again in a few minutes."
            )

        try:
            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook, ToolErrorLogger()],
                system_prompt=system_prompt,
            )
            response = await agent.invoke_async(user_input)
        finally:
            if gateway_started:
                gateway_client.stop(None, None, None)

        return response.message["content"][0]["text"]

    except Exception:
        # Full details go to the logs only; the customer never sees raw AWS errors.
        logger.exception("Agent invocation failed")
        return "Sorry, something went wrong while handling your request. Please try again in a moment."


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
