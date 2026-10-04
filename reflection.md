# Reflection

**Design decision**

For the loyalty discount I kept all the maths inside the AgentCore Code Interpreter instead of letting the model work it out. The rules (500-point blocks, a cap at half the order, tier discount after points) are easy for an LLM to get slightly wrong, and a wrong price is worse than no answer. The tool builds a Python script with the customer's values and runs it with clearContext=True, so every calculation starts clean. If the sandbox is down, it falls back to a tier-only discount.

Still, Nova 2 Lite sometimes redid the maths in its reply. In one run it showed a $15 tier discount (10% of $150) while the tool had returned $11 (10% of $110). I added a ready-made summary sentence to the tool output, told the agent to use it as written, and set temperature to 0. After that the reply matched the tool exactly.

**Challenge**

The refund test first came back with "$0". The refund schema makes amount optional and the Lambda defaults it to 0, so the model was calling initiate_refund without looking up the order. I added a rule to fetch the order first and pass its total. It still skipped the lookup on the deployed agent, once leaving the amount out and once sending 139.0. The Lambda logs showed get_order was never called. The reason was memory: CUST-123 already had facts about the Kindle refund from earlier runs, and the model treated them as enough. I added a line saying Customer Context is only for personalising replies; amounts and statuses must come from the tools. The next run looked up the order and refunded $139.99.

**Production**

Right now the Gateway is open. It is set to NONE for login, so it does not check who is calling, and anyone who knows its URL could ask it to issue a refund. That is fine for a short class test, but not for real customers. Before going live I would add a proper login check on the Gateway so only our agent can use the tools, and take the customer ID from that login instead of trusting whatever comes in the request. I would also keep an eye on cost, because every message opens a fresh connection to the tools and can start a new sandbox or browser session.
