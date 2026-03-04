import { createClient } from "@supabase/supabase-js";
import { PinchTabAdapter } from "./adapters/pinchtab.js";
import { buildSystemPrompt } from "./llm/prompt.js";
import { callLLM } from "./llm/client.js";
import { executeToolCall } from "./llm/tools.js";
import { checkPaywall } from "./paywall.js";
import { getSupabaseClient } from "./db/supabase.js";

const POLL_INTERVAL_MS = 45_000;

async function poll(): Promise<void> {
  const db = getSupabaseClient();
  const adapter = new PinchTabAdapter();

  // Load config
  const { data: config } = await db
    .from("agent_config")
    .select("*")
    .eq("id", "default")
    .single();

  if (!config) {
    console.error("No agent config found");
    return;
  }

  // Load poll state
  const { data: pollState } = await db
    .from("poll_state")
    .select("*")
    .eq("agent_id", "default")
    .single();

  const since = pollState?.last_polled_at
    ? new Date(pollState.last_polled_at)
    : new Date(Date.now() - 60_000);

  const dms = await adapter.getNewDMs(since);

  for (const dm of dms) {
    try {
      await processDM(dm, config, adapter, db);
    } catch (err) {
      console.error(`Error processing DM from ${dm.fromUserId}:`, err);
    }
  }

  // Update poll state
  await db.from("poll_state").upsert({
    agent_id: "default",
    last_polled_at: new Date().toISOString(),
    last_dm_id: dms[dms.length - 1]?.id ?? pollState?.last_dm_id,
  });
}

async function processDM(
  dm: { id: string; fromUserId: string; text: string; createdAt: Date },
  config: Record<string, unknown>,
  adapter: { sendDM: (userId: string, text: string) => Promise<void> },
  db: ReturnType<typeof getSupabaseClient>
): Promise<void> {
  // Upsert user
  await db.from("x_users").upsert(
    { id: dm.fromUserId, last_seen_at: new Date().toISOString() },
    { onConflict: "id", ignoreDuplicates: false }
  );

  const { data: user } = await db
    .from("x_users")
    .select("*")
    .eq("id", dm.fromUserId)
    .single();

  if (!user) return;

  // Paywall check
  const { allowed, paymentPrompt } = checkPaywall(user, config);
  if (!allowed) {
    await adapter.sendDM(dm.fromUserId, paymentPrompt!);
    return;
  }

  // Load message history
  const { data: history } = await db
    .from("messages")
    .select("role, content, tool_calls")
    .eq("x_user_id", dm.fromUserId)
    .order("created_at", { ascending: false })
    .limit(config.max_context_messages as number);

  const messages = (history ?? []).reverse();

  // Build prompt and call LLM
  const systemPrompt = buildSystemPrompt(config);
  const { reply, toolCalls } = await callLLM(systemPrompt, messages, dm.text, config.model as string);

  // Execute tool calls
  for (const tc of toolCalls ?? []) {
    await executeToolCall(tc, dm.fromUserId, adapter, db);
  }

  // Send reply
  await adapter.sendDM(dm.fromUserId, reply);

  // Persist messages
  await db.from("messages").insert([
    { x_user_id: dm.fromUserId, role: "user", content: dm.text },
    { x_user_id: dm.fromUserId, role: "assistant", content: reply, tool_calls: toolCalls },
  ]);

  // Increment free message count
  await db
    .from("x_users")
    .update({ free_messages_used: (user.free_messages_used ?? 0) + 1 })
    .eq("id", dm.fromUserId);
}

async function main(): Promise<void> {
  console.log("XDM Agent starting…");
  // eslint-disable-next-line no-constant-condition
  while (true) {
    await poll().catch((err) => console.error("Poll error:", err));
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
  }
}

main();
