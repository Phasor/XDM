import { TOOLS } from "./tools.js";

interface Message {
  role: string;
  content: string;
  tool_calls?: unknown;
}

interface LLMResponse {
  reply: string;
  toolCalls: Array<{ name: string; arguments: Record<string, string> }> | null;
}

export async function callLLM(
  systemPrompt: string,
  history: Message[],
  userMessage: string,
  model: string
): Promise<LLMResponse> {
  const messages = [
    ...history.map((m) => ({ role: m.role, content: m.content })),
    { role: "user", content: userMessage },
  ];

  const res = await fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${process.env.OPENROUTER_API_KEY}`,
    },
    body: JSON.stringify({
      model,
      messages: [{ role: "system", content: systemPrompt }, ...messages],
      tools: TOOLS,
      tool_choice: "auto",
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`OpenRouter error ${res.status}: ${body}`);
  }

  const data = (await res.json()) as {
    choices: Array<{
      message: {
        content: string | null;
        tool_calls?: Array<{
          function: { name: string; arguments: string };
        }>;
      };
    }>;
  };

  const choice = data.choices[0].message;
  const toolCalls = choice.tool_calls?.map((tc) => ({
    name: tc.function.name,
    arguments: JSON.parse(tc.function.arguments) as Record<string, string>,
  })) ?? null;

  return { reply: choice.content ?? "", toolCalls };
}
