export function buildSystemPrompt(config: Record<string, unknown>): string {
  const parts: string[] = [];

  if (config.system_prompt) parts.push(config.system_prompt as string);
  if (config.context_about) parts.push(`## About\n${config.context_about}`);
  if (config.context_faq) parts.push(`## FAQ\n${config.context_faq}`);
  if (config.context_offers) parts.push(`## Offers\n${config.context_offers}`);
  if (config.context_style) parts.push(`## Style Guidelines\n${config.context_style}`);

  parts.push(
    `## Rules\n` +
    `- You are responding to DMs on X (Twitter).\n` +
    `- Keep replies concise and natural — this is a DM conversation.\n` +
    `- Use tools when you need to deliver content or handle payments.\n` +
    `- Never reveal you are an AI unless explicitly asked.`
  );

  return parts.join("\n\n");
}
