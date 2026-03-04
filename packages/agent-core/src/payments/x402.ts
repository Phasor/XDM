import { Hono } from "hono";
import { serve } from "@hono/node-server";
import { getSupabaseClient } from "../db/supabase.js";

const AGENT_WALLET_ADDRESS = process.env.AGENT_WALLET_ADDRESS ?? "";
const USDC_CONTRACT_BASE = process.env.USDC_CONTRACT_BASE ?? "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const FACILITATOR_URL = process.env.X402_FACILITATOR_URL ?? "https://x402.org/facilitator";

export function startPaymentServer(port = 3000): void {
  const app = new Hono();
  const db = getSupabaseClient();

  // X402 payment page for chat access or content
  app.get("/pay/:itemType/:itemId", async (c) => {
    const { itemType, itemId } = c.req.param();
    const uid = c.req.query("uid");

    // Fetch price from DB
    let priceUsd: number;
    if (itemType === "chat") {
      const { data: config } = await db
        .from("agent_config")
        .select("chat_session_price_usd")
        .eq("id", "default")
        .single();
      priceUsd = Number(config?.chat_session_price_usd ?? 5);
    } else {
      const { data: item } = await db
        .from("content_items")
        .select("price_usd")
        .eq("id", itemId)
        .single();
      priceUsd = Number(item?.price_usd ?? 0);
    }

    return c.json({
      facilitatorUrl: FACILITATOR_URL,
      scheme: "exact",
      network: "base-mainnet",
      maxAmountRequired: String(priceUsd * 1_000_000), // USDC has 6 decimals
      resource: c.req.url,
      payTo: AGENT_WALLET_ADDRESS,
      asset: USDC_CONTRACT_BASE,
      extra: { uid, itemType, itemId },
    });
  });

  // Webhook called by X402 facilitator on confirmed payment
  app.post("/webhook/x402", async (c) => {
    const body = await c.req.json<{
      tx_hash: string;
      extra: { uid: string; itemType: string; itemId: string };
      amount: string;
    }>();

    const { uid, itemType, itemId } = body.extra;

    await db.from("payments").insert({
      id: body.tx_hash,
      x_user_id: uid,
      item_type: itemType,
      item_id: itemId,
      usdc_amount: Number(body.amount) / 1_000_000,
      tx_hash: body.tx_hash,
      status: "confirmed",
      confirmed_at: new Date().toISOString(),
    });

    if (itemType === "chat") {
      const { data: config } = await db
        .from("agent_config")
        .select("chat_session_hours")
        .eq("id", "default")
        .single();
      const hours = Number(config?.chat_session_hours ?? 24);
      const until = new Date(Date.now() + hours * 3_600_000).toISOString();
      await db.from("x_users").update({ chat_access_until: until }).eq("id", uid);
    } else {
      await db.from("content_access").upsert({ x_user_id: uid, item_id: itemId });
    }

    return c.json({ ok: true });
  });

  serve({ fetch: app.fetch, port });
  console.log(`Payment server listening on port ${port}`);
}
